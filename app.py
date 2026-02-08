## Connection Check: VS Code is synced!
import base64
import io
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import ddddocr
import streamlit as st
from bs4 import BeautifulSoup
from PIL import Image, ImageEnhance
from playwright.sync_api import sync_playwright

# Compatibility for OCR libs expecting deprecated PIL constant.
if not hasattr(Image, "ANTIALIAS") and hasattr(Image, "Resampling"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

URL = "https://hcservices.ecourts.gov.in/hcservices/main.php"
MAX_RETRIES = 5


def update_terminal(message, placeholder, logs):
    now = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{now}] {message}")
    placeholder.code("\n".join(logs), language="bash")


def ensure_dir(path: Path):
    path.mkdir(parents=True, exist_ok=True)


def build_debug_file_path(debug_dir: Path, case_slug: str, attempt: int, name: str, ext: str):
    ts = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_case = re.sub(r"[^A-Za-z0-9_-]", "_", case_slug)
    return debug_dir / f"{safe_case}_attempt{attempt}_{name}_{ts}.{ext}"


def write_debug_bytes(debug_mode, debug_dir, case_slug, attempt, name, ext, data, placeholder, logs):
    if not debug_mode:
        return None
    ensure_dir(debug_dir)
    out = build_debug_file_path(debug_dir, case_slug, attempt, name, ext)
    out.write_bytes(data)
    update_terminal(f"[debug] saved {name}: {out.as_posix()}", placeholder, logs)
    return out


def latest_file(debug_dir: Path, pattern: str):
    if not debug_dir.exists():
        return None
    files = list(debug_dir.glob(pattern))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def build_debug_zip_bytes(debug_dir: Path):
    if not debug_dir.exists():
        return b""
    files = [p for p in debug_dir.iterdir() if p.is_file()]
    if not files:
        return b""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in sorted(files):
            zf.write(file_path, arcname=file_path.name)
    buf.seek(0)
    return buf.getvalue()


def solve_captcha(page, case_slug, attempt, debug_mode, debug_dir, placeholder, logs):
    try:
        page.wait_for_selector("#captcha_image", state="visible", timeout=8000)
        time.sleep(0.5)
        locator = page.locator("#captcha_image")
        src = (locator.get_attribute("src") or "").strip()
        captcha_bytes = None

        # Prefer the original image URL. Element screenshots in headless mode may be blurrier.
        if src:
            try:
                if src.startswith("data:image"):
                    _, b64 = src.split(",", 1)
                    captcha_bytes = base64.b64decode(b64)
                else:
                    img_url = urljoin(page.url, src)
                    res = page.request.get(img_url, timeout=15000)
                    if res.status == 200:
                        captcha_bytes = res.body()
                    else:
                        update_terminal(f"[debug] captcha URL status={res.status}", placeholder, logs)
            except Exception as err:
                update_terminal(f"[debug] captcha URL fetch failed: {str(err).splitlines()[0]}", placeholder, logs)

        if not captcha_bytes:
            captcha_bytes = locator.screenshot(type="png")

        write_debug_bytes(
            debug_mode, debug_dir, case_slug, attempt, "captcha_raw", "png", captcha_bytes, placeholder, logs
        )

        img = Image.open(io.BytesIO(captcha_bytes))
        raw_w, raw_h = img.size
        img = img.convert("RGB")
        img = ImageEnhance.Contrast(img).enhance(3.0)
        img = ImageEnhance.Brightness(img).enhance(1.2)
        img = img.resize((img.width * 4, img.height * 4), Image.LANCZOS)
        img = img.convert("L")
        img = img.point(lambda px: 0 if px < 128 else 255, "1")

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        processed = buf.getvalue()
        write_debug_bytes(
            debug_mode, debug_dir, case_slug, attempt, "captcha_processed", "png", processed, placeholder, logs
        )

        dims = page.evaluate(
            """() => {
                const el = document.querySelector('#captcha_image');
                if (!el) return null;
                return {
                    clientWidth: el.clientWidth,
                    clientHeight: el.clientHeight,
                    naturalWidth: el.naturalWidth || null,
                    naturalHeight: el.naturalHeight || null
                };
            }"""
        )

        ocr = ddddocr.DdddOcr(show_ad=False)
        raw_code = ocr.classification(processed)
        code = re.sub(r"[^A-Za-z0-9]", "", raw_code).strip()

        src_hint = "data-uri" if src.startswith("data:image") else (src[:120] or "n/a")
        update_terminal(
            f"[debug] captcha src={src_hint} raw={raw_w}x{raw_h} js={dims} ocr_raw='{raw_code}' ocr='{code}' len={len(code)}",
            placeholder,
            logs,
        )
        return code if len(code) == 6 else ""
    except Exception as err:
        update_terminal(f"[error] captcha exception: {str(err).splitlines()[0]}", placeholder, logs)
        return ""


def get_latest_order_link(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", class_="order_table")
    if not table:
        return None, None

    orders = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 5:
            continue
        date_text = cols[3].get_text(strip=True)
        link_tag = cols[4].find("a")
        if date_text and link_tag:
            try:
                dt_obj = datetime.strptime(date_text, "%d-%m-%Y")
                orders.append((dt_obj, link_tag.get("href")))
            except Exception:
                continue

    if not orders:
        return None, None
    orders.sort(key=lambda item: item[0], reverse=True)
    return orders[0][0].strftime("%d-%m-%Y"), orders[0][1]


def run_bot(cases, terminal_placeholder, debug_mode=False, debug_dir=Path("debug_artifacts")):
    logs = []
    results = []

    if debug_mode:
        ensure_dir(debug_dir)

    with sync_playwright() as p:
        update_terminal("[start] cloud robot", terminal_placeholder, logs)

        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            device_scale_factor=2,
        )
        page = context.new_page()
        page.set_default_timeout(60000)

        for case in cases:
            case_label = f"{case['name']} {case['no']}/{case['year']}"
            case_slug = f"{case['name']}_{case['no']}_{case['year']}"
            update_terminal(f"[case] {case_label}", terminal_placeholder, logs)
            success = False

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    update_terminal(f"[attempt {attempt}/{MAX_RETRIES}] open page", terminal_placeholder, logs)
                    try:
                        page.goto(URL, timeout=60000, wait_until="domcontentloaded")
                    except Exception:
                        update_terminal("[warn] page load timeout (continue)", terminal_placeholder, logs)

                    time.sleep(2)
                    try:
                        page.evaluate("document.querySelectorAll('.modal, .alert, #bs_alert').forEach(e => e.remove())")
                        update_terminal("[info] popups removed", terminal_placeholder, logs)
                    except Exception:
                        pass

                    try:
                        page.locator("#leftPaneMenuCS").click(force=True)
                    except Exception:
                        page.evaluate("document.querySelector('#leftPaneMenuCS').click()")

                    page.select_option("#sess_state_code", value="1")
                    time.sleep(1)
                    page.select_option("#court_complex_code", value="1")
                    time.sleep(1)

                    try:
                        if page.locator("#CScaseNumber").is_visible():
                            page.locator("#CScaseNumber").click(force=True)
                    except Exception:
                        page.evaluate("document.querySelector('#CScaseNumber').click()")

                    page.select_option("#case_type", value=case["value"])
                    page.locator("#search_case_no").fill(case["no"])
                    page.locator("#rgyear").fill(case["year"])

                    write_debug_bytes(
                        debug_mode,
                        debug_dir,
                        case_slug,
                        attempt,
                        "page_before_captcha",
                        "png",
                        page.screenshot(full_page=True),
                        terminal_placeholder,
                        logs,
                    )
                    code = solve_captcha(
                        page, case_slug, attempt, debug_mode, debug_dir, terminal_placeholder, logs
                    )
                    if not code:
                        update_terminal("[warn] captcha unreadable. retrying", terminal_placeholder, logs)
                        write_debug_bytes(
                            debug_mode,
                            debug_dir,
                            case_slug,
                            attempt,
                            "dom_snapshot",
                            "html",
                            page.content().encode("utf-8", errors="ignore"),
                            terminal_placeholder,
                            logs,
                        )
                        page.reload()
                        continue

                    page.locator("#captcha").fill(code)
                    page.locator("#goResetDiv input[value='Go']").click(force=True)

                    try:
                        page.wait_for_selector("text=Invalid Captcha", timeout=3000)
                        update_terminal("[warn] invalid captcha. retrying", terminal_placeholder, logs)
                        write_debug_bytes(
                            debug_mode,
                            debug_dir,
                            case_slug,
                            attempt,
                            "invalid_captcha_page",
                            "png",
                            page.screenshot(full_page=True),
                            terminal_placeholder,
                            logs,
                        )
                        continue
                    except Exception:
                        pass

                    try:
                        page.wait_for_selector("#dispTable a[onclick*='viewHistory']", timeout=10000)
                        page.locator("#dispTable a[onclick*='viewHistory']").first.click(force=True)
                        page.wait_for_selector(".order_table", state="visible", timeout=20000)
                    except Exception:
                        update_terminal("[info] no history/orders found", terminal_placeholder, logs)
                        success = True
                        break

                    date_str, rel_link = get_latest_order_link(page.content())
                    if date_str:
                        full_url = f"https://hcservices.ecourts.gov.in/hcservices/{rel_link}"
                        update_terminal(f"[info] latest order date: {date_str}", terminal_placeholder, logs)
                        response = page.request.get(full_url)
                        content_type = response.headers.get("content-type", "")
                        if response.status == 200 and "application/pdf" in content_type:
                            results.append(
                                {
                                    "label": f"{case['no']}/{case['year']}",
                                    "desc": f"{case['name']} (Order: {date_str})",
                                    "data": response.body(),
                                }
                            )
                            update_terminal("[ok] pdf downloaded", terminal_placeholder, logs)
                            success = True
                            break
                        update_terminal("[warn] order listed but file missing/broken", terminal_placeholder, logs)
                        success = True
                        break

                    update_terminal("[info] no recent orders found", terminal_placeholder, logs)
                    success = True
                    break
                except Exception as err:
                    msg = str(err).split("\n")[0]
                    update_terminal(f"[warn] retry {attempt} exception: {msg}", terminal_placeholder, logs)
                    write_debug_bytes(
                        debug_mode,
                        debug_dir,
                        case_slug,
                        attempt,
                        "exception_page",
                        "png",
                        page.screenshot(full_page=True),
                        terminal_placeholder,
                        logs,
                    )
                    time.sleep(2)

            if not success:
                update_terminal("[error] failed after retries", terminal_placeholder, logs)
            time.sleep(1)

        browser.close()
        update_terminal("[done] finished", terminal_placeholder, logs)
        return results


st.set_page_config(page_title="High Court Bot", layout="wide")
st.title("High Court Automation")

default_debug_mode = os.getenv("DEBUG_MODE", "1") == "1"
default_debug_dir = os.getenv("DEBUG_DIR", "debug_artifacts")
debug_mode = st.checkbox("Enable cloud diagnostics", value=default_debug_mode)
debug_dir = Path(st.text_input("Diagnostics folder", value=default_debug_dir).strip() or "debug_artifacts")

if debug_dir.exists():
    debug_files = sorted([p for p in debug_dir.iterdir() if p.is_file()], key=lambda p: p.stat().st_mtime, reverse=True)
    st.caption(f"Debug path: `{debug_dir.as_posix()}` | Files: {len(debug_files)}")
    if debug_files:
        zip_bytes = build_debug_zip_bytes(debug_dir)
        if zip_bytes:
            st.download_button(
                "Download all debug files (.zip)",
                data=zip_bytes,
                file_name=f"debug_artifacts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip",
                mime="application/zip",
            )
        with st.expander("Latest debug files", expanded=False):
            for p in debug_files[:20]:
                st.write(p.name)
else:
    st.caption(f"Debug path: `{debug_dir.as_posix()}` (not created yet)")

CASES = [
    {"name": "Second Appeal", "value": "4", "no": "508", "year": "1999"},
    {"name": "Writ Petition", "value": "1", "no": "11311", "year": "2025"},
]

if st.button("Fetch Orders"):
    terminal = st.empty()
    results = run_bot(CASES, terminal, debug_mode=debug_mode, debug_dir=debug_dir)
    st.session_state["last_results"] = results

    if debug_mode:
        st.info(f"Diagnostics saved under: `{debug_dir.as_posix()}`")
        raw_img = latest_file(debug_dir, "*_captcha_raw_*.png")
        processed_img = latest_file(debug_dir, "*_captcha_processed_*.png")
        if raw_img or processed_img:
            st.markdown("### Latest Captcha Diagnostics")
            col1, col2 = st.columns(2)
            with col1:
                if raw_img:
                    st.image(str(raw_img), caption=f"Raw captcha: {raw_img.name}", use_column_width=True)
                else:
                    st.write("No raw captcha image found yet.")
            with col2:
                if processed_img:
                    st.image(
                        str(processed_img),
                        caption=f"Processed captcha: {processed_img.name}",
                        use_column_width=True,
                    )
                else:
                    st.write("No processed captcha image found yet.")

results_to_show = st.session_state.get("last_results", [])
if results_to_show:
    st.markdown("---")
    st.success(f"Fetched {len(results_to_show)} orders")
    for res in results_to_show:
        with st.expander(res["desc"], expanded=True):
            st.download_button(
                label="Download PDF",
                data=res["data"],
                file_name=f"{res['label'].replace('/', '_')}.pdf",
                mime="application/pdf",
            )
            b64_pdf = base64.b64encode(res["data"]).decode("utf-8")
            pdf_display = (
                f'<iframe src="data:application/pdf;base64,{b64_pdf}" width="100%" height="500"></iframe>'
            )
            st.markdown(pdf_display, unsafe_allow_html=True)
elif "last_results" in st.session_state:
    st.warning("Run finished, but no orders were fetched in this attempt. Check terminal/debug artifacts above.")
