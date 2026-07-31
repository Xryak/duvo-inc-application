# If the task needs browser automation inside the container, switch base to:
#   FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/

CMD ["python", "-m", "src.main"]
