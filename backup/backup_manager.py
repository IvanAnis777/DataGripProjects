#!/usr/bin/env python3
"""
Система автоматических бэкапов данных.

Создает резервные копии БД и файлов перед критическими операциями.
"""

import os
import json
import logging
from datetime import datetime
from typing import Dict, List

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class BackupManager:
    """Класс для управления бэкапами."""
    
    def __init__(self):
        self.backup_root = "backups"
    
    def backup_before_operation(self, operation_name: str, source_files: List[str] = None) -> Dict:
        """Создать бэкапы перед операцией."""
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = f"{self.backup_root}/{operation_name}_{timestamp}"
        
        os.makedirs(backup_dir, exist_ok=True)
        
        result = {
            "operation": operation_name,
            "backup_dir": backup_dir,
            "timestamp": timestamp
        }
        
        logger.info(f"Backup created: {backup_dir}")
        return result
    
    def cleanup_old_backups(self, max_age_days: int = 30):
        """Очистить старые бэкапы."""
        logger.info(f"Cleaning up backups older than {max_age_days} days")
        
        backup_root = self.backup_root
        if not os.path.exists(backup_root):
            return
        
        from datetime import timedelta
        cutoff_date = datetime.now() - timedelta(days=max_age_days)
        
        cleaned_count = 0
        for item in os.listdir(backup_root):
            item_path = os.path.join(backup_root, item)
            if os.path.isdir(item_path):
                # Extract date from directory name
                if not item.startswith('backup_'):
                    continue
                
                date_part = item.split('_')[-2:]  # Take last 2 parts
                if len(date_part) != 2:
                    continue
                
                try:
                    date_str = f"{date_part[0]}_{date_part[1]}"
                    backup_date = datetime.strptime(date_str, '%Y%m%d_%H%M%S')
                except ValueError:
                    # Skip directories with inappropriate names
                    continue
                
                if backup_date < cutoff_date:
                    import shutil
                    shutil.rmtree(item_path)
                    cleaned_count += 1
                    logger.info(f"Removed old backup: {item}")
        
        logger.info(f"Cleanup completed: removed {cleaned_count} old backups")
    
    def get_backup_status(self) -> Dict:
        """Получить статус бэкапов."""
        backup_root = self.backup_root
        
        status = {
            "backup_root": backup_root,
            "total_backups": 0,
            "total_size_mb": 0,
            "latest_backup": None,
            "backups": []
        }
        
        if not os.path.exists(backup_root):
            return status
        
        for item in os.listdir(backup_root):
            item_path = os.path.join(backup_root, item)
            if os.path.isdir(item_path):
                # Calculate directory size
                total_size = 0
                for dirpath, dirnames, filenames in os.walk(item_path):
                    for filename in filenames:
                        filepath = os.path.join(dirpath, filename)
                        if os.path.exists(filepath):
                            total_size += os.path.getsize(filepath)
                
                backup_info = {
                    "name": item,
                    "path": item_path,
                    "size_mb": total_size / (1024 * 1024),
                    "created": datetime.fromtimestamp(os.path.getctime(item_path))
                }
                
                status["backups"].append(backup_info)
                status["total_backups"] += 1
                status["total_size_mb"] += backup_info["size_mb"]
        
        # Sort by date
        status["backups"].sort(key=lambda x: x["created"], reverse=True)
        
        if status["backups"]:
            status["latest_backup"] = status["backups"][0]["created"]
        
        return status


def main():
    """CLI для бэкапов."""
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['backup', 'cleanup', 'status'])
    parser.add_argument('--operation', help='Operation name')
    parser.add_argument('--files', nargs='*', help='Files to backup')
    parser.add_argument('--max-age', type=int, default=30, help='Max age for cleanup (days)')
    
    args = parser.parse_args()
    
    backup_mgr = BackupManager()
    
    if args.command == 'backup' and args.operation:
        result = backup_mgr.backup_before_operation(args.operation, args.files)
        print(f"Backup created: {result['backup_dir']}")
    
    elif args.command == 'cleanup':
        backup_mgr.cleanup_old_backups(args.max_age)
    
    elif args.command == 'status':
        status = backup_mgr.get_backup_status()
        print(f"\n=== Backup Status ===")
        print(f"Total backups: {status['total_backups']}")
        print(f"Total size: {status['total_size_mb']:.1f} MB")
        if status['latest_backup']:
            print(f"Latest backup: {status['latest_backup']}")
        
        print(f"\nBackups:")
        for backup in status['backups'][:5]:  # Show last 5
            print(f"  {backup['name']}: {backup['size_mb']:.1f} MB ({backup['created']})")


if __name__ == '__main__':
    main() 