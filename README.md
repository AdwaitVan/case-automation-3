---
title: Case Automation App
emoji: 📊
colorFrom: green
colorTo: indigo
sdk: docker
pinned: false
---

Check out the configuration reference at https://huggingface.co/docs/hub/spaces-config-reference

# Note
the app will stop working properly if few are changes to hcserviecs ecourt website or lawyerservice website. need to update again,

# High Court Automation (Streamlit + Playwright)

## What was failing
Cloud logs showed repeated `Captcha blurry. Retrying...` and eventual failure after max retries.

Primary issue in code: captcha bytes were usually taken from an element screenshot, which can be low-quality in headless cloud runs. OCR then returns invalid/short text repeatedly.

## Fixes added
- Captcha fetch now prefers the real captcha image URL (`src`) via `page.request.get(...)`.
- Element screenshot is kept only as fallback.
- Added `device_scale_factor=2` to improve visual capture quality in cloud.
- Added per-attempt diagnostics:
  - full-page screenshot before captcha
  - raw captcha image
  - processed captcha image
  - DOM snapshot when captcha parse fails
  - screenshot when server says `Invalid Captcha`
- Added rich debug logs in Streamlit terminal:
  - captcha source
  - image dimensions (`raw` and browser `natural/client`)
  - OCR raw output and cleaned value

## Cloud diagnostics
Diagnostics are enabled by default in Docker:
- `DEBUG_MODE=1`
- `DEBUG_DIR=/app/debug_artifacts`

In app UI:
- Toggle `Enable cloud diagnostics`
- Set diagnostics folder path

## Run locally
```bash
pip install -r requirements.txt
playwright install chromium
streamlit run app.py
```

## Run in Docker
```bash
docker build -t case-automation-app-local .
docker run --rm -p 8501:8501 case-automation-app-local
```
