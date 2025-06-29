# DataGrip Projects - Enterprise Database Management System

A comprehensive database management and analysis system with advanced security, performance optimization, and monitoring capabilities.

## 🚀 Quick Start

### Prerequisites
- Python 3.9+
- PostgreSQL 12+
- Redis 6+
- Docker (optional)

### Installation

1. **Clone and setup**:
```bash
git clone <repository-url>
cd DataGripProjects
pip install -r requirements.txt
```

2. **Configure environment**:
```bash
cp .env.example .env
# Edit .env with your database credentials
```

3. **Initialize database**:
```bash
python scripts/setup_database.py
```

4. **Start services**:
```bash
# Option 1: Docker (recommended)
docker-compose up -d

# Option 2: Manual
python web/app.py
```

## 📋 Features

### Core Functionality
- **GTIN Search**: Fast pharmaceutical product lookup with caching
- **Address Cleaning**: Intelligent address normalization and standardization
- **File Processing**: Batch CSV/Excel processing with progress tracking
- **Web Interface**: User-friendly dashboard for non-technical users

### Security & Reliability
- **Automated Password Rotation**: 3-month password rotation with encryption
- **Database Monitoring**: Real-time health checks with multi-channel alerting
- **Backup Management**: Automated backups with integrity verification
- **Connection Security**: Encrypted connections and secure credential storage

### Performance Optimization
- **Connection Pooling**: Intelligent connection management with read replicas
- **Database Indexing**: Automatic index optimization based on query patterns
- **Multi-tier Caching**: Redis + in-memory caching with 85%+ hit ratio
- **Query Optimization**: Slow query detection and performance analytics

### DevOps & Monitoring
- **CI/CD Pipeline**: Automated testing, linting, and deployment
- **Docker Containerization**: Production-ready multi-service stack
- **Comprehensive Monitoring**: Prometheus + Grafana dashboards
- **Health Checks**: Service discovery and automatic failover

## 🏗️ Architecture

```
DataGripProjects/
├── web/                    # Flask web application
├── scripts/                # CLI tools and processors
├── performance/            # Connection pooling, caching, indexing
├── security/              # Password rotation, encryption
├── monitoring/            # Database health monitoring
├── backup/                # Automated backup management
├── tests/                 # Comprehensive test suite
└── docker-compose.yml     # Production stack
```

## 🛠️ Configuration

### Database Settings
Edit `config/database.yaml`:
```yaml
primary:
  host: "localhost"
  port: 5432
  database: "mcrm_project"
  user: "postgres"
  password: "your-password"

replica:
  host: "replica-host"
  port: 5432
  # ... same structure
```

### Monitoring Setup
Configure `config/monitoring.yaml`:
```yaml
check_interval_seconds: 60
thresholds:
  response_time_ms: 1000
  connection_usage_percent: 80

alerts:
  email:
    enabled: true
    smtp_server: "smtp.gmail.com"
    from_email: "alerts@company.com"
    to_emails: ["admin@company.com"]
```

## 📊 Web Interface

Access the web dashboard at `http://localhost:5000`:

- **Dashboard**: System overview with real-time statistics
- **GTIN Search**: Product lookup with autocomplete
- **Address Cleaner**: Batch address processing
- **File Upload**: CSV/Excel batch processing
- **Monitoring**: Real-time system health metrics

## 💻 CLI Usage

### GTIN Search
```bash
python scripts/mdlp_processor.py --gtin 4607034056439
python scripts/mdlp_processor.py --file gtins.txt --output results.xlsx
```

### Address Cleaning
```bash
python scripts/address_cleaner.py --address "г Москва ул Ленина д 1"
python scripts/address_cleaner.py --file addresses.csv --output cleaned.csv
```

### System Management
```bash
# Password rotation
python security/password_rotator.py --rotate-now

# Database monitoring
python monitoring/db_monitor.py --status

# Backup management
python backup/backup_manager.py --create-backup
python backup/backup_manager.py --list-backups
```

## 🔧 Performance Tuning

### Connection Pool Optimization
- **Primary Pool**: 5-20 connections for writes
- **Replica Pool**: 3-15 connections for reads
- **Automatic Failover**: Falls back to primary if replica unavailable

### Caching Strategy
- **Redis**: Primary cache with 1-hour TTL
- **Memory**: Fallback cache with 15-minute TTL
- **Categories**: GTIN (1h), addresses (30m), queries (15m)

### Index Management
Automatic index recommendations based on:
- Query frequency analysis
- Table size considerations  
- Performance impact measurement

## 📈 Monitoring & Alerting

### Health Checks
- Database connectivity and performance
- Connection pool utilization
- Cache hit/miss ratios
- Query execution times

### Alert Channels
- **Email**: SMTP-based notifications
- **Slack**: Webhook integration
- **Telegram**: Bot-based alerts
- **Dashboard**: Web-based status page

### Metrics Collection
- Prometheus metrics export
- Grafana visualization
- Historical data retention (7 days)
- Performance trend analysis

## 🚀 Deployment

### Docker Production Stack
```bash
# Start full stack
docker-compose up -d

# View logs
docker-compose logs -f

# Scale services
docker-compose up -d --scale web=3
```

### Manual Deployment
```bash
# Install dependencies
pip install -r requirements.txt

# Setup systemd services
sudo cp systemd/*.service /etc/systemd/system/
sudo systemctl enable datagrip-web datagrip-monitor
sudo systemctl start datagrip-web datagrip-monitor

# Setup nginx proxy
sudo cp nginx/datagrip.conf /etc/nginx/sites-available/
sudo ln -s /etc/nginx/sites-available/datagrip.conf /etc/nginx/sites-enabled/
sudo systemctl reload nginx
```

## 🧪 Testing

### Run Test Suite
```bash
# All tests
python -m pytest tests/ -v

# Specific components
python -m pytest tests/test_helpers.py -v
python -m pytest tests/test_performance.py -v

# Coverage report
python -m pytest tests/ --cov=. --cov-report=html
```

### Performance Testing
```bash
# Load testing
python tests/load_test.py --concurrent-users 50 --duration 300

# Database stress test
python tests/db_stress_test.py --connections 100 --queries 10000
```

## 🐛 Troubleshooting

### Common Issues

**Connection Timeouts**:
```bash
# Check connection pool status
python performance/connection_pool.py --status

# Increase pool size in config
# monitoring.pool.maxconn: 30
```

**High Memory Usage**:
```bash
# Clear caches
python performance/cache_manager.py --clear-all

# Check cache statistics
python performance/cache_manager.py --stats
```

**Slow Queries**:
```bash
# Analyze slow queries
python performance/db_indexes.py --analyze-slow-queries

# Create recommended indexes
python performance/db_indexes.py --apply-recommendations
```

### Logs Location
- **Application**: `logs/app.log`
- **Database**: `logs/db_monitor.log`
- **Performance**: `logs/performance.log`
- **Security**: `logs/security.log`

## 📞 Support

For issues and questions:
- Check logs in `logs/` directory
- Review configuration files
- Run diagnostic commands
- Monitor system health dashboard

## 📄 License

This project is proprietary software. All rights reserved.

---

**System Status**: ✅ Production Ready | **Last Updated**: 2024-01-15 | **Version**: 2.0.0 