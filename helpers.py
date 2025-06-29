"""
Общие утилиты для проекта DataGripProjects.

Этот модуль содержит переиспользуемые функции для:
- Подключения к базам данных
- Обработки GTIN/EAN кодов  
- Очистки и нормализации адресов
- Работы с большими файлами (chunking)
- Утилит для DataFrame операций
"""

import os
import re
import logging
import pandas as pd
import psycopg2
import psycopg2.extras
from contextlib import contextmanager
from typing import List, Dict, Optional, Generator, Any, Tuple
from tqdm import tqdm
import shutil

# Настройка логирования
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


class DatabaseConfig:
    """Database configuration constants"""
    DEFAULT_HOST = "10.0.0.136"
    DEFAULT_PORT = 5432
    DEFAULT_DATABASE = "mcrm_project"
    DEFAULT_USER = "postgres"
    DEFAULT_PASSWORD = "123456"
    
    # Alternative settings for testing
    TEST_DATABASE = "test_mcrm"


@contextmanager
def get_db_connection(config: Optional[DatabaseConfig] = None):
    """
    Контекстный менеджер для безопасного подключения к БД.
    
    Args:
        config: Конфигурация БД. Если None, создается из переменных окружения.
        
    Yields:
        psycopg2.connection: Подключение к базе данных
        
    Example:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM table")
            results = cursor.fetchall()
    """
    if config is None:
        config = DatabaseConfig()
    
    conn = None
    try:
        conn = psycopg2.connect(
            dbname=config.DEFAULT_DATABASE,
            user=config.DEFAULT_USER,
            password=config.DEFAULT_PASSWORD,
            host=config.DEFAULT_HOST,
            port=config.DEFAULT_PORT
        )
        logger.info(f"Connected to database {config.DEFAULT_DATABASE} on {config.DEFAULT_HOST}")
        yield conn
    except psycopg2.Error as e:
        logger.error(f"Database error: {e}")
        if conn:
            conn.rollback()
        raise
    except Exception as e:
        logger.error(f"Unexpected error: {e}")
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            conn.close()
            logger.info("Database connection closed")


def execute_query(conn: psycopg2.extensions.connection, 
                 query: str, 
                 params: Optional[tuple] = None) -> List[tuple]:
    """
    Выполнить SQL запрос и вернуть результаты.
    
    Args:
        conn: Подключение к базе данных
        query: SQL запрос
        params: Параметры для запроса
        
    Returns:
        List[tuple]: Результаты запроса
    """
    with conn.cursor() as cursor:
        cursor.execute(query, params)
        if query.strip().upper().startswith('SELECT'):
            return cursor.fetchall()
        else:
            conn.commit()
            return []


def execute_batch_update(conn: psycopg2.extensions.connection, 
                        query: str, 
                        data: List[tuple], 
                        chunk_size: int = 1000) -> int:
    """
    Выполнить batch UPDATE/INSERT операции.
    
    Args:
        conn: Подключение к базе данных
        query: SQL запрос с плейсхолдерами
        data: Список кортежей с данными
        chunk_size: Размер чанка для batch операций
        
    Returns:
        int: Количество обработанных записей
    """
    total_updated = 0
    
    for i in tqdm(range(0, len(data), chunk_size), desc="Updating database"):
        chunk = data[i:i + chunk_size]
        with conn.cursor() as cursor:
            psycopg2.extras.execute_batch(cursor, query, chunk)
            conn.commit()
            total_updated += len(chunk)
    
    logger.info(f"Successfully updated {total_updated} records")
    return total_updated


def chunk_list(lst: List[Any], chunk_size: int) -> Generator[List[Any], None, None]:
    """
    Разделить список на чанки заданного размера.
    
    Args:
        lst: Исходный список
        chunk_size: Размер чанка
        
    Yields:
        List[Any]: Чанк данных
    """
    for i in range(0, len(lst), chunk_size):
        yield lst[i:i + chunk_size]


def clean_address(address: str) -> str:
    """
    Очистить и нормализовать российский адрес.
    
    Args:
        address: Исходный адрес
        
    Returns:
        str: Очищенный адрес
    """
    if not address:
        return ''

    # List of pharmacy chains to remove
    pharmacy_chains = [
        'максавит', 'ригла', 'аптека №', 'аптека низких цен', 'старый лекарь',
        'асна', 'надежда', 'первая помощь', 'озерки', 'вита', 'живика',
        'планета здоровья', 'радуга', 'столички', 'фармкопейка', 'доктор столетов',
        'губернские аптеки', 'эвалар', 'неофарм', 'здравсити', 'аптечный склад',
        'фарм-альянс', 'панацея', 'аптека+', 'здравушка', 'социальная аптека',
        'аптека от склада', 'мелодия здоровья', 'фармация', 'ремедиум',
        'фармленд', 'благодея', 'будь здоров', 'калина', 'медикер',
        'норма', 'аптечная сеть', 'аптечный пункт'
    ]

    # Regular expressions for cleaning
    patterns = {
        'whitespace': re.compile(r'\s+'),
        'postal_code': re.compile(r'^\d{6}\s*'),
        'apartment': re.compile(r'[,\s]*(?:кв|квартира|к|комната)[\s\.]*\d+.*$', re.IGNORECASE),
        'office': re.compile(r'[,\s]*(?:оф|офис|помещение|пом)[\s\.]*\d+.*$', re.IGNORECASE),
        'region_normalize': re.compile(r'\b(?:обл|область|г|город|с|село|д|деревня|п|поселок|респ|республика)\b\.?', re.IGNORECASE)
    }
    
    # Remove pharmacy chain names
    address_lower = address.lower()
    for chain in pharmacy_chains:
        if chain in address_lower:
            address = re.sub(re.escape(chain), '', address, flags=re.IGNORECASE)
    
    # Remove postal codes at the beginning
    address = patterns['postal_code'].sub('', address)
    
    # Remove "Аптека" at the beginning
    address = re.sub(r'^аптека\s*', '', address, flags=re.IGNORECASE)
    
    # Normalize region abbreviations
    address = patterns['region_normalize'].sub(lambda m: {
        'обл': 'область', 'г': 'город', 'с': 'село', 'д': 'деревня', 
        'п': 'поселок', 'респ': 'республика'
    }.get(m.group().lower().rstrip('.'), m.group()), address)

    # Remove apartments, offices, floors
    address = patterns['apartment'].sub('', address)
    address = patterns['office'].sub('', address)
    
    # Remove unnecessary symbols
    address = re.sub(r'[,;]+$', '', address)  # Commas at the end
    address = re.sub(r'^[,;\s]+', '', address)  # Commas at the beginning
    address = re.sub(r'[,;]{2,}', ',', address)  # Multiple commas
    
    # Normalize abbreviations
    abbreviations = {
        'ул': 'улица', 'пр': 'проспект', 'пер': 'переулок', 'пл': 'площадь',
        'б-р': 'бульвар', 'наб': 'набережная', 'ш': 'шоссе', 'тракт': 'тракт'
    }
    
    # Final cleanup
    address = patterns['whitespace'].sub(' ', address)
    address = address.strip()

    return address


def check_broken_address(address: str) -> bool:
    """
    Проверить, является ли адрес "сломанным".
    
    Args:
        address: Адрес для проверки
        
    Returns:
        bool: True если адрес сломан
    """
    if not address:
        return True
    
    # No digits
    if not re.search(r'\d', address):
        return True
        
    # Only whitespace
    if re.search(r'^\s*$', address):
        return True
        
    # Only digits
    if re.search(r'^\d+$', address):
        return True
        
    return False


def normalize_gtin(gtin: str) -> str:
    """
    Нормализовать GTIN код.
    
    Args:
        gtin: GTIN код
        
    Returns:
        str: Нормализованный GTIN
    """
    if not gtin:
        return ''
    
    # Remove everything except digits
    gtin = re.sub(r'\D', '', gtin)
    
    # Pad to 14 characters with leading zeros
    gtin = gtin.zfill(14)

    return gtin


def search_gtin_in_database(gtin_list: List[str], 
                           config: Optional[DatabaseConfig] = None) -> Tuple[set, set]:
    """
    Поиск GTIN кодов в базе данных.
    
    Args:
        gtin_list: Список GTIN кодов для поиска
        config: Конфигурация БД
        
    Returns:
        Tuple[set, set]: (найденные GTIN, отсутствующие GTIN)
    """
    if not gtin_list:
        return set(), set()
    
    with get_db_connection(config) as conn:
        # Split into chunks for efficiency
        chunk_size = 100
        all_found_gtins = set()
        
        for chunk in chunk_list(gtin_list, chunk_size):
            placeholders = ','.join(['%s'] * len(chunk))
            query = f'SELECT "GTIN" FROM "ManufSKUs" WHERE "GTIN" = ANY(ARRAY[{placeholders}])'
            
            results = execute_query(conn, query, chunk)
            
            # Process results (GTIN can be a space-separated list)
            for row in results:
                gtin_field = row[0]
                if gtin_field:
                    # Handle cases where GTIN contains multiple codes
                    gtin_codes = gtin_field.split()
                    for code in gtin_codes:
                        normalized_code = normalize_gtin(code)
                        all_found_gtins.add(normalized_code)
    
    gtin_set = set(gtin_list)
    found_gtins = gtin_set.intersection(all_found_gtins)
    missing_gtins = gtin_set - all_found_gtins
    
    logger.info(f"Found {len(found_gtins)} GTINs, missing {len(missing_gtins)} GTINs")
    
    return found_gtins, missing_gtins


def read_file_chunked(file_path: str, 
                     chunk_size: int = 10**6, 
                     **kwargs) -> Generator[pd.DataFrame, None, None]:
    """
    Читать большой CSV файл по частям.
    
    Args:
        file_path: Путь к файлу
        chunk_size: Размер чанка
        **kwargs: Дополнительные параметры для pd.read_csv
        
    Yields:
        pd.DataFrame: Чанк данных
    """
    try:
        for chunk in pd.read_csv(file_path, chunksize=chunk_size, **kwargs):
            yield chunk
    except Exception as e:
        logger.error(f"Error reading file {file_path}: {e}")
        raise


def save_dataframe_safe(df: pd.DataFrame, 
                       file_path: str, 
                       backup: bool = True,
                       **kwargs) -> bool:
    """
    Безопасно сохранить DataFrame с возможностью создания backup.
    
    Args:
        df: DataFrame для сохранения
        file_path: Путь для сохранения
        backup: Создавать ли backup существующего файла
        **kwargs: Дополнительные параметры для метода сохранения
    """
    # Create backup if file exists
    if backup and os.path.exists(file_path):
        backup_file = f"{file_path}.backup"
        if os.path.exists(backup_file):
            os.remove(backup_file)
        shutil.copy2(file_path, backup_file)
        logger.info(f"Created backup: {backup_file}")
    
    try:
        # Determine format by extension
        if file_path.endswith('.xlsx'):
            df.to_excel(file_path, index=False, engine='openpyxl')
        elif file_path.endswith('.csv'):
            df.to_csv(file_path, index=False, encoding='utf-8')
        else:
            # Default to CSV
            df.to_csv(file_path, index=False, encoding='utf-8')
        
        logger.info(f"File saved successfully: {file_path}")
        
        # Remove backup after successful save
        backup_file = f"{file_path}.backup"
        if os.path.exists(backup_file):
            os.remove(backup_file)
        
        return True
        
    except Exception as e:
        logger.error(f"Error saving file {file_path}: {e}")
        # Restore backup on error
        backup_file = f"{file_path}.backup"
        if os.path.exists(backup_file):
            shutil.move(backup_file, file_path)
            logger.info(f"Restored backup: {file_path}")
        return False


def validate_dataframe(df: pd.DataFrame, 
                      required_columns: List[str],
                      min_rows: int = 1) -> bool:
    """
    Валидация DataFrame.
    
    Args:
        df: DataFrame для валидации
        required_columns: Обязательные колонки
        min_rows: Минимальное количество строк
        
    Returns:
        bool: True если валидация прошла успешно
        
    Raises:
        ValueError: При ошибке валидации
    """
    if df is None:
        raise ValueError("DataFrame is None")
    
    if len(df) < min_rows:
        raise ValueError(f"DataFrame has {len(df)} rows, expected at least {min_rows}")
    
    missing_columns = set(required_columns) - set(df.columns)
    if missing_columns:
        raise ValueError(f"Missing required columns: {missing_columns}")
    
    logger.info(f"DataFrame validation passed: {len(df)} rows, {len(df.columns)} columns")
    return True


# Константы для удобства
DEFAULT_CHUNK_SIZE = 1000
DEFAULT_FILE_CHUNK_SIZE = 10**6 