FROM python:3.11-slim

WORKDIR /app

# 1. Сначала только requirements — этот слой кэшируется, пока requirements.txt не меняется
COPY shared/requirements.txt /app/shared/requirements.txt
RUN pip install --no-cache-dir -r /app/shared/requirements.txt

# 2. Потом код — если меняется только код, pip install берётся из кэша
COPY shared /app/shared
COPY services/catalog /app/catalog

ENV PYTHONPATH=/app
WORKDIR /app/catalog

EXPOSE 8001

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]
