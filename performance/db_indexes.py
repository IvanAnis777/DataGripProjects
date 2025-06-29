#!/usr/bin/env python3
"""
Система управления индексами базы данных для оптимизации производительности.

Автоматически создает, анализирует и поддерживает индексы
для быстрого поиска GTIN кодов и других часто используемых запросов.
"""

import os
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass
import argparse

# Импорт системы connection pool
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent))
from connection_pool import get_global_pool, DatabaseConnectionPool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class IndexInfo:
    """Информация об индексе."""
    schema_name: str
    table_name: str
    index_name: str
    columns: List[str]
    index_type: str  # btree, gin, gist, hash
    is_unique: bool
    is_partial: bool
    condition: Optional[str]
    size_mb: float
    usage_count: int
    last_used: Optional[datetime]
    creation_reason: str


@dataclass
class QueryPattern:
    """Паттерн запроса для анализа."""
    table_name: str
    columns: List[str]
    conditions: List[str]
    frequency: int
    avg_execution_time_ms: float
    recommended_index: Optional[str] = None


class DatabaseIndexManager:
    """Менеджер индексов базы данных."""
    
    def __init__(self, config_path: str = "performance/index_config.json"):
        self.config = self._load_config(config_path)
        self.pool = get_global_pool()
        self.existing_indexes: Dict[str, IndexInfo] = {}
        self.query_patterns: List[QueryPattern] = []
    
    def _load_config(self, config_path: str) -> Dict:
        """Загрузить конфигурацию индексов."""
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                return json.load(f)
        
        # Конфигурация по умолчанию
        default_config = {
            "recommended_indexes": [
                {
                    "name": "idx_gtin_search",
                    "table": "products",
                    "columns": ["gtin"],
                    "type": "btree",
                    "reason": "Быстрый поиск по GTIN кодам"
                },
                {
                    "name": "idx_product_name_search",
                    "table": "products",
                    "columns": ["name"],
                    "type": "gin",
                    "reason": "Полнотекстовый поиск по названиям"
                },
                {
                    "name": "idx_address_normalized",
                    "table": "addresses",
                    "columns": ["normalized_address"],
                    "type": "btree",
                    "reason": "Поиск по нормализованным адресам"
                }
            ],
            "monitoring": {
                "min_usage_count": 100,  # Минимальное использование для сохранения индекса
                "max_unused_days": 30,   # Удалять неиспользуемые индексы через N дней
                "analyze_frequency_hours": 24,  # Частота анализа статистики
                "size_threshold_mb": 100  # Предупреждать о больших индексах
            },
            "maintenance": {
                "reindex_frequency_days": 7,  # Переиндексация каждые N дней
                "vacuum_after_reindex": True,
                "analyze_after_reindex": True
            }
        }
        
        # Создаем директорию и сохраняем конфигурацию
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, 'w') as f:
            json.dump(default_config, f, indent=2)
        
        return default_config
    
    def scan_existing_indexes(self) -> Dict[str, IndexInfo]:
        """Сканировать существующие индексы."""
        logger.info("Scanning existing database indexes")
        
        query = """
        SELECT 
            schemaname,
            tablename,
            indexname,
            indexdef,
            pg_size_pretty(pg_relation_size(indexname::regclass)) as size,
            pg_relation_size(indexname::regclass) as size_bytes,
            idx_tup_read as usage_count,
            idx_tup_fetch,
            CASE 
                WHEN indisunique THEN true 
                ELSE false 
            END as is_unique,
            CASE 
                WHEN indpred IS NOT NULL THEN true 
                ELSE false 
            END as is_partial,
            indpred as condition
        FROM pg_indexes
        JOIN pg_stat_user_indexes ON pg_stat_user_indexes.indexrelname = pg_indexes.indexname
        JOIN pg_index ON pg_index.indexrelid = pg_stat_user_indexes.indexrelid
        WHERE schemaname = 'public'
        ORDER BY size_bytes DESC
        """
        
        try:
            results = self.pool.execute_query(query, readonly=True)
            indexes = {}
            
            for row in results:
                # Извлекаем колонки из определения индекса
                columns = self._extract_columns_from_indexdef(row['indexdef'])
                index_type = self._extract_index_type(row['indexdef'])
                
                index_info = IndexInfo(
                    schema_name=row['schemaname'],
                    table_name=row['tablename'],
                    index_name=row['indexname'],
                    columns=columns,
                    index_type=index_type,
                    is_unique=row['is_unique'],
                    is_partial=row['is_partial'],
                    condition=row['condition'],
                    size_mb=row['size_bytes'] / (1024 * 1024) if row['size_bytes'] else 0,
                    usage_count=row['usage_count'] or 0,
                    last_used=None,  # Эта информация требует дополнительного запроса
                    creation_reason="Existing index"
                )
                
                indexes[row['indexname']] = index_info
            
            self.existing_indexes = indexes
            logger.info(f"Found {len(indexes)} existing indexes")
            
            return indexes
            
        except Exception as e:
            logger.error(f"Failed to scan existing indexes: {e}")
            return {}
    
    def _extract_columns_from_indexdef(self, indexdef: str) -> List[str]:
        """Извлечь колонки из определения индекса."""
        try:
            # Ищем содержимое в скобках после ON table_name
            import re
            
            # Паттерн для извлечения колонок: ON table_name (columns)
            pattern = r'ON\s+\w+\s*\(([^)]+)\)'
            match = re.search(pattern, indexdef, re.IGNORECASE)
            
            if match:
                columns_str = match.group(1)
                # Убираем функции и извлекаем только имена колонок
                columns = []
                for col in columns_str.split(','):
                    col = col.strip()
                    # Убираем функции типа lower(), trim() и т.д.
                    col_clean = re.sub(r'\w+\s*\(([^)]+)\)', r'\1', col)
                    col_clean = col_clean.strip('"').strip("'").strip()
                    if col_clean:
                        columns.append(col_clean)
                
                return columns
                
        except Exception as e:
            logger.debug(f"Failed to extract columns from indexdef: {e}")
        
        return ["unknown"]
    
    def _extract_index_type(self, indexdef: str) -> str:
        """Извлечь тип индекса из определения."""
        indexdef_lower = indexdef.lower()
        
        if 'using gin' in indexdef_lower:
            return 'gin'
        elif 'using gist' in indexdef_lower:
            return 'gist'
        elif 'using hash' in indexdef_lower:
            return 'hash'
        elif 'using btree' in indexdef_lower or 'using' not in indexdef_lower:
            return 'btree'
        else:
            return 'unknown'
    
    def create_recommended_indexes(self, dry_run: bool = False) -> List[str]:
        """Создать рекомендуемые индексы."""
        logger.info(f"Creating recommended indexes (dry_run={dry_run})")
        
        created_indexes = []
        
        for index_config in self.config["recommended_indexes"]:
            index_name = index_config["name"]
            
            # Проверяем, существует ли уже такой индекс
            if index_name in self.existing_indexes:
                logger.info(f"Index {index_name} already exists, skipping")
                continue
            
            # Генерируем SQL для создания индекса
            create_sql = self._generate_create_index_sql(index_config)
            
            if dry_run:
                logger.info(f"Would create index: {create_sql}")
                created_indexes.append(index_name)
                continue
            
            try:
                start_time = time.time()
                
                logger.info(f"Creating index: {index_name}")
                self.pool.execute_query(create_sql, fetch="none")
                
                creation_time = time.time() - start_time
                logger.info(f"Index {index_name} created successfully in {creation_time:.2f}s")
                
                created_indexes.append(index_name)
                
            except Exception as e:
                logger.error(f"Failed to create index {index_name}: {e}")
        
        # Обновляем список существующих индексов
        if not dry_run and created_indexes:
            self.scan_existing_indexes()
        
        return created_indexes
    
    def _generate_create_index_sql(self, index_config: Dict) -> str:
        """Генерировать SQL для создания индекса."""
        index_name = index_config["name"]
        table_name = index_config["table"]
        columns = ", ".join(index_config["columns"])
        index_type = index_config.get("type", "btree")
        
        if index_type == "gin":
            # Для полнотекстового поиска
            if "name" in index_config["columns"]:
                expression = f"to_tsvector('russian', {index_config['columns'][0]})"
                return f"CREATE INDEX {index_name} ON {table_name} USING gin ({expression})"
        
        return f"CREATE INDEX {index_name} ON {table_name} ({columns})"
    
    def analyze_query_patterns(self) -> List[QueryPattern]:
        """Анализировать паттерны запросов для рекомендаций индексов."""
        logger.info("Analyzing query patterns from pg_stat_statements")
        
        # Проверяем, доступно ли расширение pg_stat_statements
        extension_check = """
        SELECT EXISTS (
            SELECT 1 FROM pg_extension WHERE extname = 'pg_stat_statements'
        ) as has_pg_stat_statements
        """
        
        try:
            result = self.pool.execute_query(extension_check, readonly=True)
            if not result[0]['has_pg_stat_statements']:
                logger.warning("pg_stat_statements extension not available, skipping query pattern analysis")
                return []
            
        except Exception as e:
            logger.error(f"Failed to check pg_stat_statements: {e}")
            return []
        
        # Анализируем медленные и частые запросы
        query = """
        SELECT 
            query,
            calls,
            total_time,
            mean_time,
            rows,
            100.0 * shared_blks_hit / nullif(shared_blks_hit + shared_blks_read, 0) AS hit_percent
        FROM pg_stat_statements
        WHERE query NOT LIKE '%pg_stat_statements%'
            AND query NOT LIKE '%information_schema%'
            AND calls > 10
        ORDER BY mean_time DESC
        LIMIT 50
        """
        
        try:
            results = self.pool.execute_query(query, readonly=True)
            patterns = []
            
            for row in results:
                # Простой анализ - ищем SELECT запросы с WHERE условиями
                query_text = row['query'].lower()
                
                if 'select' in query_text and 'where' in query_text:
                    # Пытаемся извлечь информацию о таблице и условиях
                    table_name = self._extract_table_from_query(query_text)
                    conditions = self._extract_conditions_from_query(query_text)
                    
                    if table_name and conditions:
                        pattern = QueryPattern(
                            table_name=table_name,
                            columns=conditions,
                            conditions=conditions,
                            frequency=row['calls'],
                            avg_execution_time_ms=row['mean_time']
                        )
                        patterns.append(pattern)
            
            self.query_patterns = patterns
            logger.info(f"Analyzed {len(patterns)} query patterns")
            
            return patterns
            
        except Exception as e:
            logger.error(f"Failed to analyze query patterns: {e}")
            return []
    
    def _extract_table_from_query(self, query: str) -> Optional[str]:
        """Извлечь имя таблицы из запроса."""
        import re
        
        # Простой паттерн для извлечения таблицы из FROM
        pattern = r'from\s+(\w+)'
        match = re.search(pattern, query)
        
        return match.group(1) if match else None
    
    def _extract_conditions_from_query(self, query: str) -> List[str]:
        """Извлечь условия WHERE из запроса."""
        import re
        
        # Ищем условия вида column = ? или column IN (?)
        patterns = [
            r'(\w+)\s*=\s*\$\d+',  # column = $1
            r'(\w+)\s*=\s*\?',     # column = ?
            r'(\w+)\s+in\s*\(',    # column IN (
            r'(\w+)\s+like\s+',    # column LIKE
            r'(\w+)\s*>\s*',       # column >
            r'(\w+)\s*<\s*',       # column <
        ]
        
        conditions = []
        for pattern in patterns:
            matches = re.findall(pattern, query, re.IGNORECASE)
            conditions.extend(matches)
        
        return list(set(conditions))  # Убираем дубликаты
    
    def suggest_missing_indexes(self) -> List[Dict]:
        """Предложить недостающие индексы на основе анализа."""
        suggestions = []
        
        # Анализируем паттерны запросов
        if not self.query_patterns:
            self.analyze_query_patterns()
        
        for pattern in self.query_patterns:
            # Ищем медленные запросы без подходящих индексов
            if (pattern.avg_execution_time_ms > 100 and  # Медленнее 100мс
                pattern.frequency > 50):  # Часто выполняемые
                
                # Проверяем, есть ли подходящий индекс
                existing_suitable = False
                for index_name, index_info in self.existing_indexes.items():
                    if (index_info.table_name == pattern.table_name and
                        any(col in index_info.columns for col in pattern.columns)):
                        existing_suitable = True
                        break
                
                if not existing_suitable:
                    suggestion = {
                        "table": pattern.table_name,
                        "columns": pattern.columns,
                        "reason": f"Slow query pattern (avg: {pattern.avg_execution_time_ms:.1f}ms, calls: {pattern.frequency})",
                        "suggested_name": f"idx_{pattern.table_name}_{'_'.join(pattern.columns[:2])}",
                        "type": "btree",
                        "priority": "high" if pattern.avg_execution_time_ms > 1000 else "medium"
                    }
                    suggestions.append(suggestion)
        
        logger.info(f"Generated {len(suggestions)} index suggestions")
        return suggestions
    
    def maintenance_cleanup(self, dry_run: bool = False) -> Dict:
        """Выполнить обслуживание индексов."""
        logger.info(f"Starting index maintenance (dry_run={dry_run})")
        
        results = {
            "reindexed": [],
            "dropped": [],
            "analyzed": [],
            "warnings": []
        }
        
        monitoring = self.config["monitoring"]
        
        # Сканируем текущие индексы
        current_indexes = self.scan_existing_indexes()
        
        for index_name, index_info in current_indexes.items():
            # Пропускаем системные индексы
            if index_name.startswith("pg_") or "_pkey" in index_name:
                continue
            
            # Проверяем большие неиспользуемые индексы
            if (index_info.usage_count < monitoring["min_usage_count"] and
                index_info.size_mb > monitoring["size_threshold_mb"]):
                
                warning = f"Large unused index: {index_name} ({index_info.size_mb:.1f}MB, {index_info.usage_count} uses)"
                results["warnings"].append(warning)
                logger.warning(warning)
            
            # Переиндексация больших индексов
            if index_info.size_mb > monitoring["size_threshold_mb"]:
                if dry_run:
                    logger.info(f"Would reindex large index: {index_name}")
                    results["reindexed"].append(index_name)
                else:
                    try:
                        logger.info(f"Reindexing large index: {index_name}")
                        reindex_sql = f"REINDEX INDEX {index_name}"
                        self.pool.execute_query(reindex_sql, fetch="none")
                        results["reindexed"].append(index_name)
                        
                        # Анализируем таблицу после переиндексации
                        if self.config["maintenance"]["analyze_after_reindex"]:
                            analyze_sql = f"ANALYZE {index_info.table_name}"
                            self.pool.execute_query(analyze_sql, fetch="none")
                            results["analyzed"].append(index_info.table_name)
                        
                    except Exception as e:
                        logger.error(f"Failed to reindex {index_name}: {e}")
        
        return results
    
    def get_index_usage_report(self) -> Dict:
        """Получить отчет об использовании индексов."""
        query = """
        SELECT 
            schemaname,
            tablename,
            indexrelname,
            idx_tup_read,
            idx_tup_fetch,
            pg_size_pretty(pg_relation_size(indexrelid)) as index_size,
            pg_relation_size(indexrelid) as size_bytes
        FROM pg_stat_user_indexes
        WHERE schemaname = 'public'
        ORDER BY idx_tup_read DESC
        """
        
        try:
            results = self.pool.execute_query(query, readonly=True)
            
            report = {
                "total_indexes": len(results),
                "total_size_mb": 0,
                "most_used": [],
                "least_used": [],
                "unused": []
            }
            
            # Сортируем по использованию
            sorted_results = sorted(results, key=lambda x: x['idx_tup_read'] or 0, reverse=True)
            
            for row in sorted_results:
                size_mb = (row['size_bytes'] or 0) / (1024 * 1024)
                report["total_size_mb"] += size_mb
                
                index_data = {
                    "name": row['indexrelname'],
                    "table": row['tablename'],
                    "reads": row['idx_tup_read'] or 0,
                    "fetches": row['idx_tup_fetch'] or 0,
                    "size_mb": size_mb
                }
                
                if row['idx_tup_read'] and row['idx_tup_read'] > 1000:
                    report["most_used"].append(index_data)
                elif not row['idx_tup_read'] or row['idx_tup_read'] < 10:
                    report["unused"].append(index_data)
                else:
                    report["least_used"].append(index_data)
            
            # Ограничиваем количество записей в отчете
            report["most_used"] = report["most_used"][:10]
            report["least_used"] = report["least_used"][:10]
            report["unused"] = report["unused"][:20]
            
            return report
            
        except Exception as e:
            logger.error(f"Failed to generate index usage report: {e}")
            return {}


def main():
    """CLI для управления индексами."""
    parser = argparse.ArgumentParser(description="Database Index Manager")
    
    parser.add_argument(
        'command',
        choices=['scan', 'create', 'analyze', 'suggest', 'maintenance', 'report'],
        help='Command to execute'
    )
    
    parser.add_argument('--dry-run', action='store_true', help='Show what would be done')
    parser.add_argument('--config', default='performance/index_config.json')
    
    args = parser.parse_args()
    
    index_mgr = DatabaseIndexManager(args.config)
    
    if args.command == 'scan':
        indexes = index_mgr.scan_existing_indexes()
        
        print(f"\n=== Existing Indexes ({len(indexes)}) ===")
        for name, info in indexes.items():
            print(f"\n{name}:")
            print(f"  Table: {info.table_name}")
            print(f"  Columns: {', '.join(info.columns)}")
            print(f"  Type: {info.index_type}")
            print(f"  Size: {info.size_mb:.2f} MB")
            print(f"  Usage: {info.usage_count} reads")
            print(f"  Unique: {info.is_unique}")
    
    elif args.command == 'create':
        created = index_mgr.create_recommended_indexes(dry_run=args.dry_run)
        
        if args.dry_run:
            print(f"\nWould create {len(created)} indexes:")
        else:
            print(f"\nCreated {len(created)} indexes:")
        
        for index_name in created:
            print(f"  - {index_name}")
    
    elif args.command == 'analyze':
        patterns = index_mgr.analyze_query_patterns()
        
        print(f"\n=== Query Patterns ({len(patterns)}) ===")
        for pattern in patterns[:10]:  # Показываем топ-10
            print(f"\nTable: {pattern.table_name}")
            print(f"  Columns: {', '.join(pattern.columns)}")
            print(f"  Frequency: {pattern.frequency} calls")
            print(f"  Avg Time: {pattern.avg_execution_time_ms:.2f}ms")
    
    elif args.command == 'suggest':
        suggestions = index_mgr.suggest_missing_indexes()
        
        print(f"\n=== Index Suggestions ({len(suggestions)}) ===")
        for suggestion in suggestions:
            print(f"\n{suggestion['suggested_name']} ({suggestion['priority']} priority):")
            print(f"  Table: {suggestion['table']}")
            print(f"  Columns: {', '.join(suggestion['columns'])}")
            print(f"  Reason: {suggestion['reason']}")
    
    elif args.command == 'maintenance':
        results = index_mgr.maintenance_cleanup(dry_run=args.dry_run)
        
        print(f"\n=== Maintenance Results ===")
        
        if results['reindexed']:
            action = "Would reindex" if args.dry_run else "Reindexed"
            print(f"{action}: {', '.join(results['reindexed'])}")
        
        if results['dropped']:
            action = "Would drop" if args.dry_run else "Dropped"
            print(f"{action}: {', '.join(results['dropped'])}")
        
        if results['analyzed']:
            print(f"Analyzed: {', '.join(results['analyzed'])}")
        
        if results['warnings']:
            print(f"\nWarnings:")
            for warning in results['warnings']:
                print(f"  ⚠️  {warning}")
    
    elif args.command == 'report':
        report = index_mgr.get_index_usage_report()
        
        print(f"\n=== Index Usage Report ===")
        print(f"Total Indexes: {report['total_indexes']}")
        print(f"Total Size: {report['total_size_mb']:.2f} MB")
        
        print(f"\nMost Used Indexes ({len(report['most_used'])}):")
        for idx in report['most_used']:
            print(f"  {idx['name']}: {idx['reads']} reads, {idx['size_mb']:.2f} MB")
        
        print(f"\nUnused Indexes ({len(report['unused'])}):")
        for idx in report['unused'][:5]:  # Показываем только топ-5
            print(f"  {idx['name']}: {idx['reads']} reads, {idx['size_mb']:.2f} MB")


if __name__ == '__main__':
    main() 