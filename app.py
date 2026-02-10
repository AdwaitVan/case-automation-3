## Connection Check: VS Code is synced!
import base64
import html
import io
import json
import os
import re
import time
import zipfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin

import ddddocr
import streamlit as st
import streamlit.components.v1 as components
from bs4 import BeautifulSoup
from PIL import Image, ImageEnhance
from playwright.sync_api import sync_playwright

# Compatibility for OCR libs expecting deprecated PIL constant.
if not hasattr(Image, "ANTIALIAS") and hasattr(Image, "Resampling"):
    Image.ANTIALIAS = Image.Resampling.LANCZOS

URL = "https://hcservices.ecourts.gov.in/hcservices/main.php"
MAX_RETRIES = 5
CASE_TYPES_FILE = Path(__file__).with_name("bench_case_types.json")
HIGH_COURTS = {
    "Allahabad High Court": "13",
    "Bombay High Court": "1",
    "Calcutta High Court": "16",
    "Gauhati High Court": "6",
    "High Court  for State of Telangana": "29",
    "High Court of Andhra Pradesh": "2",
    "High Court of Chhattisgarh": "17",
    "High Court of Delhi": "26",
    "High Court of Gujarat": "18",
    "High Court of Himachal Pradesh": "5",
    "High Court of Jammu and Kashmir": "12",
    "High Court of Jharkhand": "7",
    "High Court of Karnataka": "3",
    "High Court of Kerala": "4",
    "High Court of Madhya Pradesh": "23",
    "High Court of Manipur": "25",
    "High Court of Meghalaya": "21",
    "High Court of Orissa": "11",
    "High Court of Punjab and Haryana": "22",
    "High Court of Rajasthan": "9",
    "High Court of Sikkim": "24",
    "High Court of Tripura": "20",
    "High Court of Uttarakhand": "15",
    "Madras High Court": "10",
    "Patna High Court": "8",
}
BENCHES_BY_HIGH_COURT = {
    "1": {
        "Appellate Side,Bombay": "1",
        "Bench at Aurangabad": "3",
        "Bench at Nagpur": "4",
        "Bombay High Court,Bench at Kolhapur": "7",
        "High court of Bombay at Goa": "5",
        "Original Side,Bombay": "2",
        "Special Court (TORTS) Bombay": "6",
    }
}


def load_case_types_by_bench(path: Path):
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        return {}
    return {}


CASE_TYPES_BY_BENCH = load_case_types_by_bench(CASE_TYPES_FILE)


def update_terminal(message, placeholder, logs):
    now = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{now}] {message}")
    # Keep terminal as a single contained box and always show latest lines.
    placeholder.text_area(
        "Live terminal logs",
        value="\n".join(logs[-250:]),
        height=260,
        disabled=True,
        label_visibility="collapsed",
    )


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


def render_fetch_timer(timer_placeholder, start_time, total_cases, current_case_index):
    if timer_placeholder is None or start_time is None:
        return
    elapsed = max(time.time() - start_time, 0.0)
    mm = int(elapsed // 60)
    ss = int(elapsed % 60)
    timer_placeholder.caption(
        f"Fetching timer: {mm:02d}:{ss:02d} | Case {current_case_index}/{max(total_cases, 1)}"
    )


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


def extract_cnr_number(html_content):
    # Example: HCBM01-023517-1995
    match = re.search(r"\b[A-Z]{2,6}\d{2}-\d{6}-\d{4}\b", html_content or "")
    return match.group(0) if match else ""


def run_bot(
    cases,
    terminal_placeholder,
    default_sess_state_code="1",
    default_court_complex_code="1",
    timer_placeholder=None,
    fetch_start_time=None,
    debug_mode=False,
    debug_dir=Path("debug_artifacts"),
):
    logs = []
    results = []
    case_outcomes = []

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

        total_cases = len(cases)
        for case_index, case in enumerate(cases, start=1):
            render_fetch_timer(timer_placeholder, fetch_start_time, total_cases, case_index)
            case_label = f"{case['name']} {case['no']}/{case['year']}"
            case_slug = f"{case['name']}_{case['no']}_{case['year']}"
            row_no = case.get("source_row")
            search_mode = case.get("search_mode", "CN")
            update_terminal(f"[case] {case_label}", terminal_placeholder, logs)
            success = False
            fetched = False
            outcome_reason = "Unknown"

            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    update_terminal(f"[attempt {attempt}/{MAX_RETRIES}] open page", terminal_placeholder, logs)
                    render_fetch_timer(timer_placeholder, fetch_start_time, total_cases, case_index)
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

                    case_sess_state_code = case.get("sess_state_code", default_sess_state_code)
                    case_court_complex_code = case.get("court_complex_code", default_court_complex_code)
                    page.select_option("#sess_state_code", value=case_sess_state_code)
                    time.sleep(1)
                    page.select_option("#court_complex_code", value=case_court_complex_code)
                    time.sleep(1)

                    if search_mode == "ST":
                        try:
                            if page.locator("#CSfilingNumber").is_visible():
                                page.locator("#CSfilingNumber").click(force=True)
                        except Exception:
                            page.evaluate("document.querySelector('#CSfilingNumber').click()")
                        page.locator("#filing_no").fill(case["no"])
                        page.locator("#filyear").fill(case["year"])
                    else:
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

                    cnr_no = extract_cnr_number(page.content())
                    if cnr_no:
                        update_terminal(f"[info] CNR: {cnr_no}", terminal_placeholder, logs)

                    try:
                        page.wait_for_selector("#dispTable a[onclick*='viewHistory']", timeout=10000)
                        page.locator("#dispTable a[onclick*='viewHistory']").first.click(force=True)
                        page.wait_for_selector(".order_table", state="visible", timeout=20000)
                    except Exception:
                        update_terminal("[info] no history/orders found", terminal_placeholder, logs)
                        outcome_reason = "No history/orders found"
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
                                    "source_row": row_no,
                                    "cnr": cnr_no,
                                }
                            )
                            update_terminal("[ok] pdf downloaded", terminal_placeholder, logs)
                            fetched = True
                            outcome_reason = "PDF downloaded"
                            success = True
                            break
                        update_terminal("[warn] order listed but file missing/broken", terminal_placeholder, logs)
                        outcome_reason = "Order listed but PDF missing/broken"
                        success = True
                        break

                    update_terminal("[info] no recent orders found", terminal_placeholder, logs)
                    outcome_reason = "No recent orders found"
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
                outcome_reason = "Failed after retries"
            case_outcomes.append(
                {
                    "source_row": row_no,
                    "case_label": case_label,
                    "fetched": fetched,
                    "reason": outcome_reason,
                }
            )
            time.sleep(1)
            render_fetch_timer(timer_placeholder, fetch_start_time, total_cases, case_index)

        browser.close()
        update_terminal("[done] finished", terminal_placeholder, logs)
        return results, logs, case_outcomes


st.set_page_config(page_title="High Court Bot", layout="wide")
st.title("High Court Automation")
st.markdown(
    """
<style>
[class*="st-key-top_add_row"] div[data-testid="stButton"] > button,
[class*="st-key-bottom_add_row"] div[data-testid="stButton"] > button,
[class*="st-key-top_add_row"] button,
[class*="st-key-bottom_add_row"] button {
    background-color: #2e7d32 !important;
    color: white !important;
    border-color: #2e7d32 !important;
}
[class*="st-key-top_reset_rows"] div[data-testid="stButton"] > button,
[class*="st-key-bottom_reset_rows"] div[data-testid="stButton"] > button,
[class*="st-key-top_reset_rows"] button,
[class*="st-key-bottom_reset_rows"] button {
    background-color: #ffdede !important;
    color: #7a1111 !important;
    border-color: #ffbcbc !important;
}
[class*="st-key-row_st_btn_off_"] button,
[class*="st-key-row_st_btn_on_"] button {
    width: 100% !important;
    min-height: 3rem !important;
    white-space: nowrap !important;
    padding: 0.25rem 0.4rem !important;
    font-weight: 600 !important;
}
[class*="st-key-row_st_btn_on_"] button {
    background-color: #e9f8ec !important;
    color: #116329 !important;
    border-color: #55b16b !important;
}
</style>
""",
    unsafe_allow_html=True,
)

main_col, bg_col = st.columns([2, 1], gap="large")

with main_col:
    st.subheader("Main")
    avg_case_seconds = float(st.session_state.get("avg_case_seconds", 35.0))
    st.caption(f"Estimated time per case: ~{int(avg_case_seconds)} sec")
    high_court_name = st.selectbox(
        "High Court",
        options=list(HIGH_COURTS.keys()),
        index=list(HIGH_COURTS.keys()).index("Bombay High Court"),
    )
    selected_hc_code = HIGH_COURTS[high_court_name]
    bench_map = BENCHES_BY_HIGH_COURT.get(selected_hc_code, {})
    bench_options = list(bench_map.keys()) if bench_map else []
    default_row_bench = "Appellate Side,Bombay" if "Appellate Side,Bombay" in bench_options else ""
    quick_filter_text = st.text_input(
        "Case Type Search",
        value="",
        help="Type part of case type (e.g. cra, wp, contempt). Filter applies bench-wise per row.",
    ).strip().lower()

    st.caption("One row = one case. Use per-row `bench` and `case_type` dropdowns.")
    if not bench_map:
        st.warning("Bench list for this High Court is not configured yet.")

    sample_rows = [
        {"bench": "Appellate Side,Bombay", "mode": "CN", "case_type": "SA(Second Appeal)-4", "no": "508", "year": "1999"},
    ]

    if "next_row_id" not in st.session_state:
        st.session_state["next_row_id"] = 1

    def make_row(bench="", mode="CN", case_type="", no="", year=""):
        row_id = st.session_state["next_row_id"]
        st.session_state["next_row_id"] += 1
        return {"id": row_id, "bench": bench, "mode": mode, "case_type": case_type, "no": no, "year": year}

    if "case_rows" not in st.session_state:
        st.session_state["case_rows"] = [make_row(**r) for r in sample_rows]

    def add_row_once():
        st.session_state["case_rows"].append(make_row(bench=default_row_bench))

    def reset_to_first_sample_once():
        first = sample_rows[0]
        st.session_state["case_rows"] = [make_row(**first)]

    def set_focus_target(target_label):
        st.session_state["focus_input_label"] = target_label

    action_col1, action_col2 = st.columns(2)
    with action_col1:
        st.button("Add Row", key="top_add_row", on_click=add_row_once)
    with action_col2:
        st.button("Reset To First Sample", key="top_reset_rows", on_click=reset_to_first_sample_once)

    st.markdown("**Case Table**")
    head1, head2, head3, head4, head5, head6, head7 = st.columns([5, 1.2, 6, 2, 2, 1, 1])
    head1.markdown("`bench`")
    head2.markdown("`ST`")
    head3.markdown("`case_type`")
    head4.markdown("`no`")
    head5.markdown("`year`")
    head6.markdown("`x`")
    head7.markdown("`↻`")

    row_inputs = []
    row_id_to_delete = None
    retry_row_id = None
    has_previous_fetch = "last_outcomes" in st.session_state
    for idx, row in enumerate(st.session_state["case_rows"], start=1):
        row_id = row.get("id")
        c1, c2, c3, c4, c5, c6, c7 = st.columns([5, 1.2, 6, 2, 2, 1, 1])

        default_bench = str(row.get("bench", "") or "")
        if bench_options:
            bench_choice_options = ["Choose Option"] + bench_options
            bench_idx = (
                bench_choice_options.index(default_bench)
                if default_bench in bench_choice_options
                else 0
            )
            bench_name_display = c1.selectbox(
                f"bench_{idx}",
                options=bench_choice_options,
                index=bench_idx,
                key=f"row_bench_{row_id}",
                label_visibility="collapsed",
                on_change=set_focus_target,
                args=(f"case_type_{idx}",),
            )
            if bench_name_display == "Choose Option":
                bench_name = ""
            else:
                bench_name = bench_name_display
        else:
            bench_name = c1.text_input(
                f"bench_{idx}",
                value=default_bench,
                key=f"row_bench_{row_id}",
                label_visibility="collapsed",
            ).strip()

        mode = str(row.get("mode", "CN") or "CN")
        if mode == "ST":
            st_key = f"row_st_btn_on_{row_id}"
            st_label = "ST"
        else:
            st_key = f"row_st_btn_off_{row_id}"
            st_label = "ST"

        if c2.button(st_label, key=st_key, help="Toggle ST mode for this row"):
            mode = "CN" if mode == "ST" else "ST"

        bench_case_types = CASE_TYPES_BY_BENCH.get(bench_name, [])
        bench_case_labels = [item.get("label", "").strip() for item in bench_case_types if item.get("label")]
        if quick_filter_text:
            bench_case_labels = [opt for opt in bench_case_labels if quick_filter_text in opt.lower()]
        if not bench_case_labels:
            bench_case_labels = [item.get("label", "").strip() for item in CASE_TYPES_BY_BENCH.get(bench_name, []) if item.get("label")]

        default_case_type = str(row.get("case_type", "") or "")
        case_type_options = ["Choose Option"] + bench_case_labels if bench_case_labels else ["Choose Option"]
        if default_case_type and default_case_type not in case_type_options:
            case_type_options = ["Choose Option", default_case_type] + bench_case_labels
        ct_idx = case_type_options.index(default_case_type) if default_case_type in case_type_options else 0
        case_type = c3.selectbox(
            f"case_type_{idx}",
            options=case_type_options,
            index=ct_idx,
            key=f"row_case_type_{row_id}",
            label_visibility="collapsed",
            on_change=set_focus_target,
            args=(f"no_{idx}",),
            disabled=(mode == "ST"),
        )
        if case_type == "Choose Option" or mode == "ST":
            case_type = ""

        no = c4.text_input(
            f"no_{idx}",
            value=str(row.get("no", "") or ""),
            key=f"row_no_{row_id}",
            label_visibility="collapsed",
        ).strip()
        year = c5.text_input(
            f"year_{idx}",
            value=str(row.get("year", "") or ""),
            key=f"row_year_{row_id}",
            label_visibility="collapsed",
        ).strip()
        if c6.button("x", key=f"row_remove_{row_id}", help="Remove this row"):
            row_id_to_delete = row_id
        if c7.button("↻", key=f"row_retry_{row_id}", help="Retry this row", disabled=not has_previous_fetch):
            retry_row_id = row_id

        row_inputs.append({"id": row_id, "bench": bench_name, "mode": mode, "case_type": case_type, "no": no, "year": year})

    if row_id_to_delete is not None:
        st.session_state["case_rows"] = [r for r in row_inputs if r.get("id") != row_id_to_delete]
        st.rerun()

    st.session_state["case_rows"] = row_inputs

    focus_label = st.session_state.pop("focus_input_label", "")
    if focus_label:
        components.html(
            f"""
<script>
  const target = window.parent.document.querySelector('input[aria-label="{focus_label}"]');
  if (target) {{
    target.focus();
    target.select();
    target.scrollIntoView({{behavior: "smooth", block: "center"}});
  }}
</script>
""",
            height=0,
        )

    bottom_col1, bottom_col2 = st.columns(2)
    with bottom_col1:
        st.button("Add Row", key="bottom_add_row", on_click=add_row_once)
    with bottom_col2:
        st.button("Reset To First Sample", key="bottom_reset_rows", on_click=reset_to_first_sample_once)

    parsed_cases = []
    parse_errors = []
    for idx, row in enumerate(row_inputs, start=1):
        bench_name = str(row.get("bench", "") or "").strip()
        mode = str(row.get("mode", "CN") or "CN")
        case_type = str(row.get("case_type", "") or "").strip()
        no = str(row.get("no", "") or "").strip()
        year = str(row.get("year", "") or "").strip()

        if not bench_name and not case_type and not no and not year:
            continue
        if mode == "ST":
            if not (bench_name and no and year):
                parse_errors.append(f"Row {idx}: fill bench, filing no, year for ST mode")
                continue
        elif not (bench_name and case_type and no and year):
            parse_errors.append(f"Row {idx}: fill all columns (bench, case_type, no, year)")
            continue
        if bench_map and bench_name not in bench_map:
            parse_errors.append(f"Row {idx}: invalid bench '{bench_name}' for selected High Court")
            continue
        value = ""
        if mode != "ST":
            bench_case_types = CASE_TYPES_BY_BENCH.get(bench_name, [])
            label_to_value = {item.get("label"): item.get("value") for item in bench_case_types}
            value = label_to_value.get(case_type)
            if not value:
                parse_errors.append(f"Row {idx}: case_type '{case_type}' is not valid for bench '{bench_name}'")
                continue
        case_bench_code = bench_map[bench_name] if bench_map else bench_name
        parsed_cases.append(
            {
                "row_id": row.get("id"),
                "source_row": idx,
                "bench": bench_name,
                "case_type": case_type,
                "search_mode": mode,
                "name": case_type if mode != "ST" else "Filing Number",
                "value": value,
                "no": no,
                "year": year,
                "sess_state_code": selected_hc_code,
                "court_complex_code": case_bench_code,
            }
        )

    if parse_errors:
        for err in parse_errors:
            st.error(err)
    else:
        st.caption(f"Cases ready: {len(parsed_cases)}")
        est_total = len(parsed_cases) * avg_case_seconds
        st.caption(f"Estimated total fetch time: ~{int(est_total // 60)}m {int(est_total % 60)}s")
    fetch_orders = st.button("Fetch Orders", disabled=not parsed_cases or bool(parse_errors))
    parsed_case_by_row_id = {c.get("row_id"): c for c in parsed_cases}

with bg_col:
    st.subheader("History")
    if "run_history" not in st.session_state:
        st.session_state["run_history"] = []
    history_items = st.session_state.get("run_history", [])
    if history_items:
        for hidx, entry in enumerate(history_items[:10], start=1):
            header = (
                f"{entry.get('run_id', 'R-NA')} | {entry['timestamp']} | "
                f"Fetched: {entry.get('fetched_rows', 0)} | Failed: {entry.get('failed_rows', 0)}"
            )
            with st.expander(header, expanded=False):
                for cidx, case in enumerate(entry["cases"], start=1):
                    hc1, hc2 = st.columns([10, 1])
                    with hc1:
                        mode_txt = case.get("mode", "CN")
                        mode_label = "ST" if mode_txt == "ST" else "CN"
                        st.caption(
                            f"[{mode_label}] {case['bench']} | {case['case_type'] or 'Filing Number'} | {case['no']}/{case['year']}"
                        )
                    with hc2:
                        if st.button("+", key=f"hist_add_{hidx}_{cidx}", help="Add this case back to rows"):
                            st.session_state["case_rows"].append(
                                make_row(
                                    bench=case["bench"],
                                    mode=case.get("mode", "CN"),
                                    case_type=case["case_type"],
                                    no=case["no"],
                                    year=case["year"],
                                )
                            )
    else:
        st.caption("No run history yet.")

    st.subheader("Background")
    default_debug_mode = os.getenv("DEBUG_MODE", "1") == "1"
    default_debug_dir = os.getenv("DEBUG_DIR", "debug_artifacts")
    debug_mode = st.checkbox("Enable cloud diagnostics", value=default_debug_mode)
    debug_dir = Path(st.text_input("Diagnostics folder", value=default_debug_dir).strip() or "debug_artifacts")
    st.caption("Live terminal logs")
    timer_placeholder = st.empty()
    terminal = st.empty()

    last_run_logs = st.session_state.get("last_run_logs", [])
    if last_run_logs:
        log_text = "\n".join(last_run_logs)
        st.download_button(
            "Download last run terminal logs (.txt)",
            data=log_text.encode("utf-8"),
            file_name=f"run_terminal_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            mime="text/plain",
        )
        with st.expander("Last run terminal logs", expanded=False):
            st.code(log_text, language="bash")

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

run_single_retry = retry_row_id is not None and retry_row_id in parsed_case_by_row_id
if fetch_orders or run_single_retry:
    run_now = datetime.now()
    run_id = run_now.strftime("R-%y%m%d-%H%M%S")
    run_cases = parsed_cases if fetch_orders else [parsed_case_by_row_id[retry_row_id]]
    t0 = time.time()
    results, run_logs, case_outcomes = run_bot(
        run_cases,
        terminal,
        default_sess_state_code=selected_hc_code,
        default_court_complex_code="1",
        timer_placeholder=timer_placeholder,
        fetch_start_time=t0,
        debug_mode=debug_mode,
        debug_dir=debug_dir,
    )
    timer_placeholder.empty()
    elapsed = max(time.time() - t0, 1.0)
    per_case = elapsed / max(len(run_cases), 1)
    old_avg = float(st.session_state.get("avg_case_seconds", 35.0))
    st.session_state["avg_case_seconds"] = (old_avg * 0.7) + (per_case * 0.3)

    existing_results = st.session_state.get("last_results", [])
    if run_single_retry:
        keep = [r for r in existing_results if r.get("source_row") != retry_row_id]
        st.session_state["last_results"] = keep + results
    else:
        st.session_state["last_results"] = results
    st.session_state["last_run_logs"] = run_logs
    st.session_state["last_outcomes"] = case_outcomes
    st.session_state["run_history"].insert(
        0,
        {
            "run_id": run_id,
            "timestamp": run_now.strftime("%Y-%m-%d %H:%M:%S"),
            "total_rows": len(run_cases),
            "fetched_rows": len([o for o in case_outcomes if o.get("fetched")]),
            "failed_rows": len([o for o in case_outcomes if not o.get("fetched")]),
            "cases": [
                {
                    "bench": c["bench"],
                    "mode": c.get("mode", "CN"),
                    "case_type": c["case_type"],
                    "no": c["no"],
                    "year": c["year"],
                }
                for c in row_inputs
                if c["bench"] and c["case_type"] and c["no"] and c["year"]
            ],
        },
    )
    st.session_state["run_history"] = st.session_state["run_history"][:20]

    if debug_mode:
        ensure_dir(debug_dir)
        log_path = debug_dir / f"terminal_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt"
        log_path.write_text("\n".join(run_logs), encoding="utf-8")

    if debug_mode:
        with bg_col:
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

with main_col:
    st.markdown('<div id="results_anchor"></div>', unsafe_allow_html=True)
    results_to_show = st.session_state.get("last_results", [])
    last_outcomes = st.session_state.get("last_outcomes", [])
    not_fetched_rows = [str(o.get("source_row")) for o in last_outcomes if not o.get("fetched") and o.get("source_row")]
    if not_fetched_rows:
        st.warning(f"Rows not fetched: {', '.join(not_fetched_rows)}")

    if results_to_show:
        st.markdown("---")
        st.success(f"Fetched {len(results_to_show)} orders")
        for res in results_to_show:
            with st.expander(res["desc"], expanded=True):
                if res.get("cnr"):
                    st.caption(f"CNR: {res['cnr']}")
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
        st.warning("Run finished, but no orders were fetched in this attempt. Check terminal/debug artifacts in Background.")

if fetch_orders or run_single_retry:
    components.html(
        """
<script>
  const anchor = window.parent.document.getElementById('results_anchor');
  if (anchor) { anchor.scrollIntoView({behavior: 'smooth', block: 'start'}); }
</script>
""",
        height=0,
    )
