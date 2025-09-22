FROM python:3.13.5-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

ENV PYTHONUNBUFFERED=1
ENV UVICORN_PORT=9999
ENV UVICORN_HOST=0.0.0.0
CMD ["uvicorn", "main:app"]
