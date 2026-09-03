FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Default location for the SQLite file. Mount a volume here or membership
# records disappear on every redeploy.
ENV DB_PATH=/data/gatekeeper.db
RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "-m", "app.main"]
