"""
Тесты для модуля helpers.py.

Проверяем корректность работы критичных функций:
- Очистка адресов
- Валидация GTIN
- Работа с БД (mock тесты)
- Chunking функции
"""

import pytest
import pandas as pd
import os
from unittest.mock import Mock, patch, MagicMock
from contextlib import contextmanager

import sys
from pathlib import Path

# Добавляем путь к модулю helpers
sys.path.append(str(Path(__file__).parent.parent))

from helpers import (
    clean_address,
    check_broken_address,
    normalize_gtin,
    chunk_list,
    validate_dataframe,
    DatabaseConfig,
    get_db_connection,
    execute_query
)


class TestAddressCleaning:
    """Тесты для функций очистки адресов."""
    
    def test_clean_address_pharmacy_removal(self):
        """Тест удаления названий аптек."""
        # Тест удаления аптеки с номером
        address = "Аптека Медикс №123, ул. Пушкина, д. 10"
        result = clean_address(address)
        assert "Медикс" not in result
        assert "№123" not in result
        assert "ул. Пушкина, д 10" in result
    
    def test_clean_address_index_removal(self):
        """Тест удаления индексов."""
        address = "123456, Москва, ул. Ленина, д. 5"
        result = clean_address(address)
        assert not result.startswith("123456")
        assert "Москва" in result
    
    def test_clean_address_region_normalization(self):
        """Тест нормализации регионов."""
        test_cases = [
            ("москва обл, город", "Московская обл., город"),
            ("спб, улица", "г. Санкт-Петербург, улица"),
            ("краснодар обл, район", "Краснодарский край, район")
        ]
        
        for input_addr, expected in test_cases:
            result = clean_address(input_addr)
            assert expected in result
    
    def test_clean_address_empty_input(self):
        """Тест обработки пустых значений."""
        assert clean_address("") == ""
        assert clean_address(None) == ""
    
    def test_clean_address_office_removal(self):
        """Тест удаления офисов и помещений."""
        test_cases = [
            "ул. Ленина, д. 5, оф. 123",
            "ул. Ленина, д. 5, помещение 45",
            "ул. Ленина, д. 5, 2 этаж"
        ]
        
        for address in test_cases:
            result = clean_address(address)
            assert "оф" not in result
            assert "помещение" not in result
            assert "этаж" not in result
            assert "ул. Ленина, д 5" in result


class TestAddressValidation:
    """Тесты для валидации адресов."""
    
    def test_check_broken_address_empty(self):
        """Тест проверки пустых адресов."""
        assert check_broken_address("") is True
        assert check_broken_address("   ") is True
        assert check_broken_address(None) is True
    
    def test_check_broken_address_no_digits(self):
        """Тест проверки адресов без цифр."""
        assert check_broken_address("улица без номера") is True
    
    def test_check_broken_address_only_digits(self):
        """Тест проверки адресов только из цифр."""
        assert check_broken_address("12345") is True
    
    def test_check_broken_address_valid(self):
        """Тест проверки валидных адресов."""
        valid_addresses = [
            "ул. Ленина, д 5",
            "Москва, проспект Мира, 123",
            "г. Санкт-Петербург, ул. Невский, д 10"
        ]
        
        for address in valid_addresses:
            assert check_broken_address(address) is False


class TestGTINProcessing:
    """Тесты для обработки GTIN кодов."""
    
    def test_normalize_gtin_valid(self):
        """Тест нормализации валидных GTIN."""
        test_cases = [
            ("1234567890123", "01234567890123"),
            ("12345678901234", "12345678901234"),
            ("123", "00000000000123")
        ]
        
        for input_gtin, expected in test_cases:
            result = normalize_gtin(input_gtin)
            assert result == expected
            assert len(result) == 14
    
    def test_normalize_gtin_with_non_digits(self):
        """Тест нормализации GTIN с нецифровыми символами."""
        result = normalize_gtin("123-456-789-0123")
        assert result == "01234567890123"
    
    def test_normalize_gtin_empty(self):
        """Тест обработки пустых GTIN."""
        assert normalize_gtin("") == ""
        assert normalize_gtin(None) == ""


class TestChunkingFunctions:
    """Тесты для функций разделения на чанки."""
    
    def test_chunk_list_basic(self):
        """Тест базового разделения списка."""
        data = list(range(10))
        chunks = list(chunk_list(data, 3))
        
        assert len(chunks) == 4
        assert chunks[0] == [0, 1, 2]
        assert chunks[1] == [3, 4, 5]
        assert chunks[2] == [6, 7, 8]
        assert chunks[3] == [9]
    
    def test_chunk_list_exact_division(self):
        """Тест разделения с точным делением."""
        data = list(range(9))
        chunks = list(chunk_list(data, 3))
        
        assert len(chunks) == 3
        assert all(len(chunk) == 3 for chunk in chunks)
    
    def test_chunk_list_empty(self):
        """Тест разделения пустого списка."""
        chunks = list(chunk_list([], 5))
        assert len(chunks) == 0
    
    def test_chunk_list_single_element(self):
        """Тест разделения одного элемента."""
        chunks = list(chunk_list([42], 5))
        assert len(chunks) == 1
        assert chunks[0] == [42]


class TestDataFrameValidation:
    """Тесты для валидации DataFrame."""
    
    def test_validate_dataframe_success(self):
        """Тест успешной валидации."""
        df = pd.DataFrame({
            'col1': [1, 2, 3],
            'col2': ['a', 'b', 'c']
        })
        
        # Должен пройти без исключений
        result = validate_dataframe(df, ['col1', 'col2'], min_rows=2)
        assert result is True
    
    def test_validate_dataframe_missing_columns(self):
        """Тест валидации с отсутствующими колонками."""
        df = pd.DataFrame({'col1': [1, 2, 3]})
        
        with pytest.raises(ValueError, match="Missing required columns"):
            validate_dataframe(df, ['col1', 'col2'])
    
    def test_validate_dataframe_insufficient_rows(self):
        """Тест валидации с недостаточным количеством строк."""
        df = pd.DataFrame({'col1': [1]})
        
        with pytest.raises(ValueError, match="has 1 rows, expected at least 5"):
            validate_dataframe(df, ['col1'], min_rows=5)
    
    def test_validate_dataframe_none_input(self):
        """Тест валидации None DataFrame."""
        with pytest.raises(ValueError, match="DataFrame is None"):
            validate_dataframe(None, ['col1'])


class TestDatabaseConfig:
    """Тесты для конфигурации базы данных."""
    
    def test_database_config_with_env_vars(self):
        """Тест создания конфигурации с переменными окружения."""
        with patch.dict(os.environ, {
            'DB_PASSWORD': 'test_password',
            'DB_HOST': 'test_host',
            'DB_NAME': 'test_db'
        }):
            config = DatabaseConfig()
            params = config.get_connection_params()
            
            assert params['password'] == 'test_password'
            assert params['host'] == 'test_host'
            assert params['dbname'] == 'test_db'
    
    def test_database_config_missing_password(self):
        """Тест создания конфигурации без пароля."""
        with patch.dict(os.environ, {}, clear=True):
            with pytest.raises(ValueError, match="DB_PASSWORD environment variable is required"):
                DatabaseConfig()
    
    def test_database_config_defaults(self):
        """Тест значений по умолчанию."""
        with patch.dict(os.environ, {'DB_PASSWORD': 'test_pass'}):
            config = DatabaseConfig()
            params = config.get_connection_params()
            
            assert params['dbname'] == 'pharmhub'
            assert params['user'] == 'ivan.anisimov'
            assert params['port'] == '5432'


class TestDatabaseConnection:
    """Тесты для подключения к базе данных (с mock)."""
    
    @patch('helpers.psycopg2.connect')
    def test_get_db_connection_success(self, mock_connect):
        """Тест успешного подключения к БД."""
        mock_conn = Mock()
        mock_connect.return_value = mock_conn
        
        with patch.dict(os.environ, {'DB_PASSWORD': 'test_pass'}):
            with get_db_connection() as conn:
                assert conn == mock_conn
                mock_connect.assert_called_once()
        
        mock_conn.close.assert_called_once()
    
    @patch('helpers.psycopg2.connect')
    def test_get_db_connection_error(self, mock_connect):
        """Тест обработки ошибки подключения."""
        from psycopg2 import Error as PsycopgError
        mock_connect.side_effect = PsycopgError("Connection failed")
        
        with patch.dict(os.environ, {'DB_PASSWORD': 'test_pass'}):
            with pytest.raises(PsycopgError):
                with get_db_connection() as conn:
                    pass
    
    @patch('helpers.psycopg2.connect')
    def test_execute_query_select(self, mock_connect):
        """Тест выполнения SELECT запроса."""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_cursor.fetchall.return_value = [('row1',), ('row2',)]
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        result = execute_query(mock_conn, "SELECT * FROM table")
        
        assert result == [('row1',), ('row2',)]
        mock_cursor.execute.assert_called_with("SELECT * FROM table", None)
        mock_cursor.fetchall.assert_called_once()
    
    @patch('helpers.psycopg2.connect')  
    def test_execute_query_update(self, mock_connect):
        """Тест выполнения UPDATE запроса."""
        mock_conn = Mock()
        mock_cursor = Mock()
        mock_conn.cursor.return_value.__enter__.return_value = mock_cursor
        
        result = execute_query(mock_conn, "UPDATE table SET col=1")
        
        assert result == []
        mock_cursor.execute.assert_called_with("UPDATE table SET col=1", None)
        mock_conn.commit.assert_called_once()


class TestIntegration:
    """Интеграционные тесты."""
    
    def test_address_cleaning_pipeline(self):
        """Тест полного пайплайна очистки адресов."""
        test_addresses = [
            "123456, Аптека Медикс №123, ул. Пушкина, д. 10, оф. 5",
            "спб, улица Невская, дом 15",
            "только текст без цифр",
            "",
            "12345"
        ]
        
        results = []
        for addr in test_addresses:
            cleaned = clean_address(addr)
            is_broken = check_broken_address(cleaned)
            results.append({
                'original': addr,
                'cleaned': cleaned,
                'is_broken': is_broken
            })
        
        # Проверяем ожидаемые результаты
        assert "Медикс" not in results[0]['cleaned']
        assert "123456" not in results[0]['cleaned']
        assert "оф" not in results[0]['cleaned']
        
        assert "г. Санкт-Петербург" in results[1]['cleaned']
        
        assert results[2]['is_broken'] is True  # нет цифр
        assert results[3]['is_broken'] is True  # пустой
        assert results[4]['is_broken'] is True  # только цифры


if __name__ == '__main__':
    pytest.main([__file__, '-v']) 