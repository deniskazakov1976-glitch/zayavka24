FROM python:3.12-slim

# Create non-root user
RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy project files
COPY . .

# Ensure data and companies directories exist with correct permissions
RUN mkdir -p /app/data /app/companies && \
    chown -R appuser:appgroup /app

USER appuser

EXPOSE 8080

CMD ["python", "main.py"]
