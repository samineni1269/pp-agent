FROM python:3.11-slim

WORKDIR /app

# Install dependencies first (cached layer)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Expose Koyeb's expected port
EXPOSE 8000

# Run with gunicorn — 1 worker, 4 threads (fits 512 MB free tier)
CMD ["gunicorn", "app:app", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "4", "--timeout", "120"]
