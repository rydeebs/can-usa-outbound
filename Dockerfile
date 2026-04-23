FROM python:3.11-slim

WORKDIR /app

# Copy and install dependencies first (cache layer)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt && \
    python -c "from googleapiclient.discovery import build; print('googleapiclient OK')" && \
    python -c "import schedule; print('schedule OK')"

# Copy application code
COPY . .

RUN mkdir -p data

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]