# 1. Use Python 3.9 (Best for ddddocr stability)
FROM python:3.10-slim

# 2. Install system tools needed for Chrome
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 3. Set up folder
WORKDIR /app

# 4. Install Python libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Install Playwright & Chrome Browser (The magic step)
RUN pip install playwright
RUN playwright install chromium
RUN playwright install-deps

# 6. Copy your code
COPY . .

# 7. Run Streamlit on Port 7860 (Standard for Hugging Face)
CMD ["streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0"]