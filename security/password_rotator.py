#!/usr/bin/env python3
"""
Система автоматической ротации паролей БД.

Безопасная ротация паролей каждые 3 месяца с уведомлениями.
"""

import os
import secrets
import string
import json
import logging
import smtplib
from datetime import datetime, timedelta
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart
from pathlib import Path
from typing import Dict, Optional
import psycopg2
from cryptography.fernet import Fernet
import argparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PasswordRotator:
    """Класс для безопасной ротации паролей БД."""
    
    def __init__(self, config_path: str = "security/rotation_config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.cipher = self._init_cipher()
    
    def _load_config(self) -> Dict:
        """Загрузить конфигурацию ротации."""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        
        # Конфигурация по умолчанию
        default_config = {
            "rotation_interval_days": 90,  # 3 месяца
            "password_length": 16,
            "password_complexity": {
                "uppercase": True,
                "lowercase": True,
                "digits": True,
                "special_chars": "!@#$%^&*"
            },
            "notification": {
                "email_enabled": True,
                "slack_enabled": False,
                "recipients": []
            },
            "databases": [
                {
                    "name": "pharmhub",
                    "host": "10.0.0.171",
                    "port": 5432,
                    "username": "ivan.anisimov",
                    "admin_username": "postgres"
                }
            ]
        }
        
        # Создаем директорию и сохраняем конфигурацию
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(default_config, f, indent=2)
        
        return default_config
    
    def _init_cipher(self) -> Fernet:
        """Инициализировать шифрование для паролей."""
        key_path = "security/.rotation_key"
        
        if os.path.exists(key_path):
            with open(key_path, 'rb') as f:
                key = f.read()
        else:
            key = Fernet.generate_key()
            os.makedirs(os.path.dirname(key_path), exist_ok=True)
            with open(key_path, 'wb') as f:
                f.write(key)
            # Устанавливаем безопасные права доступа
            os.chmod(key_path, 0o600)
        
        return Fernet(key)
    
    def generate_strong_password(self) -> str:
        """Генерировать сильный пароль."""
        config = self.config["password_complexity"]
        length = self.config["password_length"]
        
        chars = ""
        if config["lowercase"]:
            chars += string.ascii_lowercase
        if config["uppercase"]:
            chars += string.ascii_uppercase
        if config["digits"]:
            chars += string.digits
        if config["special_chars"]:
            chars += config["special_chars"]
        
        # Генерируем пароль, гарантируя наличие всех типов символов
        password = []
        
        if config["lowercase"]:
            password.append(secrets.choice(string.ascii_lowercase))
        if config["uppercase"]:
            password.append(secrets.choice(string.ascii_uppercase))
        if config["digits"]:
            password.append(secrets.choice(string.digits))
        if config["special_chars"]:
            password.append(secrets.choice(config["special_chars"]))
        
        # Дополняем до нужной длины
        for _ in range(length - len(password)):
            password.append(secrets.choice(chars))
        
        # Перемешиваем
        secrets.SystemRandom().shuffle(password)
        
        return ''.join(password)
    
    def check_password_age(self, db_config: Dict) -> bool:
        """Проверить, нужна ли ротация пароля."""
        history_path = f"security/.password_history_{db_config['name']}.json"
        
        if not os.path.exists(history_path):
            return True  # Первая ротация
        
        with open(history_path, 'r') as f:
            history = json.load(f)
        
        last_rotation = datetime.fromisoformat(history["last_rotation"])
        rotation_interval = timedelta(days=self.config["rotation_interval_days"])
        
        return datetime.now() - last_rotation >= rotation_interval
    
    def rotate_database_password(self, db_config: Dict, force: bool = False) -> bool:
        """Ротировать пароль для конкретной БД."""
        if not force and not self.check_password_age(db_config):
            logger.info(f"Password for {db_config['name']} is still fresh")
            return False
        
        logger.info(f"Starting password rotation for {db_config['name']}")
        
        try:
            # Генерируем новый пароль
            new_password = self.generate_strong_password()
            
            # Подключаемся как администратор
            admin_conn = psycopg2.connect(
                host=db_config["host"],
                port=db_config["port"],
                database=db_config["name"],
                user=db_config["admin_username"],
                password=os.getenv("DB_ADMIN_PASSWORD")
            )
            
            with admin_conn.cursor() as cursor:
                # Изменяем пароль пользователя
                cursor.execute(
                    f"ALTER USER {db_config['username']} PASSWORD %s",
                    (new_password,)
                )
                admin_conn.commit()
            
            admin_conn.close()
            
            # Тестируем новый пароль
            test_conn = psycopg2.connect(
                host=db_config["host"],
                port=db_config["port"],
                database=db_config["name"],
                user=db_config["username"],
                password=new_password
            )
            test_conn.close()
            
            # Сохраняем зашифрованный пароль и историю
            self._save_password_history(db_config["name"], new_password)
            self._update_env_file(db_config["name"], new_password)
            
            # Отправляем уведомления
            self._send_rotation_notification(db_config["name"], success=True)
            
            logger.info(f"Password rotation completed successfully for {db_config['name']}")
            return True
            
        except Exception as e:
            logger.error(f"Password rotation failed for {db_config['name']}: {e}")
            self._send_rotation_notification(db_config["name"], success=False, error=str(e))
            return False
    
    def _save_password_history(self, db_name: str, password: str):
        """Сохранить историю паролей."""
        history_path = f"security/.password_history_{db_name}.json"
        
        # Шифруем пароль
        encrypted_password = self.cipher.encrypt(password.encode()).decode()
        
        history = {
            "last_rotation": datetime.now().isoformat(),
            "encrypted_password": encrypted_password,
            "rotation_count": 1
        }
        
        if os.path.exists(history_path):
            with open(history_path, 'r') as f:
                old_history = json.load(f)
                history["rotation_count"] = old_history.get("rotation_count", 0) + 1
        
        with open(history_path, 'w') as f:
            json.dump(history, f, indent=2)
        
        # Безопасные права доступа
        os.chmod(history_path, 0o600)
    
    def _update_env_file(self, db_name: str, password: str):
        """Обновить .env файл с новым паролем."""
        env_path = ".env"
        lines = []
        
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                lines = f.readlines()
        
        # Обновляем или добавляем пароль
        password_line = f"DB_PASSWORD={password}\n"
        updated = False
        
        for i, line in enumerate(lines):
            if line.startswith("DB_PASSWORD="):
                lines[i] = password_line
                updated = True
                break
        
        if not updated:
            lines.append(password_line)
        
        with open(env_path, 'w') as f:
            f.writelines(lines)
        
        # Безопасные права доступа
        os.chmod(env_path, 0o600)
    
    def _send_rotation_notification(self, db_name: str, success: bool, error: str = None):
        """Отправить уведомление о ротации."""
        if not self.config["notification"]["email_enabled"]:
            return
        
        subject = f"Password Rotation {'Successful' if success else 'Failed'} - {db_name}"
        
        if success:
            body = f"""
Password rotation completed successfully for database: {db_name}

Rotation Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
Next Rotation: {(datetime.now() + timedelta(days=self.config['rotation_interval_days'])).strftime('%Y-%m-%d')}

Please update your applications with the new password from .env file.
            """
        else:
            body = f"""
Password rotation FAILED for database: {db_name}

Error: {error}
Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

Please check the logs and rotate manually if needed.
            """
        
        self._send_email(subject, body)
    
    def _send_email(self, subject: str, body: str):
        """Отправить email уведомление."""
        try:
            smtp_server = os.getenv("SMTP_SERVER", "localhost")
            smtp_port = int(os.getenv("SMTP_PORT", "587"))
            smtp_user = os.getenv("SMTP_USER")
            smtp_password = os.getenv("SMTP_PASSWORD")
            
            if not smtp_user:
                logger.warning("SMTP credentials not configured, skipping email notification")
                return
            
            msg = MimeMultipart()
            msg['From'] = smtp_user
            msg['Subject'] = subject
            msg.attach(MimeText(body, 'plain'))
            
            recipients = self.config["notification"]["recipients"]
            if not recipients:
                recipients = [smtp_user]  # Отправляем себе
            
            msg['To'] = ', '.join(recipients)
            
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            
            logger.info("Email notification sent successfully")
            
        except Exception as e:
            logger.error(f"Failed to send email notification: {e}")
    
    def rotate_all_passwords(self, force: bool = False) -> Dict[str, bool]:
        """Ротировать пароли для всех БД."""
        results = {}
        
        for db_config in self.config["databases"]:
            results[db_config["name"]] = self.rotate_database_password(db_config, force)
        
        return results
    
    def get_rotation_status(self) -> Dict:
        """Получить статус ротации всех БД."""
        status = {}
        
        for db_config in self.config["databases"]:
            db_name = db_config["name"]
            history_path = f"security/.password_history_{db_name}.json"
            
            if os.path.exists(history_path):
                with open(history_path, 'r') as f:
                    history = json.load(f)
                
                last_rotation = datetime.fromisoformat(history["last_rotation"])
                next_rotation = last_rotation + timedelta(days=self.config["rotation_interval_days"])
                days_until_rotation = (next_rotation - datetime.now()).days
                
                status[db_name] = {
                    "last_rotation": last_rotation.strftime('%Y-%m-%d %H:%M:%S'),
                    "next_rotation": next_rotation.strftime('%Y-%m-%d %H:%M:%S'),
                    "days_until_rotation": days_until_rotation,
                    "rotation_count": history["rotation_count"],
                    "needs_rotation": days_until_rotation <= 0
                }
            else:
                status[db_name] = {
                    "last_rotation": "Never",
                    "next_rotation": "Now",
                    "days_until_rotation": 0,
                    "rotation_count": 0,
                    "needs_rotation": True
                }
        
        return status


def main():
    """CLI для управления ротацией паролей."""
    parser = argparse.ArgumentParser(description="Database Password Rotation Manager")
    
    parser.add_argument(
        'command',
        choices=['rotate', 'status', 'force-rotate'],
        help='Command to execute'
    )
    
    parser.add_argument(
        '--database',
        help='Specific database to rotate (default: all)'
    )
    
    parser.add_argument(
        '--config',
        default='security/rotation_config.json',
        help='Configuration file path'
    )
    
    args = parser.parse_args()
    
    rotator = PasswordRotator(args.config)
    
    if args.command == 'status':
        status = rotator.get_rotation_status()
        
        print("\n=== Password Rotation Status ===")
        for db_name, info in status.items():
            print(f"\nDatabase: {db_name}")
            print(f"  Last Rotation: {info['last_rotation']}")
            print(f"  Next Rotation: {info['next_rotation']}")
            print(f"  Days Until Rotation: {info['days_until_rotation']}")
            print(f"  Rotation Count: {info['rotation_count']}")
            print(f"  Needs Rotation: {'Yes' if info['needs_rotation'] else 'No'}")
    
    elif args.command == 'rotate':
        if args.database:
            # Ротация конкретной БД
            db_config = next(
                (db for db in rotator.config["databases"] if db["name"] == args.database),
                None
            )
            if db_config:
                success = rotator.rotate_database_password(db_config)
                print(f"Rotation {'successful' if success else 'failed'} for {args.database}")
            else:
                print(f"Database {args.database} not found in configuration")
        else:
            # Ротация всех БД
            results = rotator.rotate_all_passwords()
            print("\n=== Rotation Results ===")
            for db_name, success in results.items():
                print(f"{db_name}: {'Success' if success else 'Failed'}")
    
    elif args.command == 'force-rotate':
        if args.database:
            db_config = next(
                (db for db in rotator.config["databases"] if db["name"] == args.database),
                None
            )
            if db_config:
                success = rotator.rotate_database_password(db_config, force=True)
                print(f"Force rotation {'successful' if success else 'failed'} for {args.database}")
            else:
                print(f"Database {args.database} not found in configuration")
        else:
            results = rotator.rotate_all_passwords(force=True)
            print("\n=== Force Rotation Results ===")
            for db_name, success in results.items():
                print(f"{db_name}: {'Success' if success else 'Failed'}")


if __name__ == '__main__':
    main() 