#!/usr/bin/env python3
"""
Веб-интерфейс для DataGripProjects.

Простой и удобный интерфейс для нетехнических пользователей
для работы с GTIN поиском, очисткой адресов и мониторингом.
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, Any, List
from flask import Flask, render_template, request, jsonify, redirect, url_for, flash
from werkzeug.utils import secure_filename
import pandas as pd

# Импорт наших модулей
import sys
from pathlib import Path
sys.path.append(str(Path(__file__).parent.parent))

from helpers import (
    get_db_connection, 
    search_gtin_in_database,
    clean_address,
    validate_dataframe
)
from performance.connection_pool import get_global_pool
from performance.cache_manager import get_global_cache
from monitoring.db_monitor import DatabaseMonitor
from backup.backup_manager import BackupManager

# Настройка логирования
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Создание Flask приложения
app = Flask(__name__)
app.secret_key = os.getenv('SECRET_KEY', 'dev-secret-key-change-in-production')
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024  # 50MB макс размер файла
app.config['UPLOAD_FOLDER'] = 'uploads'

# Создаем директорию для загрузок
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

# Глобальные объекты
db_monitor = DatabaseMonitor()
backup_manager = BackupManager()
cache_manager = get_global_cache()


@app.route('/')
def index():
    """Главная страница."""
    try:
        # Получаем базовую статистику для dashboard
        pool = get_global_pool()
        pool_stats = pool.get_pool_stats()
        cache_stats = cache_manager.get_stats()
        
        stats = {
            'database_health': True,
            'cache_health': True,
            'total_cache_hits': cache_stats.hits,
            'cache_hit_ratio': f"{cache_stats.hit_ratio:.1%}",
            'active_connections': sum(s.active_connections for s in pool_stats.values()),
        }
        
    except Exception as e:
        logger.error(f"Error getting stats for dashboard: {e}")
        stats = {
            'database_health': False,
            'cache_health': False,
            'error': str(e)
        }
    
    return render_template('index.html', stats=stats)


@app.route('/gtin-search')
def gtin_search_page():
    """Страница поиска GTIN."""
    return render_template('gtin_search.html')


@app.route('/api/gtin-search', methods=['POST'])
def api_gtin_search():
    """API для поиска GTIN кодов."""
    try:
        data = request.get_json()
        gtin = data.get('gtin', '').strip()
        
        if not gtin:
            return jsonify({'error': 'GTIN код не указан'}), 400
        
        # Ищем в базе данных
        results = search_gtin_in_database(gtin)
        
        if results:
            return jsonify({
                'success': True,
                'results': results,
                'total_found': len(results)
            })
        else:
            return jsonify({
                'success': False,
                'message': 'GTIN код не найден в базе данных',
                'total_found': 0
            })
            
    except Exception as e:
        logger.error(f"Error in GTIN search: {e}")
        return jsonify({'error': f'Ошибка поиска: {str(e)}'}), 500


@app.route('/address-cleaner')
def address_cleaner_page():
    """Страница очистки адресов."""
    return render_template('address_cleaner.html')


@app.route('/api/clean-address', methods=['POST'])
def api_clean_address():
    """API для очистки отдельного адреса."""
    try:
        data = request.get_json()
        address = data.get('address', '').strip()
        
        if not address:
            return jsonify({'error': 'Адрес не указан'}), 400
        
        # Очищаем адрес
        cleaned = clean_address(address)
        
        return jsonify({
            'success': True,
            'original': address,
            'cleaned': cleaned,
            'changes_made': address != cleaned
        })
        
    except Exception as e:
        logger.error(f"Error cleaning address: {e}")
        return jsonify({'error': f'Ошибка очистки: {str(e)}'}), 500


@app.route('/file-upload')
def file_upload_page():
    """Страница загрузки файлов."""
    return render_template('file_upload.html')


@app.route('/api/upload-file', methods=['POST'])
def api_upload_file():
    """API для загрузки и обработки файлов."""
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'Файл не выбран'}), 400
        
        file = request.files['file']
        operation_type = request.form.get('operation_type', 'gtin_search')
        
        if file.filename == '':
            return jsonify({'error': 'Файл не выбран'}), 400
        
        if not file.filename.lower().endswith(('.csv', '.xlsx', '.xls')):
            return jsonify({'error': 'Поддерживаются только CSV и Excel файлы'}), 400
        
        # Сохраняем файл
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        safe_filename = f"{timestamp}_{filename}"
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
        file.save(file_path)
        
        # Создаем бэкап перед обработкой
        backup_info = backup_manager.backup_before_operation(
            f"file_upload_{operation_type}",
            [file_path]
        )
        
        # Читаем файл
        if filename.lower().endswith('.csv'):
            df = pd.read_csv(file_path)
        else:
            df = pd.read_excel(file_path)
        
        # Валидируем данные
        validation_result = validate_dataframe(df)
        if not validation_result['is_valid']:
            return jsonify({
                'error': f'Ошибка валидации файла: {validation_result["error"]}'
            }), 400
        
        # Обрабатываем в зависимости от типа операции
        if operation_type == 'gtin_search':
            results = process_gtin_file(df)
        elif operation_type == 'address_cleaning':
            results = process_address_file(df)
        else:
            return jsonify({'error': 'Неизвестный тип операции'}), 400
        
        # Сохраняем результат
        result_filename = f"result_{safe_filename}"
        result_path = os.path.join(app.config['UPLOAD_FOLDER'], result_filename)
        results['dataframe'].to_excel(result_path, index=False)
        
        return jsonify({
            'success': True,
            'message': 'Файл успешно обработан',
            'backup_id': backup_info.get('timestamp', ''),
            'result_file': result_filename,
            'statistics': results['stats'],
            'download_url': f'/api/download/{result_filename}'
        })
        
    except Exception as e:
        logger.error(f"Error processing file upload: {e}")
        return jsonify({'error': f'Ошибка обработки файла: {str(e)}'}), 500


def process_gtin_file(df: pd.DataFrame) -> Dict[str, Any]:
    """Обработать файл с GTIN кодами."""
    results = []
    stats = {'total': len(df), 'found': 0, 'not_found': 0}
    
    # Ищем колонку с GTIN
    gtin_column = None
    for col in df.columns:
        if 'gtin' in col.lower() or 'код' in col.lower() or 'barcode' in col.lower():
            gtin_column = col
            break
    
    if not gtin_column:
        # Берем первую колонку
        gtin_column = df.columns[0]
    
    # Обрабатываем каждый GTIN
    for idx, row in df.iterrows():
        gtin = str(row[gtin_column]).strip()
        
        if gtin and gtin != 'nan':
            found_products = search_gtin_in_database(gtin)
            
            if found_products:
                stats['found'] += 1
                df.at[idx, 'Статус'] = 'Найден'
                df.at[idx, 'Количество_найденных'] = len(found_products)
                df.at[idx, 'Названия_товаров'] = '; '.join([p.get('name', '') for p in found_products[:3]])
            else:
                stats['not_found'] += 1
                df.at[idx, 'Статус'] = 'Не найден'
                df.at[idx, 'Количество_найденных'] = 0
                df.at[idx, 'Названия_товаров'] = ''
        else:
            df.at[idx, 'Статус'] = 'Пустое значение'
    
    return {
        'dataframe': df,
        'stats': stats
    }


def process_address_file(df: pd.DataFrame) -> Dict[str, Any]:
    """Обработать файл с адресами."""
    stats = {'total': len(df), 'cleaned': 0, 'unchanged': 0}
    
    # Ищем колонку с адресами
    address_column = None
    for col in df.columns:
        if 'адрес' in col.lower() or 'address' in col.lower() or 'улица' in col.lower():
            address_column = col
            break
    
    if not address_column:
        address_column = df.columns[0]
    
    # Очищаем каждый адрес
    for idx, row in df.iterrows():
        original_address = str(row[address_column]).strip()
        
        if original_address and original_address != 'nan':
            cleaned_address = clean_address(original_address)
            
            df.at[idx, 'Очищенный_адрес'] = cleaned_address
            
            if original_address != cleaned_address:
                stats['cleaned'] += 1
                df.at[idx, 'Изменения'] = 'Да'
            else:
                stats['unchanged'] += 1
                df.at[idx, 'Изменения'] = 'Нет'
        else:
            df.at[idx, 'Очищенный_адрес'] = ''
            df.at[idx, 'Изменения'] = 'Пустое значение'
    
    return {
        'dataframe': df,
        'stats': stats
    }


@app.route('/api/download/<filename>')
def api_download_file(filename):
    """API для скачивания обработанных файлов."""
    try:
        safe_filename = secure_filename(filename)
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], safe_filename)
        
        if not os.path.exists(file_path):
            return jsonify({'error': 'Файл не найден'}), 404
        
        from flask import send_file
        return send_file(file_path, as_attachment=True, download_name=filename)
        
    except Exception as e:
        logger.error(f"Error downloading file: {e}")
        return jsonify({'error': f'Ошибка скачивания: {str(e)}'}), 500


@app.route('/monitoring')
def monitoring_page():
    """Страница мониторинга."""
    try:
        # Получаем статистику различных компонентов
        pool_stats = get_global_pool().get_pool_stats()
        cache_stats = cache_manager.get_stats()
        
        # Проверяем здоровье системы
        db_health = get_global_pool().health_check()
        cache_health = cache_manager.health_check()
        
        monitoring_data = {
            'database': {
                'health': db_health,
                'pool_stats': {name: {
                    'total_connections': stats.total_connections,
                    'active_connections': stats.active_connections,
                    'total_requests': stats.total_requests,
                    'hit_ratio': f"{((stats.total_requests - stats.failed_requests) / max(stats.total_requests, 1)):.1%}",
                    'avg_wait_time': f"{stats.avg_wait_time_ms:.1f}ms"
                } for name, stats in pool_stats.items()}
            },
            'cache': {
                'health': cache_health,
                'stats': {
                    'type': cache_manager.cache_type,
                    'hits': cache_stats.hits,
                    'misses': cache_stats.misses,
                    'hit_ratio': f"{cache_stats.hit_ratio:.1%}",
                    'entries_count': cache_stats.entries_count,
                    'size_mb': f"{cache_stats.total_size_mb:.2f} MB",
                    'avg_access_time': f"{cache_stats.avg_access_time_ms:.2f}ms"
                }
            }
        }
        
        return render_template('monitoring.html', monitoring=monitoring_data)
        
    except Exception as e:
        logger.error(f"Error getting monitoring data: {e}")
        return render_template('monitoring.html', monitoring={'error': str(e)})


@app.route('/api/monitoring/status')
def api_monitoring_status():
    """API для получения статуса мониторинга."""
    try:
        # Быстрая проверка статуса
        pool = get_global_pool()
        
        status = {
            'timestamp': datetime.now().isoformat(),
            'database_healthy': all(pool.health_check().values()),
            'cache_healthy': cache_manager.health_check()['is_healthy'],
            'uptime': 'OK'  # Можно добавить реальный uptime
        }
        
        return jsonify(status)
        
    except Exception as e:
        logger.error(f"Error getting status: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/settings')
def settings_page():
    """Страница настроек."""
    return render_template('settings.html')


@app.route('/api/clear-cache', methods=['POST'])
def api_clear_cache():
    """API для очистки кэша."""
    try:
        data = request.get_json()
        category = data.get('category', 'all')
        
        if category == 'all':
            if cache_manager.cache_type == "redis":
                cleared = cache_manager.cache.flushdb()
            else:
                count = len(cache_manager.cache)
                cache_manager.cache.clear()
                cache_manager.cache_meta.clear()
                cleared = count
            
            message = f"Очищен весь кэш: {cleared} записей"
        else:
            cleared = cache_manager.invalidate_category(category)
            message = f"Очищена категория '{category}': {cleared} записей"
        
        return jsonify({
            'success': True,
            'message': message,
            'cleared_count': cleared
        })
        
    except Exception as e:
        logger.error(f"Error clearing cache: {e}")
        return jsonify({'error': f'Ошибка очистки кэша: {str(e)}'}), 500


@app.errorhandler(404)
def not_found_error(error):
    """Обработчик ошибки 404."""
    return render_template('error.html', 
                         error_code=404, 
                         error_message="Страница не найдена"), 404


@app.errorhandler(500)
def internal_error(error):
    """Обработчик ошибки 500."""
    return render_template('error.html',
                         error_code=500,
                         error_message="Внутренняя ошибка сервера"), 500


if __name__ == '__main__':
    # Запуск в режиме разработки
    app.run(
        host='0.0.0.0',
        port=int(os.getenv('WEB_PORT', 5000)),
        debug=os.getenv('FLASK_ENV') == 'development'
    ) 