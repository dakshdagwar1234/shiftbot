# Why this exists:
# Container definition for the Shift-Change Request Agent application.
# Installs pinned dependencies on lightweight Python slim and runs Uvicorn.

FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
