# 1. Use Python 3.10 on Debian Bullseye (Stable, widely supported)
FROM python:3.10-bullseye

# 2. Install system tools manually to avoid dependency issues
# We include specific libraries that Playwright needs
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    libgl1 \
    libglib2.0-0 \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# 3. Set up folder
WORKDIR /app

# 4. Install Python libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 5. Install Playwright & Chrome
RUN pip install playwright
RUN playwright install chromium
# We can skip install-deps because we installed them manually in Step 2

# 6. Copy your code
COPY . .

# 7. Run Streamlit
CMD ["streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0"]