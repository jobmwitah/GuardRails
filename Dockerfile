# Docker configuration for GuardRails backend

FROM python:3.12-slim

# Prevent Python from writing .pyc files and buffering stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY *.py .

# Storage directory for SQLite (volume-mountable)
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/bot_data.db

EXPOSE 8000

CMD ["python", "main.py"]
