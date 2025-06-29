#!/usr/bin/env python3
"""
Connection Pool для оптимизации подключений к базам данных.

Управляет пулом соединений для повышения производительности
при большом количестве операций с БД.
"""

import os
import time
import logging
import threading
import psycopg2
import psycopg2.pool
import psycopg2.extras
from contextlib import contextmanager
from typing import Dict, Optional, Any
from dataclasses import dataclass
from queue import Queue, Empty
import json
from collections import deque

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class PoolStats:
    """Статистика пула соединений."""
    total_connections: int
    active_connections: int
    idle_connections: int
    total_requests: int
    failed_requests: int
    avg_wait_time_ms: float
    max_wait_time_ms: float


class DatabaseConnectionPool:
    """Улучшенный пул соединений с мониторингом."""
    
    def __init__(self, config_path: str = "performance/pool_config.json"):
        self.config = self._load_config(config_path)
        self.pools: Dict[str, psycopg2.pool.ThreadedConnectionPool] = {}
        self.stats: Dict[str, PoolStats] = {}
        self._locks: Dict[str, threading.Lock] = {}
        self._request_times: Dict[str, Queue] = {} 
        self._initialize_pools()
    
    def _load_config(self, config_path: str) -> Dict:
        """Загрузить конфигурацию пула."""
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return json.load(f)
        
        # Default configuration
        default_config = {
            "database": {
                "primary": {
                    "host": "10.0.0.136",
                    "port": 5432,
                    "database": "mcrm_project",
                    "user": "postgres",
                    "password": "123456",
                    "minconn": 5,
                    "maxconn": 20
                },
                "replica": {
                    "host": "10.0.0.136",  # Read replica
                    "port": 5432,
                    "database": "mcrm_project",
                    "user": "postgres",
                    "password": "123456",
                    "minconn": 3,
                    "maxconn": 15
                }
            },
            "performance": {
                "connection_timeout": 30,
                "query_timeout": 60,
                "slow_query_threshold_ms": 1000,
                "batch_size": 1000,
                "enable_query_cache": True,
                "max_idle_time": 300
            },
            "monitoring": {
                "stats_window_size": 1000,  # Store last 1000 queries
                "log_slow_queries": True,
                "enable_connection_pooling_stats": True
            }
        }
        
        # Create directory and save configuration
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(default_config, f, indent=2)
        
        return default_config
    
    def _initialize_pools(self):
        """Инициализировать пулы соединений."""
        logger.info("Initializing database connection pools")
        
        for pool_name, pool_config in self.config["database"].items():
            if not pool_config.get("enabled", True):
                logger.info(f"Pool {pool_name} is disabled, skipping")
                continue
            
            try:
                # Создаем пул соединений
                pool = psycopg2.pool.ThreadedConnectionPool(
                    minconn=pool_config["minconn"],
                    maxconn=pool_config["maxconn"],
                    host=pool_config["host"],
                    port=pool_config["port"],
                    database=pool_config["database"],
                    user=pool_config["user"],
                    password=pool_config["password"],
                    connect_timeout=self.config["performance"]["connection_timeout"],
                    # Дополнительные настройки для производительности
                    options="-c statement_timeout=30000",  # 30 секунд на запрос
                    application_name=f"DataGripProjects_{pool_name}"
                )
                
                self.pools[pool_name] = pool
                self._locks[pool_name] = threading.Lock()
                self._request_times[pool_name] = Queue(
                    maxsize=self.config["monitoring"]["stats_window_size"]
                )
                
                # Инициализируем статистику
                self.stats[pool_name] = PoolStats(
                    total_connections=pool_config["maxconn"],
                    active_connections=0,
                    idle_connections=pool_config["minconn"],
                    total_requests=0,
                    failed_requests=0,
                    avg_wait_time_ms=0.0,
                    max_wait_time_ms=0.0
                )
                
                logger.info(
                    f"Pool '{pool_name}' initialized: "
                    f"{pool_config['minconn']}-{pool_config['maxconn']} connections "
                    f"to {pool_config['host']}:{pool_config['port']}/{pool_config['database']}"
                )
                
                # Additional performance settings
                for conn in pool._pool:
                    if conn:
                        with conn.cursor() as cursor:
                            # Set connection-level optimizations
                            cursor.execute("SET work_mem = '256MB'")
                            cursor.execute("SET shared_buffers = '512MB'") 
                            cursor.execute("SET effective_cache_size = '2GB'")
                            cursor.execute("SET maintenance_work_mem = '512MB'")
                        conn.commit()
                
            except Exception as e:
                logger.error(f"Failed to initialize pool '{pool_name}': {e}")
    
    @contextmanager
    def get_connection(self, pool_name: str = "primary", readonly: bool = False):
        """
        Получить соединение из пула.
        
        Args:
            pool_name: Имя пула
            readonly: Если True, попытаться использовать реплику для чтения
        """
        # Автоматический выбор пула для чтения
        if readonly and f"{pool_name}_replica" in self.pools:
            pool_name = f"{pool_name}_replica"
        
        if pool_name not in self.pools:
            raise ValueError(f"Pool '{pool_name}' not found")
        
        pool = self.pools[pool_name]
        conn = None
        start_time = time.time()
        
        try:
            # Получаем соединение из пула
            conn = pool.getconn()
            
            if conn is None:
                raise Exception(f"Failed to get connection from pool '{pool_name}'")
            
            wait_time_ms = (time.time() - start_time) * 1000
            
            # Обновляем статистику
            self._update_stats(pool_name, wait_time_ms, success=True)
            
            # Логируем медленные получения соединений
            if wait_time_ms > self.config["monitoring"]["log_slow_queries_ms"]:
                logger.warning(f"Slow connection acquisition: {wait_time_ms:.1f}ms for pool '{pool_name}'")
            
            # Устанавливаем read-only режим если нужно
            if readonly:
                with conn.cursor() as cursor:
                    cursor.execute("SET default_transaction_read_only = on")
            
            yield conn
            
        except Exception as e:
            wait_time_ms = (time.time() - start_time) * 1000
            self._update_stats(pool_name, wait_time_ms, success=False)
            logger.error(f"Error getting connection from pool '{pool_name}': {e}")
            raise
            
        finally:
            # Возвращаем соединение в пул
            if conn is not None:
                try:
                    # Сбрасываем read-only режим
                    if readonly:
                        with conn.cursor() as cursor:
                            cursor.execute("SET default_transaction_read_only = off")
                    
                    # Откатываем незавершенные транзакции
                    if not conn.closed:
                        conn.rollback()
                    
                    pool.putconn(conn)
                    
                except Exception as e:
                    logger.error(f"Error returning connection to pool '{pool_name}': {e}")
    
    def _update_stats(self, pool_name: str, wait_time_ms: float, success: bool):
        """Обновить статистику пула."""
        if pool_name not in self.stats:
            return
        
        with self._locks[pool_name]:
            stats = self.stats[pool_name]
            
            # Обновляем счетчики
            stats.total_requests += 1
            if not success:
                stats.failed_requests += 1
            
            # Обновляем времена ожидания
            if stats.max_wait_time_ms < wait_time_ms:
                stats.max_wait_time_ms = wait_time_ms
            
            # Добавляем время в очередь для вычисления среднего
            request_times = self._request_times[pool_name]
            
            try:
                # Если очередь полная, удаляем старое значение
                if request_times.full():
                    request_times.get_nowait()
                
                request_times.put_nowait(wait_time_ms)
                
                # Вычисляем среднее время
                times_list = []
                temp_queue = Queue()
                
                while not request_times.empty():
                    try:
                        time_val = request_times.get_nowait()
                        times_list.append(time_val)
                        temp_queue.put_nowait(time_val)
                    except Empty:
                        break
                
                # Возвращаем значения обратно в очередь
                while not temp_queue.empty():
                    try:
                        request_times.put_nowait(temp_queue.get_nowait())
                    except Empty:
                        break
                
                if times_list:
                    stats.avg_wait_time_ms = sum(times_list) / len(times_list)
                
            except Exception as e:
                logger.debug(f"Error updating stats: {e}")
    
    def get_pool_stats(self, pool_name: str = None) -> Dict[str, PoolStats]:
        """Получить статистику пулов."""
        if pool_name:
            if pool_name in self.stats:
                return {pool_name: self.stats[pool_name]}
            else:
                return {}
        
        # Обновляем текущие статистики соединений
        for name, pool in self.pools.items():
            if name in self.stats:
                # Получаем текущее количество соединений
                # Это приблизительная оценка, так как ThreadedConnectionPool не предоставляет точных метрик
                self.stats[name].active_connections = max(0, 
                    self.stats[name].total_connections - len(pool._pool)
                )
                self.stats[name].idle_connections = len(pool._pool)
        
        return dict(self.stats)
    
    def execute_query(self, query: str, params: tuple = None, 
                     pool_name: str = "primary", readonly: bool = False,
                     fetch: str = "all") -> Any:
        """
        Выполнить запрос с использованием пула соединений.
        
        Args:
            query: SQL запрос
            params: Параметры запроса
            pool_name: Имя пула соединений
            readonly: Запрос только для чтения
            fetch: Тип выборки ('all', 'one', 'many', 'none')
        """
        start_time = time.time()
        
        with self.get_connection(pool_name, readonly) as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute(query, params)
                
                query_time_ms = (time.time() - start_time) * 1000
                
                # Логируем медленные запросы
                if query_time_ms > self.config["monitoring"]["log_slow_queries_ms"]:
                    logger.warning(f"Slow query ({query_time_ms:.1f}ms): {query[:100]}...")
                
                if fetch == "all":
                    return cursor.fetchall()
                elif fetch == "one":
                    return cursor.fetchone()
                elif fetch == "many":
                    return cursor.fetchmany()
                elif fetch == "none":
                    conn.commit()
                    return cursor.rowcount
                else:
                    raise ValueError(f"Invalid fetch type: {fetch}")
    
    def execute_batch(self, query: str, params_list: list, 
                     pool_name: str = "primary", batch_size: int = 1000) -> int:
        """
        Выполнить batch запрос для множественных операций.
        
        Args:
            query: SQL запрос (обычно INSERT/UPDATE)
            params_list: Список параметров для каждого запроса
            pool_name: Имя пула соединений  
            batch_size: Размер батча
        """
        total_affected = 0
        
        with self.get_connection(pool_name, readonly=False) as conn:
            with conn.cursor() as cursor:
                # Выполняем батчами для лучшей производительности
                for i in range(0, len(params_list), batch_size):
                    batch_params = params_list[i:i + batch_size]
                    
                    try:
                        psycopg2.extras.execute_batch(
                            cursor, query, batch_params, page_size=batch_size
                        )
                        conn.commit()
                        total_affected += len(batch_params)
                        
                    except Exception as e:
                        conn.rollback()
                        logger.error(f"Batch execution failed at batch {i//batch_size}: {e}")
                        raise
        
        return total_affected
    
    def health_check(self) -> Dict[str, bool]:
        """Проверить здоровье всех пулов."""
        health_status = {}
        
        for pool_name in self.pools:
            try:
                with self.get_connection(pool_name, readonly=True) as conn:
                    with conn.cursor() as cursor:
                        cursor.execute("SELECT 1")
                        cursor.fetchone()
                        
                health_status[pool_name] = True
                
            except Exception as e:
                logger.error(f"Health check failed for pool '{pool_name}': {e}")
                health_status[pool_name] = False
        
        return health_status
    
    def close_all_pools(self):
        """Закрыть все пулы соединений."""
        logger.info("Closing all connection pools")
        
        for pool_name, pool in self.pools.items():
            try:
                pool.closeall()
                logger.info(f"Pool '{pool_name}' closed")
            except Exception as e:
                logger.error(f"Error closing pool '{pool_name}': {e}")
        
        self.pools.clear()
        self.stats.clear()
        self._locks.clear()


# Глобальный экземпляр пула для переиспользования
_connection_pool: Optional[DatabaseConnectionPool] = None


def get_global_pool() -> DatabaseConnectionPool:
    """Получить глобальный экземпляр пула соединений."""
    global _connection_pool
    
    if _connection_pool is None:
        _connection_pool = DatabaseConnectionPool()
    
    return _connection_pool


@contextmanager
def get_db_connection(pool_name: str = "primary", readonly: bool = False):
    """Утилита для получения соединения из глобального пула."""
    pool = get_global_pool()
    
    with pool.get_connection(pool_name, readonly) as conn:
        yield conn


def execute_query(query: str, params: tuple = None, **kwargs) -> Any:
    """Утилита для выполнения запроса через глобальный пул.""" 
    pool = get_global_pool()
    return pool.execute_query(query, params, **kwargs)


def main():
    """CLI для управления пулом соединений."""
    import argparse
    
    parser = argparse.ArgumentParser(description="Database Connection Pool Manager")
    parser.add_argument('command', choices=['stats', 'health', 'test'])
    parser.add_argument('--pool', help='Specific pool name')
    parser.add_argument('--config', default='performance/pool_config.json')
    
    args = parser.parse_args()
    
    pool_manager = DatabaseConnectionPool(args.config)
    
    if args.command == 'stats':
        stats = pool_manager.get_pool_stats(args.pool)
        
        print("\n=== Connection Pool Statistics ===")
        for pool_name, pool_stats in stats.items():
            print(f"\nPool: {pool_name}")
            print(f"  Total Connections: {pool_stats.total_connections}")
            print(f"  Active Connections: {pool_stats.active_connections}")
            print(f"  Idle Connections: {pool_stats.idle_connections}")
            print(f"  Total Requests: {pool_stats.total_requests}")
            print(f"  Failed Requests: {pool_stats.failed_requests}")
            print(f"  Avg Wait Time: {pool_stats.avg_wait_time_ms:.2f}ms")
            print(f"  Max Wait Time: {pool_stats.max_wait_time_ms:.2f}ms")
            
            if pool_stats.total_requests > 0:
                success_rate = ((pool_stats.total_requests - pool_stats.failed_requests) 
                              / pool_stats.total_requests) * 100
                print(f"  Success Rate: {success_rate:.1f}%")
    
    elif args.command == 'health':
        health = pool_manager.health_check()
        
        print("\n=== Pool Health Check ===")
        for pool_name, is_healthy in health.items():
            status = "✅ Healthy" if is_healthy else "❌ Unhealthy"
            print(f"{pool_name}: {status}")
    
    elif args.command == 'test':
        print("\n=== Testing Connection Pool ===")
        
        # Тестируем выполнение простого запроса
        try:
            result = pool_manager.execute_query(
                "SELECT current_database(), current_user, now()",
                readonly=True
            )
            
            if result:
                row = result[0]
                print(f"✅ Test successful:")
                print(f"  Database: {row['current_database']}")
                print(f"  User: {row['current_user']}")
                print(f"  Timestamp: {row['now']}")
            
        except Exception as e:
            print(f"❌ Test failed: {e}")
    
    # Закрываем пулы при выходе
    pool_manager.close_all_pools()


if __name__ == '__main__':
    main() 