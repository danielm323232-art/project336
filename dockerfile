FROM python:3.12-slim

# Install Tesseract + Amharic language support
RUN apt-get update && apt-get install -y \
    tesseract-ocr \
    tesseract-ocr-amh \
    libtesseract-dev \
    libleptonica-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy project files
COPY . .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Expose Render's dynamic port
EXPOSE 10000

# Start FastAPI app with uvicorn (bot.py must define "app = FastAPI()")
CMD ["uvicorn", "bot:app", "--host", "0.0.0.0", "--port", "10000"]
