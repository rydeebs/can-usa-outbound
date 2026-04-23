FROM python:3.11-slim

WORKDIR /app

# Install dependencies — add --no-cache to force fresh install
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

# Copy everything
COPY . .

# Ensure data directory exists
RUN mkdir -p data

EXPOSE 8080

CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8080"]