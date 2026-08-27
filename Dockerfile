FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    HTTP_HOST=0.0.0.0

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd --create-home --uid 10001 bot && chown -R bot /app
USER bot

# State lives in PostgreSQL (DATABASE_URL), never on this filesystem.
# The only local surface is the health endpoint an uptime monitor calls.
EXPOSE 8080

CMD ["python", "-m", "app.main"]
