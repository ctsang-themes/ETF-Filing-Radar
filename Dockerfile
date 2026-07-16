FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# SEC_USER_AGENT must be set at runtime (Render dashboard -> Environment -> Add Secret)
CMD uvicorn app.api:app --host 0.0.0.0 --port ${PORT:-8000}
