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
        
        # --- MEMORY PROTECTION ARGS ---
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--no-sandbox',
                '--disable-dev-shm-usage', # Prevents crashes
                '--disable-gpu'
            ]
        )
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        for case in cases:
            case_label = f"{case['name']} {case['no']}/{case['year']}"
            update_terminal(f"\n📂 PROCESSING: {case_label}", terminal_placeholder, logs)
            
            success = False
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    try: page.goto(URL, timeout=60000)
                    except: continue

                    page.locator("#leftPaneMenuCS").click()
                    try:
                        if page.locator("button[data-bs-dismiss='modal']").is_visible():
                            page.locator("button[data-bs-dismiss='modal']").click()
                    except: pass

                    page.select_option("#sess_state_code", value="1")
                    page.wait_for_timeout(1000)
                    page.select_option("#court_complex_code", value="1")
                    page.wait_for_timeout(1000)

                    if page.locator("#CScaseNumber").is_visible():
                        page.locator("#CScaseNumber").click()
                    
                    page.select_option("#case_type", value=case['value'])
                    page.locator("#search_case_no").fill(case['no'])
                    page.locator("#rgyear").fill(case['year'])

                    code = solve_captcha(page)
                    if not code:
                        update_terminal("⚠️ Captcha blurry. Retrying...", terminal_placeholder, logs)
                        page.reload()
                        continue
                    
                    page.locator("#captcha").fill(code)
                    page.locator("#goResetDiv input[value='Go']").click()
                    
                    try: page.wait_for_selector("#dispTable, text=Invalid Captcha", timeout=15000)
                    except: continue

                    if page.locator("text=Invalid Captcha").is_visible():
                        update_terminal("❌ Invalid Captcha.", terminal_placeholder, logs)
                        continue
                    
                    page.locator("#dispTable a[onclick*='viewHistory']").first.click()
                    page.wait_for_selector(".order_table", state="visible", timeout=20000)
                    
                    date_str, rel_link = get_latest_order_link(page.content())
                    
                    if date_str:
                        full_url = f"https://hcservices.ecourts.gov.in/hcservices/{rel_link}"
                        update_terminal(f"📄 Found Link: {date_str}", terminal_placeholder, logs)
                        
                        response = page.request.get(full_url)
                        content_type = response.headers.get("content-type", "")
                        
                        # --- VALIDATE PDF ---
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
                    update_terminal(f"⚠️ Attempt {attempt} Error: {e}", terminal_placeholder, logs)
                    time.sleep(2)
            
            if not success: update_terminal("❌ Failed after retries.", terminal_placeholder, logs)
            time.sleep(1)

        browser.close()
        update_terminal("\n🏁 Finished!", terminal_placeholder, logs)
        return results

# --- UI ---
st.set_page_config(page_title="High Court Bot", layout="wide")
st.title("⚖️ High Court Automation (Hugging Face Edition)")

# You can edit your cases here
CASES = [
    {"name": "Second Appeal", "value": "4", "no": "508", "year": "1999"},
    {"name": "Writ Petition", "value": "1", "no": "11311", "year": "2025"}
]

if st.button("🚀 Fetch Orders"):
    terminal = st.empty()
    results = run_bot(CASES, terminal)
    
    if results:
        st.markdown("---")
        st.success(f"Fetched {len(results)} Orders!")
        for res in results:
            with st.expander(f"📄 {res['desc']}", expanded=True):
                # Download Button
                st.download_button(
                    label="⬇️ Download PDF",
                    data=res['data'],
                    file_name=f"{res['label'].replace('/', '_')}.pdf",
                    mime="application/pdf"
                )
                # Preview
                b64_pdf = base64.b64encode(res['data']).decode('utf-8')
                pdf_display = f'<iframe src="data:application/pdf;base64,{b64_pdf}" width="100%" height="500"></iframe>'
                st.markdown(pdf_display, unsafe_allow_html=True)