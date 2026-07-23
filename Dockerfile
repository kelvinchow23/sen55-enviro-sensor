FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY sen55.py .

ENV PYTHONUNBUFFERED=1

CMD ["python", "sen55.py"]
