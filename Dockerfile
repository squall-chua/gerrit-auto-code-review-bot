FROM python:3.12-slim

# Set environment variables
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# Install system dependencies if required by cryptography or other packages
# RUN apt-get update && apt-get install -y --no-install-recommends gcc && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source code explicitly
COPY main.py .
COPY analyzer/ analyzer/
COPY bot/ bot/
COPY gerrit/ gerrit/

# Run the bot
CMD ["python", "main.py"]
