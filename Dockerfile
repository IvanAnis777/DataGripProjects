# Multi-stage Dockerfile для оптимизации размера
FROM python:3.13-slim as builder

# Устанавливаем системные зависимости для сборки
RUN apt-get update && apt-get install -y \
    build-essential \
    git \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

# Создаем виртуальное окружение
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Копируем и устанавливаем зависимости
COPY requirements_minimal.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements_minimal.txt

# Production stage
FROM python:3.13-slim as production

# Устанавливаем runtime зависимости
RUN apt-get update && apt-get install -y \
    postgresql-client \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Создаем пользователя для приложения
RUN groupadd -r appuser && useradd -r -g appuser appuser

# Копируем виртуальное окружение из builder stage
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Создаем директории приложения
WORKDIR /app
RUN mkdir -p /app/logs /app/backups /app/performance /app/monitoring /app/security && \
    chown -R appuser:appuser /app

# Копируем код приложения
COPY --chown=appuser:appuser . .

# Устанавливаем переменные окружения
ENV PYTHONPATH=/app \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow

# Переключаемся на пользователя приложения
USER appuser

# Создаем точки монтирования
VOLUME ["/app/backups", "/app/logs", "/app/data"]

# Открываем порты
EXPOSE 8000 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import helpers; print('OK')" || exit 1

# Точка входа по умолчанию
CMD ["python", "main.py"] 