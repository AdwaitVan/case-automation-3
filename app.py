import streamlit as st
from playwright.sync_api import sync_playwright
import time
import base64
from bs4 import BeautifulSoup
from datetime import datetime
import ddddocr
import io

# --- CONFIG ---
URL = "https://hcservices.ecourts.gov.in/hcservices/main.php"
MAX_RETRIES = 5

# --- HELPER FUNCTIONS ---
def update_terminal(message, placeholder, logs):
    now = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{now}] {message}")
    placeholder.code("\n".join(logs), language="bash")

def solve_captcha(page):
    try:
        page.wait_for_selector("#captcha_image", state="visible", timeout=3000)
        time.sleep(1)
        captcha_img = page.locator("#captcha_image")
        captcha_bytes = captcha_img.screenshot()
        ocr = ddddocr.DdddOcr(show_ad=False)
        code = ocr.classification(captcha_bytes)
        return code if len(code) == 6 else ""
    except: return ""

def get_latest_order_link(html_content):
    soup = BeautifulSoup(html_content, "html.parser")
    table = soup.find("table", class_="order_table")
    if not table: return None, None
    orders = []
    for row in table.find_all("tr")[1:]:
        cols = row.find_all("td")
        if len(cols) < 5: continue
        date_text = cols[3].get_text(strip=True)
        link_tag = cols[4].find("a")
        if date_text and link_tag:
            try:
                dt_obj = datetime.strptime(date_text, "%d-%m-%Y")
                orders.append((dt_obj, link_tag.get("href")))
            except: continue
    if not orders: return None, None
    orders.sort(key=lambda x: x[0], reverse=True)
    return orders[0][0].strftime("%d-%m-%Y"), orders[0][1]

def run_bot(cases, terminal_placeholder):
    logs = []
    results = []
    
    with sync_playwright() as p:
        update_terminal("🚀 Starting Cloud Robot...", terminal_placeholder, logs)
        
        # --- CLOUD OPTIMIZED BROWSER ---
        browser = p.chromium.launch(
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu']
        )
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        for case in cases:
            case_label = f"{case['name']} {case['no']}/{case['year']}"
            update_terminal(f"\n📂 PROCESSING: {case_label}", terminal_placeholder, logs)
            
            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    # 1. Load Page
                    try: page.goto(URL, timeout=60000)
                    except: continue

                    # --- RESTORED POPUP KILLER ---
                    # The logs showed "#bs_alert" was blocking clicks.
                    # We blindly try to close it if it exists.
                    time.sleep(2)
                    try:
                        if page.locator("#bs_alert").is_visible():
                            update_terminal("🧹 Closing '#bs_alert' popup...", terminal_placeholder, logs)
                            # Try clicking the 'X' button inside the alert
                            if page.locator("#bs_alert button.close").is_visible():
                                page.locator("#bs_alert button.close").click()
                            else:
                                # Fallback: Click the body to dismiss or standard modal close
                                page.locator("button[data-bs-dismiss='modal']").click()
                            time.sleep(1)
                    except: pass
                    # -----------------------------

                    # 2. Open Left Menu
                    page.locator("#leftPaneMenuCS").click()

                    # 3. Select High Court
                    page.select_option("#sess_state_code", value="1")
                    time.sleep(1)
                    page.select_option("#court_complex_code", value="1") 
                    time.sleep(1)

                    # 4. Click Case Number (WITH FORCE=TRUE)
                    # force=True tells Playwright to ignore the popup if it's still hovering
                    if page.locator("#CScaseNumber").is_visible():
                        page.locator("#CScaseNumber").click(force=True)

                    # 5. Fill Details
                    page.select_option("#case_type", value=case['value'])
                    page.locator("#search_case_no").fill(case['no'])
                    page.locator("#rgyear").fill(case['year'])

                    # 6. Captcha
                    code = solve_captcha(page)
                    if not code:
                        update_terminal("⚠️ Captcha blurry. Retrying...", terminal_placeholder, logs)
                        page.reload()
                        continue
                    
                    page.locator("#captcha").fill(code)
                    page.locator("#goResetDiv input[value='Go']").click()
                    
                    # 7. Check for Invalid Captcha
                    try: 
                        page.wait_for_selector("text=Invalid Captcha", timeout=3000)
                        update_terminal("❌ Invalid Captcha. Retrying...", terminal_placeholder, logs)
                        continue
                    except: pass 

                    # 8. Extract Result
                    page.locator("#dispTable a[onclick*='viewHistory']").first.click()
                    page.wait_for_selector(".order_table", state="visible", timeout=20000)
                    
                    date_str, rel_link = get_latest_order_link(page.content())
                    
                    if date_str:
                        full_url = f"https://hcservices.ecourts.gov.in/hcservices/{rel_link}"
                        update_terminal(f"📄 Found Link: {date_str}", terminal_placeholder, logs)
                        
                        response = page.request.get(full_url)
                        content_type = response.headers.get("content-type", "")
                        
                        if response.status == 200 and "application/pdf" in content_type:
                            results.append({
                                "label": f"{case['no']}/{case['year']}",
                                "desc": f"{case['name']} (Order: {date_str})",
                                "data": response.body()
                            })
                            update_terminal("✅ Downloaded Successfully!", terminal_placeholder, logs)
                            success = True
                            break
                        else:
                            update_terminal("⚠️ Website Error: Order listed but file is broken/missing.", terminal_placeholder, logs)
                            success = True 
                            break
                    else:
                        update_terminal("⚠️ No orders found.", terminal_placeholder, logs)
                        success = True
                        break

                except Exception as e:
                    # Simple error logging to avoid clutter
                    msg = str(e).split("\n")[0] # Only show the first line of error
                    update_terminal(f"⚠️ Retry {attempt}: {msg}", terminal_placeholder, logs)
                    time.sleep(2)
            
            if not success: update_terminal("❌ Failed after retries.", terminal_placeholder, logs)
            time.sleep(1)

        browser.close()
        update_terminal("\n🏁 Finished!", terminal_placeholder, logs)
        return results

# --- UI ---
st.set_page_config(page_title="High Court Bot", layout="wide")
st.title("⚖️ High Court Automation")

CASES = [
    {"name": "Second Appeal", "value": "4", "no": "508", "year": "1999"},
    {"name": "Writ Petition", "value": "1", "no": "11311", "year": "2025"}
]

if st.button("🚀 Fetch Orders"):
    terminal = st.empty()
    results = run_bot(CASES, terminal)
    
    if results:
        st.markdown("---")
        for res in results:
            with st.expander(f"📄 {res['desc']}", expanded=True):
                st.download_button(
                    label="⬇️ Download PDF",
                    data=res['data'],
                    file_name=f"{res['label'].replace('/', '_')}.pdf",
                    mime="application/pdf"
                )
                b64_pdf = base64.b64encode(res['data']).decode('utf-8')
                pdf_display = f'<iframe src="data:application/pdf;base64,{b64_pdf}" width="100%" height="500"></iframe>'
                st.markdown(pdf_display, unsafe_allow_html=True)