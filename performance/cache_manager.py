#!/usr/bin/env python3
"""
Система кэширования для оптимизации повторяющихся запросов к БД.

Использует Redis для кэширования результатов и автоматической инвалидации.
"""

import os
import json
import hashlib
import logging
import pickle
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Callable, Union
from dataclasses import dataclass
from functools import wraps
import argparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Опциональный импорт Redis
try:
    import redis
    REDIS_AVAILABLE = True
except ImportError:
    REDIS_AVAILABLE = False
    logger.warning("Redis not available, using in-memory cache")


@dataclass
class CacheStats:
    """Статистика кэша."""
    hits: int
    misses: int
    total_requests: int
    hit_ratio: float
    total_size_mb: float
    entries_count: int
    avg_access_time_ms: float


class QueryCacheManager:
    """Менеджер кэширования запросов."""
    
    def __init__(self, config_path: str = "performance/cache_config.json"):
        self.config = self._load_config(config_path)
        self.stats = CacheStats(0, 0, 0, 0.0, 0.0, 0, 0.0)
        self._access_times = []
        
        # Инициализируем кэш
        if REDIS_AVAILABLE and self.config.get("use_redis", True):
            self._init_redis_cache()
        else:
            self._init_memory_cache()
    
    def _load_config(self, config_path: str) -> Dict:
        """Загрузить конфигурацию кэша."""
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return json.load(f)
        
        # Конфигурация по умолчанию
        default_config = {
            "use_redis": True,
            "redis": {
                "host": "localhost",
                "port": 6379,
                "db": 0,
                "password": None,
                "socket_timeout": 5,
                "socket_connect_timeout": 5,
                "decode_responses": False  # Для работы с pickle
            },
            "cache_policies": {
                "gtin_search": {
                    "ttl_seconds": 3600,  # 1 час
                    "max_entries": 10000,
                    "enabled": True
                },
                "product_details": {
                    "ttl_seconds": 1800,  # 30 минут
                    "max_entries": 5000,
                    "enabled": True
                },
                "address_normalized": {
                    "ttl_seconds": 86400,  # 24 часа (адреса редко меняются)
                    "max_entries": 20000,
                    "enabled": True
                },
                "manufacturer_products": {
                    "ttl_seconds": 600,   # 10 минут
                    "max_entries": 1000,
                    "enabled": True
                },
                "statistics": {
                    "ttl_seconds": 300,   # 5 минут
                    "max_entries": 100,
                    "enabled": True
                }
            },
            "global_settings": {
                "max_memory_mb": 512,
                "eviction_policy": "allkeys-lru",
                "enable_compression": True,
                "compression_threshold_bytes": 1024
            },
            "monitoring": {
                "log_slow_cache_operations_ms": 100,
                "track_access_patterns": True,
                "alert_on_low_hit_ratio": 0.7
            }
        }
        
        # Создаем директорию и сохраняем конфигурацию
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(default_config, f, indent=2)
        
        return default_config
    
    def _init_redis_cache(self):
        """Инициализировать Redis кэш."""
        redis_config = self.config["redis"]
        
        try:
            self.cache = redis.Redis(
                host=redis_config["host"],
                port=redis_config["port"],
                db=redis_config["db"],
                password=redis_config.get("password"),
                socket_timeout=redis_config["socket_timeout"],
                socket_connect_timeout=redis_config["socket_connect_timeout"],
                decode_responses=redis_config["decode_responses"]
            )
            
            # Проверяем соединение
            self.cache.ping()
            
            # Настраиваем политику выселения
            global_settings = self.config["global_settings"]
            if global_settings.get("max_memory_mb"):
                max_memory = f"{global_settings['max_memory_mb']}mb"
                self.cache.config_set("maxmemory", max_memory)
                self.cache.config_set("maxmemory-policy", global_settings["eviction_policy"])
            
            self.cache_type = "redis"
            logger.info(f"Redis cache initialized: {redis_config['host']}:{redis_config['port']}")
            
        except Exception as e:
            logger.error(f"Failed to initialize Redis cache: {e}")
            self._init_memory_cache()
    
    def _init_memory_cache(self):
        """Инициализировать кэш в памяти."""
        self.cache = {}
        self.cache_meta = {}  # Метаданные для TTL и статистики
        self.cache_type = "memory"
        logger.info("In-memory cache initialized")
    
    def _generate_cache_key(self, category: str, query: str, params: tuple = None) -> str:
        """Генерировать ключ кэша."""
        # Создаем уникальный ключ из категории, запроса и параметров
        key_data = f"{category}:{query}"
        if params:
            key_data += f":{str(params)}"
        
        # Хэшируем для получения стабильного ключа
        key_hash = hashlib.md5(key_data.encode()).hexdigest()
        return f"cache:{category}:{key_hash}"
    
    def _compress_data(self, data: Any) -> bytes:
        """Сжать данные для хранения."""
        serialized = pickle.dumps(data)
        
        if (self.config["global_settings"]["enable_compression"] and
            len(serialized) > self.config["global_settings"]["compression_threshold_bytes"]):
            
            try:
                import gzip
                compressed = gzip.compress(serialized)
                # Добавляем маркер сжатия
                return b"COMPRESSED:" + compressed
            except Exception as e:
                logger.debug(f"Compression failed: {e}")
                return serialized
        
        return serialized
    
    def _decompress_data(self, data: bytes) -> Any:
        """Распаковать данные из кэша."""
        if data.startswith(b"COMPRESSED:"):
            try:
                import gzip
                compressed_data = data[11:]  # Убираем маркер "COMPRESSED:"
                decompressed = gzip.decompress(compressed_data)
                return pickle.loads(decompressed)
            except Exception as e:
                logger.error(f"Decompression failed: {e}")
                raise
        
        return pickle.loads(data)
    
    def get(self, category: str, query: str, params: tuple = None) -> Optional[Any]:
        """Получить данные из кэша."""
        # Проверяем, включено ли кэширование для категории
        if not self.config["cache_policies"].get(category, {}).get("enabled", True):
            return None
        
        cache_key = self._generate_cache_key(category, query, params)
        start_time = time.time()
        
        try:
            if self.cache_type == "redis":
                cached_data = self.cache.get(cache_key)
                if cached_data:
                    result = self._decompress_data(cached_data)
                    self._record_hit(start_time)
                    return result
                else:
                    self._record_miss(start_time)
                    return None
            
            else:  # memory cache
                if cache_key in self.cache:
                    # Проверяем TTL
                    meta = self.cache_meta.get(cache_key)
                    if meta and meta["expires_at"] > datetime.now():
                        self._record_hit(start_time)
                        return self.cache[cache_key]
                    else:
                        # Истек TTL
                        self._remove_from_memory_cache(cache_key)
                        self._record_miss(start_time)
                        return None
                else:
                    self._record_miss(start_time)
                    return None
                    
        except Exception as e:
            logger.error(f"Cache get error: {e}")
            self._record_miss(start_time)
            return None
    
    def set(self, category: str, query: str, data: Any, params: tuple = None, 
            ttl_seconds: Optional[int] = None) -> bool:
        """Сохранить данные в кэш."""
        # Проверяем, включено ли кэширование для категории
        cache_policy = self.config["cache_policies"].get(category, {})
        if not cache_policy.get("enabled", True):
            return False
        
        cache_key = self._generate_cache_key(category, query, params)
        
        # Используем TTL из конфигурации или переданный
        ttl = ttl_seconds or cache_policy.get("ttl_seconds", 3600)
        
        try:
            if self.cache_type == "redis":
                compressed_data = self._compress_data(data)
                return self.cache.setex(cache_key, ttl, compressed_data)
            
            else:  # memory cache
                # Проверяем лимит записей для категории
                max_entries = cache_policy.get("max_entries", 1000)
                current_category_entries = len([
                    k for k in self.cache.keys() 
                    if k.startswith(f"cache:{category}:")
                ])
                
                if current_category_entries >= max_entries:
                    # Удаляем самые старые записи
                    self._evict_oldest_entries(category, max_entries // 10)
                
                # Сохраняем данные и метаданные
                self.cache[cache_key] = data
                self.cache_meta[cache_key] = {
                    "created_at": datetime.now(),
                    "expires_at": datetime.now() + timedelta(seconds=ttl),
                    "category": category,
                    "access_count": 0
                }
                
                return True
                
        except Exception as e:
            logger.error(f"Cache set error: {e}")
            return False
    
    def _remove_from_memory_cache(self, cache_key: str):
        """Удалить запись из кэша в памяти."""
        self.cache.pop(cache_key, None)
        self.cache_meta.pop(cache_key, None)
    
    def _evict_oldest_entries(self, category: str, count: int):
        """Удалить самые старые записи из указанной категории."""
        category_keys = [
            k for k in self.cache_meta.keys() 
            if self.cache_meta[k]["category"] == category
        ]
        
        # Сортируем по времени создания
        oldest_keys = sorted(
            category_keys,
            key=lambda k: self.cache_meta[k]["created_at"]
        )[:count]
        
        for key in oldest_keys:
            self._remove_from_memory_cache(key)
    
    def _record_hit(self, start_time: float):
        """Записать попадание в кэш."""
        access_time_ms = (time.time() - start_time) * 1000
        self._update_stats(hit=True, access_time_ms=access_time_ms)
    
    def _record_miss(self, start_time: float):
        """Записать промах кэша."""
        access_time_ms = (time.time() - start_time) * 1000
        self._update_stats(hit=False, access_time_ms=access_time_ms)
    
    def _update_stats(self, hit: bool, access_time_ms: float):
        """Обновить статистику кэша."""
        if hit:
            self.stats.hits += 1
        else:
            self.stats.misses += 1
        
        self.stats.total_requests += 1
        self.stats.hit_ratio = self.stats.hits / self.stats.total_requests
        
        # Отслеживаем времена доступа для вычисления среднего
        self._access_times.append(access_time_ms)
        if len(self._access_times) > 1000:  # Ограничиваем размер
            self._access_times = self._access_times[-1000:]
        
        self.stats.avg_access_time_ms = sum(self._access_times) / len(self._access_times)
        
        # Логируем медленные операции
        if access_time_ms > self.config["monitoring"]["log_slow_cache_operations_ms"]:
            logger.warning(f"Slow cache operation: {access_time_ms:.1f}ms")
    
    def invalidate_category(self, category: str) -> int:
        """Инвалидировать все записи категории."""
        logger.info(f"Invalidating cache category: {category}")
        
        try:
            if self.cache_type == "redis":
                # Ищем все ключи категории
                pattern = f"cache:{category}:*"
                keys = self.cache.keys(pattern)
                
                if keys:
                    deleted = self.cache.delete(*keys)
                    logger.info(f"Invalidated {deleted} cache entries for category '{category}'")
                    return deleted
                else:
                    return 0
            
            else:  # memory cache
                keys_to_delete = [
                    k for k in self.cache_meta.keys()
                    if self.cache_meta[k]["category"] == category
                ]
                
                for key in keys_to_delete:
                    self._remove_from_memory_cache(key)
                
                logger.info(f"Invalidated {len(keys_to_delete)} cache entries for category '{category}'")
                return len(keys_to_delete)
                
        except Exception as e:
            logger.error(f"Cache invalidation error: {e}")
            return 0
    
    def clear_expired(self) -> int:
        """Очистить истекшие записи (только для memory cache)."""
        if self.cache_type != "memory":
            return 0
        
        expired_keys = []
        now = datetime.now()
        
        for key, meta in self.cache_meta.items():
            if meta["expires_at"] <= now:
                expired_keys.append(key)
        
        for key in expired_keys:
            self._remove_from_memory_cache(key)
        
        if expired_keys:
            logger.info(f"Cleared {len(expired_keys)} expired cache entries")
        
        return len(expired_keys)
    
    def get_stats(self) -> CacheStats:
        """Получить статистику кэша."""
        # Обновляем размер и количество записей
        if self.cache_type == "redis":
            try:
                info = self.cache.info()
                self.stats.total_size_mb = info.get("used_memory", 0) / (1024 * 1024)
                self.stats.entries_count = info.get("db0", {}).get("keys", 0)
            except:
                pass
        else:  # memory cache
            import sys
            total_size = sum(sys.getsizeof(obj) for obj in self.cache.values())
            total_size += sum(sys.getsizeof(obj) for obj in self.cache_meta.values())
            self.stats.total_size_mb = total_size / (1024 * 1024)
            self.stats.entries_count = len(self.cache)
        
        return self.stats
    
    def health_check(self) -> Dict[str, Any]:
        """Проверить здоровье кэша."""
        health = {
            "cache_type": self.cache_type,
            "is_healthy": True,
            "error": None,
            "connection_test": False
        }
        
        try:
            if self.cache_type == "redis":
                # Тестируем соединение
                self.cache.ping()
                health["connection_test"] = True
                
                # Проверяем базовые операции
                test_key = "cache:health_check:test"
                self.cache.setex(test_key, 10, b"test_value")
                result = self.cache.get(test_key)
                self.cache.delete(test_key)
                
                if result != b"test_value":
                    raise Exception("Cache read/write test failed")
            
            else:  # memory cache
                health["connection_test"] = True
                # Память всегда доступна
        
        except Exception as e:
            health["is_healthy"] = False
            health["error"] = str(e)
            logger.error(f"Cache health check failed: {e}")
        
        return health


def cached_query(category: str, ttl_seconds: Optional[int] = None):
    """Декоратор для кэширования результатов функций."""
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args, **kwargs):
            # Получаем глобальный экземпляр кэша
            cache_manager = get_global_cache()
            
            # Создаем ключ из имени функции и параметров
            func_name = func.__name__
            params_str = str(args) + str(sorted(kwargs.items()))
            
            # Пробуем получить из кэша
            cached_result = cache_manager.get(category, func_name, (params_str,))
            if cached_result is not None:
                return cached_result
            
            # Выполняем функцию и кэшируем результат
            result = func(*args, **kwargs)
            cache_manager.set(category, func_name, result, (params_str,), ttl_seconds)
            
            return result
        
        return wrapper
    return decorator


# Глобальный экземпляр кэша
_cache_manager: Optional[QueryCacheManager] = None


def get_global_cache() -> QueryCacheManager:
    """Получить глобальный экземпляр кэша."""
    global _cache_manager
    
    if _cache_manager is None:
        _cache_manager = QueryCacheManager()
    
    return _cache_manager


def main():
    """CLI для управления кэшем."""
    parser = argparse.ArgumentParser(description="Query Cache Manager")
    
    parser.add_argument(
        'command',
        choices=['stats', 'clear', 'invalidate', 'health', 'test'],
        help='Command to execute'
    )
    
    parser.add_argument('--category', help='Cache category for invalidation')
    parser.add_argument('--config', default='performance/cache_config.json')
    
    args = parser.parse_args()
    
    cache_mgr = QueryCacheManager(args.config)
    
    if args.command == 'stats':
        stats = cache_mgr.get_stats()
        
        print(f"\n=== Cache Statistics ===")
        print(f"Cache Type: {cache_mgr.cache_type}")
        print(f"Total Requests: {stats.total_requests}")
        print(f"Hits: {stats.hits}")
        print(f"Misses: {stats.misses}")
        print(f"Hit Ratio: {stats.hit_ratio:.2%}")
        print(f"Entries Count: {stats.entries_count}")
        print(f"Total Size: {stats.total_size_mb:.2f} MB")
        print(f"Avg Access Time: {stats.avg_access_time_ms:.2f}ms")
    
    elif args.command == 'clear':
        if cache_mgr.cache_type == "redis":
            cleared = cache_mgr.cache.flushdb()
            print(f"Redis cache cleared: {cleared}")
        else:
            count = len(cache_mgr.cache)
            cache_mgr.cache.clear()
            cache_mgr.cache_meta.clear()
            print(f"Memory cache cleared: {count} entries")
    
    elif args.command == 'invalidate':
        if not args.category:
            print("Category is required for invalidation")
            return
        
        invalidated = cache_mgr.invalidate_category(args.category)
        print(f"Invalidated {invalidated} entries for category '{args.category}'")
    
    elif args.command == 'health':
        health = cache_mgr.health_check()
        
        print(f"\n=== Cache Health ===")
        print(f"Type: {health['cache_type']}")
        print(f"Healthy: {'✅' if health['is_healthy'] else '❌'}")
        print(f"Connection: {'✅' if health['connection_test'] else '❌'}")
        
        if health['error']:
            print(f"Error: {health['error']}")
    
    elif args.command == 'test':
        print(f"\n=== Testing Cache ===")
        
        # Тестируем базовые операции
        test_data = {"test": "data", "number": 42}
        
        # Сохраняем в кэш
        success = cache_mgr.set("test", "SELECT 1", test_data, ttl_seconds=60)
        print(f"Set operation: {'✅' if success else '❌'}")
        
        # Читаем из кэша
        cached_data = cache_mgr.get("test", "SELECT 1")
        hit = cached_data == test_data
        print(f"Get operation: {'✅' if hit else '❌'}")
        
        # Тестируем инвалидацию
        invalidated = cache_mgr.invalidate_category("test")
        print(f"Invalidation: {'✅' if invalidated > 0 else '❌'}")
        
        # Проверяем, что данные удалены
        cached_data_after = cache_mgr.get("test", "SELECT 1")
        cleared = cached_data_after is None
        print(f"Clear verification: {'✅' if cleared else '❌'}")


if __name__ == '__main__':
    main() 