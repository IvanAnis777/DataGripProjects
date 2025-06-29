#!/usr/bin/env python3
"""
Система мониторинга соединений с базами данных.

Проверяет доступность БД, производительность запросов,
отправляет алерты при проблемах.
"""

import os
import json
import time
import logging
import threading
import smtplib
import psycopg2
import requests
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
from contextlib import contextmanager
from email.mime.text import MimeText
from email.mime.multipart import MimeMultipart
import argparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


@dataclass
class DatabaseHealth:
    """Состояние здоровья БД."""
    host: str
    port: int
    database: str
    is_available: bool
    response_time_ms: float
    active_connections: int
    max_connections: int
    connection_usage_percent: float
    last_check: datetime
    error_message: Optional[str] = None


@dataclass
class Alert:
    """Алерт о проблеме с БД."""
    timestamp: datetime
    severity: str  # critical, warning, info
    database: str
    message: str
    metric_value: Optional[float] = None
    threshold: Optional[float] = None


class DatabaseMonitor:
    """Класс для мониторинга БД."""
    
    def __init__(self, config_path: str = "monitoring/monitor_config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.alert_history: List[Alert] = []
        self.health_history: Dict[str, List[DatabaseHealth]] = {}
        self._stop_monitoring = threading.Event()
    
    def _load_config(self) -> Dict:
        """Загрузить конфигурацию мониторинга."""
        if os.path.exists(self.config_path):
            with open(self.config_path, 'r') as f:
                return json.load(f)
        
        # Default configuration
        default_config = {
            "check_interval_seconds": 60,  # Check every minute
            "databases": {
                "primary": {
                    "host": "10.0.0.136",
                    "port": 5432,
                    "database": "mcrm_project",
                    "user": "postgres",
                    "password": "123456"
                }
            },
            "thresholds": {
                "response_time_ms": 1000,  # Maximum response time
                "connection_usage_percent": 80,  # Maximum connection usage
                "availability_check_timeout": 30  # Timeout for availability check
            },
            "alerts": {
                "email": {
                    "enabled": False,
                    "smtp_server": "localhost",
                    "smtp_port": 587,
                    "username": "",
                    "password": "",
                    "from_email": "alerts@company.com",
                    "to_emails": ["admin@company.com"]
                },
                "cooldown_minutes": 10,  # Don't send repeat alerts more frequently
                "slack": {
                    "enabled": False,
                    "webhook_url": ""
                },
                "telegram": {
                    "enabled": False,
                    "bot_token": "",
                    "chat_id": ""
                }
            },
            "history": {
                "health_retention_hours": 168  # Store history for 7 days
            }
        }
        
        # Create directory and save configuration
        os.makedirs(os.path.dirname(self.config_path), exist_ok=True)
        with open(self.config_path, 'w') as f:
            json.dump(default_config, f, indent=2)
        
        return default_config
    
    @contextmanager
    def get_db_connection(self, db_config: Dict):
        """Получить соединение с БД с timeout."""
        conn = None
        try:
            timeout = self.config["thresholds"]["availability_check_timeout"]
            
            conn = psycopg2.connect(
                host=db_config["host"],
                port=db_config["port"],
                database=db_config["database"],
                user=db_config["username"],
                password=os.getenv("DB_PASSWORD"),
                connect_timeout=timeout
            )
            yield conn
            
        except Exception as e:
            logger.error(f"Database connection failed: {e}")
            raise
        finally:
            if conn:
                conn.close()
    
    def check_database_health(self, db_config: Dict) -> DatabaseHealth:
        """Проверить здоровье конкретной БД."""
        start_time = time.time()
        
        try:
            with self.get_db_connection(db_config) as conn:
                # Check simple query
                with conn.cursor() as cursor:
                    cursor.execute("SELECT 1")
                    cursor.fetchone()
                
                response_time_ms = (time.time() - start_time) * 1000
                
                # Get connection information
                with conn.cursor() as cursor:
                    cursor.execute("""
                        SELECT count(*) as total_connections,
                               sum(case when state = 'active' then 1 else 0 end) as active_connections
                        FROM pg_stat_activity
                        WHERE datname = current_database()
                    """)
                    
                    total_connections, active_connections = cursor.fetchone()
                    connection_usage_percent = (active_connections / total_connections) * 100
                
                return DatabaseHealth(
                    host=db_config["host"],
                    port=db_config["port"],
                    database=db_config["database"],
                    is_available=True,
                    response_time_ms=response_time_ms,
                    active_connections=active_connections,
                    max_connections=total_connections,
                    connection_usage_percent=connection_usage_percent,
                    last_check=datetime.now()
                )
                
        except Exception as e:
            response_time_ms = (time.time() - start_time) * 1000
            
            return DatabaseHealth(
                host=db_config["host"],
                port=db_config["port"],
                database=db_config["database"],
                is_available=False,
                response_time_ms=response_time_ms,
                active_connections=0,
                max_connections=0,
                connection_usage_percent=0,
                last_check=datetime.now(),
                error_message=str(e)
            )
    
    def analyze_health_and_generate_alerts(self, health: DatabaseHealth) -> List[Alert]:
        """Анализировать состояние БД и генерировать алерты."""
        alerts = []
        thresholds = self.config["thresholds"]
        
        # Availability check
        if not health.is_available:
            alerts.append(Alert(
                timestamp=datetime.now(),
                severity="critical",
                database=health.database,
                message=f"Database {health.database} is not available: {health.error_message}"
            ))
        
        # Response time check
        if health.is_available and health.response_time_ms > thresholds["response_time_ms"]:
            alerts.append(Alert(
                timestamp=datetime.now(),
                severity="warning",
                database=health.database,
                message=f"Database {health.database} response time is high",
                metric_value=health.response_time_ms,
                threshold=thresholds["response_time_ms"]
            ))
        
        # Connection usage check
        if health.is_available and health.connection_usage_percent > thresholds["connection_usage_percent"]:
            alerts.append(Alert(
                timestamp=datetime.now(),
                severity="warning",
                database=health.database,
                message=f"Database {health.database} connection usage is high",
                metric_value=health.connection_usage_percent,
                threshold=thresholds["connection_usage_percent"]
            ))
        
        return alerts
    
    def should_send_alert(self, alert: Alert) -> bool:
        """Проверить, нужно ли отправлять алерт (проверка cooldown)."""
        cooldown_minutes = self.config["alerts"]["cooldown_minutes"]
        cooldown_time = timedelta(minutes=cooldown_minutes)
        
        # Look for similar alerts in the last N minutes
        recent_alerts = [
            a for a in self.alert_history
            if (a.database == alert.database and 
                a.message == alert.message and
                datetime.now() - a.timestamp < cooldown_time)
        ]
        
        return len(recent_alerts) == 0
    
    def send_alert(self, alert: Alert):
        """Отправить алерт через настроенные каналы."""
        if not self.should_send_alert(alert):
            logger.debug(f"Alert cooldown active, skipping: {alert.message}")
            return
        
        self.alert_history.append(alert)
        
        # Email alerts
        if self.config["alerts"]["email"]["enabled"]:
            self._send_email_alert(alert)
        
        # Slack alerts
        if self.config["alerts"]["slack"]["enabled"]:
            self._send_slack_alert(alert)
        
        # Telegram alerts
        if self.config["alerts"]["telegram"]["enabled"]:
            self._send_telegram_alert(alert)
    
    def _send_email_alert(self, alert: Alert):
        """Отправить email алерт."""
        try:
            smtp_server = self.config["alerts"]["email"]["smtp_server"]
            smtp_port = self.config["alerts"]["email"]["smtp_port"]
            smtp_user = self.config["alerts"]["email"]["username"]
            smtp_password = self.config["alerts"]["email"]["password"]
            
            if not smtp_user:
                logger.warning("SMTP not configured, skipping email alert")
                return
            
            severity_emoji = {
                "critical": "🚨",
                "warning": "⚠️",
                "info": "ℹ️"
            }
            
            subject = f"{severity_emoji.get(alert.severity, '🔔')} Database Alert - {alert.severity.upper()}"
            
            body = f"""
Database Monitoring Alert

Severity: {alert.severity.upper()}
Database: {alert.database}
Message: {alert.message}
Timestamp: {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}
"""
            
            if alert.metric_value is not None:
                body += f"Current Value: {alert.metric_value:.2f}\n"
            
            if alert.threshold is not None:
                body += f"Threshold: {alert.threshold:.2f}\n"
            
            msg = MimeMultipart()
            msg['From'] = smtp_user
            msg['Subject'] = subject
            msg.attach(MimeText(body, 'plain'))
            
            recipients = self.config["alerts"]["email"]["to_emails"]
            if not recipients:
                recipients = [smtp_user]
            
            msg['To'] = ', '.join(recipients)
            
            with smtplib.SMTP(smtp_server, smtp_port) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)
            
            logger.info(f"Email alert sent for {alert.database}")
            
        except Exception as e:
            logger.error(f"Failed to send email alert: {e}")
    
    def _send_slack_alert(self, alert: Alert):
        """Отправить Slack алерт."""
        try:
            webhook_url = self.config["alerts"]["slack"]["webhook_url"]
            if not webhook_url:
                logger.warning("Slack webhook not configured")
                return
            
            severity_colors = {
                "critical": "#FF0000",
                "warning": "#FFAA00",
                "info": "#0000FF"
            }
            
            payload = {
                "attachments": [{
                    "color": severity_colors.get(alert.severity, "#808080"),
                    "title": f"Database Alert - {alert.severity.upper()}",
                    "fields": [
                        {"title": "Database", "value": alert.database, "short": True},
                        {"title": "Message", "value": alert.message, "short": False},
                        {"title": "Time", "value": alert.timestamp.strftime('%Y-%m-%d %H:%M:%S'), "short": True}
                    ]
                }]
            }
            
            if alert.metric_value is not None:
                payload["attachments"][0]["fields"].append({
                    "title": "Current Value", 
                    "value": f"{alert.metric_value:.2f}", 
                    "short": True
                })
            
            if alert.threshold is not None:
                payload["attachments"][0]["fields"].append({
                    "title": "Threshold", 
                    "value": f"{alert.threshold:.2f}", 
                    "short": True
                })
            
            response = requests.post(webhook_url, json=payload, timeout=10)
            response.raise_for_status()
            
            logger.info(f"Slack alert sent for {alert.database}")
            
        except Exception as e:
            logger.error(f"Failed to send Slack alert: {e}")
    
    def _send_telegram_alert(self, alert: Alert):
        """Отправить Telegram алерт."""
        try:
            bot_token = self.config["alerts"]["telegram"]["bot_token"]
            chat_id = self.config["alerts"]["telegram"]["chat_id"]
            
            if not bot_token or not chat_id:
                logger.warning("Telegram not configured")
                return
            
            severity_emoji = {
                "critical": "🚨",
                "warning": "⚠️",
                "info": "ℹ️"
            }
            
            message = f"""
{severity_emoji.get(alert.severity, '🔔')} *Database Alert*

*Severity:* {alert.severity.upper()}
*Database:* {alert.database}
*Message:* {alert.message}
*Time:* {alert.timestamp.strftime('%Y-%m-%d %H:%M:%S')}
"""
            
            if alert.metric_value is not None:
                message += f"*Current Value:* {alert.metric_value:.2f}\n"
            
            if alert.threshold is not None:
                message += f"*Threshold:* {alert.threshold:.2f}\n"
            
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "Markdown"
            }
            
            response = requests.post(url, json=payload, timeout=10)
            response.raise_for_status()
            
            logger.info(f"Telegram alert sent for {alert.database}")
            
        except Exception as e:
            logger.error(f"Failed to send Telegram alert: {e}")
    
    def save_health_history(self, health: DatabaseHealth):
        """Сохранить историю состояния БД."""
        db_name = health.database
        
        if db_name not in self.health_history:
            self.health_history[db_name] = []
        
        self.health_history[db_name].append(health)
        
        # Clean old history
        retention_hours = self.config["history"]["health_retention_hours"]
        cutoff_time = datetime.now() - timedelta(hours=retention_hours)
        
        self.health_history[db_name] = [
            h for h in self.health_history[db_name]
            if h.last_check > cutoff_time
        ]
        
        # Save to file
        history_path = f"monitoring/.health_history_{db_name}.json"
        os.makedirs(os.path.dirname(history_path), exist_ok=True)
        
        # Convert to JSON-serializable format
        history_data = []
        for h in self.health_history[db_name][-100:]:  # Last 100 records
            data = asdict(h)
            data['last_check'] = h.last_check.isoformat()
            history_data.append(data)
        
        with open(history_path, 'w') as f:
            json.dump(history_data, f, indent=2)
    
    def monitor_once(self):
        """Выполнить одну итерацию мониторинга."""
        logger.info("Starting monitoring cycle")
        
        for db_config in self.config["databases"]:
            try:
                # Check database health
                health = self.check_database_health(db_config)
                
                logger.info(
                    f"Database {health.database}: "
                    f"Available={health.is_available}, "
                    f"ResponseTime={health.response_time_ms:.1f}ms, "
                    f"ConnectionUsage={health.connection_usage_percent:.1f}%"
                )
                
                # Save history
                self.save_health_history(health)
                
                # Analyze and send alerts
                alerts = self.analyze_health_and_generate_alerts(health)
                for alert in alerts:
                    logger.warning(f"Alert generated: {alert.message}")
                    self.send_alert(alert)
                
            except Exception as e:
                logger.error(f"Error monitoring database {db_config['name']}: {e}")
        
        logger.info("Monitoring cycle completed")
    
    def start_monitoring(self):
        """Запустить непрерывный мониторинг."""
        logger.info("Starting continuous database monitoring")
        
        while not self._stop_monitoring.is_set():
            try:
                self.monitor_once()
                
                # Wait until next check
                self._stop_monitoring.wait(self.config["check_interval_seconds"])
                
            except KeyboardInterrupt:
                logger.info("Monitoring stopped by user")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                # Short pause before retry
                time.sleep(10)
    
    def stop_monitoring(self):
        """Остановить мониторинг."""
        logger.info("Stopping database monitoring")
        self._stop_monitoring.set()
    
    def get_health_report(self, hours: int = 24) -> Dict:
        """Получить отчет о здоровье БД за последние N часов."""
        cutoff_time = datetime.now() - timedelta(hours=hours)
        report = {}
        
        for db_name, history in self.health_history.items():
            recent_history = [h for h in history if h.last_check > cutoff_time]
            
            if not recent_history:
                continue
            
            # Calculate statistics
            total_checks = len(recent_history)
            available_checks = len([h for h in recent_history if h.is_available])
            availability_percent = (available_checks / total_checks) * 100
            
            response_times = [h.response_time_ms for h in recent_history if h.is_available]
            avg_response_time = sum(response_times) / len(response_times) if response_times else 0
            max_response_time = max(response_times) if response_times else 0
            
            connection_usages = [h.connection_usage_percent for h in recent_history if h.is_available]
            avg_connection_usage = sum(connection_usages) / len(connection_usages) if connection_usages else 0
            max_connection_usage = max(connection_usages) if connection_usages else 0
            
            # Recent alerts
            recent_alerts = [
                a for a in self.alert_history
                if a.database == db_name and a.timestamp > cutoff_time
            ]
            
            report[db_name] = {
                "availability_percent": availability_percent,
                "total_checks": total_checks,
                "available_checks": available_checks,
                "avg_response_time_ms": avg_response_time,
                "max_response_time_ms": max_response_time,
                "avg_connection_usage_percent": avg_connection_usage,
                "max_connection_usage_percent": max_connection_usage,
                "alerts_count": len(recent_alerts),
                "last_check": recent_history[-1].last_check.isoformat() if recent_history else None
            }
        
        return report


def main():
    """CLI для управления мониторингом БД."""
    parser = argparse.ArgumentParser(description="Database Monitoring System")
    
    parser.add_argument(
        'command',
        choices=['start', 'check', 'report', 'status'],
        help='Command to execute'
    )
    
    parser.add_argument(
        '--config',
        default='monitoring/monitor_config.json',
        help='Configuration file path'
    )
    
    parser.add_argument(
        '--hours',
        type=int,
        default=24,
        help='Hours for report generation (default: 24)'
    )
    
    args = parser.parse_args()
    
    monitor = DatabaseMonitor(args.config)
    
    if args.command == 'start':
        try:
            monitor.start_monitoring()
        except KeyboardInterrupt:
            monitor.stop_monitoring()
    
    elif args.command == 'check':
        monitor.monitor_once()
    
    elif args.command == 'report':
        report = monitor.get_health_report(args.hours)
        
        print(f"\n=== Database Health Report (Last {args.hours} hours) ===")
        for db_name, stats in report.items():
            print(f"\nDatabase: {db_name}")
            print(f"  Availability: {stats['availability_percent']:.1f}%")
            print(f"  Checks: {stats['available_checks']}/{stats['total_checks']}")
            print(f"  Avg Response Time: {stats['avg_response_time_ms']:.1f}ms")
            print(f"  Max Response Time: {stats['max_response_time_ms']:.1f}ms")
            print(f"  Avg Connection Usage: {stats['avg_connection_usage_percent']:.1f}%")
            print(f"  Max Connection Usage: {stats['max_connection_usage_percent']:.1f}%")
            print(f"  Alerts: {stats['alerts_count']}")
            print(f"  Last Check: {stats['last_check']}")
    
    elif args.command == 'status':
        print("\n=== Current Database Status ===")
        for db_config in monitor.config["databases"]:
            health = monitor.check_database_health(db_config)
            
            status = "🟢 OK" if health.is_available else "🔴 DOWN"
            print(f"\n{health.database}: {status}")
            print(f"  Host: {health.host}:{health.port}")
            print(f"  Response Time: {health.response_time_ms:.1f}ms")
            
            if health.is_available:
                print(f"  Connections: {health.active_connections}/{health.max_connections} ({health.connection_usage_percent:.1f}%)")
            else:
                print(f"  Error: {health.error_message}")


if __name__ == '__main__':
    main() 