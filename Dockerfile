# Simple production Dockerfile
FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

ARG CACHEBUST=1
COPY requirements.txt .
RUN echo $CACHEBUST && pip install --no-cache-dir -r requirements.txt

COPY backend ./backend

ENV PORT=8080
EXPOSE 8080

CMD uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8080}
