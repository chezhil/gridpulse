FROM python:3.11-slim
WORKDIR /app
COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt
COPY backend /app/backend
COPY frontend /app/frontend
RUN mkdir -p /app/data
EXPOSE 8000
# Hosts such as Railway/Render assign PORT; default to 8000 locally. One worker: sessions and simulator state are in memory.
CMD ["sh", "-c", "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
