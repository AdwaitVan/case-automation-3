# 1. Keep Python on latest compatible line for ddddocr 1.5.x
FROM python:3.12-slim-bookworm

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
ENV PYTHONUNBUFFERED=1
ENV DEBUG_MODE=1
ENV DEBUG_DIR=/app/debug_artifacts

# 4. Install Python libraries
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN python -c "import ddddocr; ddddocr.DdddOcr(show_ad=False); print('ddddocr import ok')"

# 5. Install Playwright browser
RUN playwright install chromium
# We can skip install-deps because we installed them manually in Step 2

# 6. Copy your code
COPY . .

# 7. Run Streamlit
EXPOSE 8501
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]
