import base64
import re
from pydantic import BaseModel
import requests
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from bs4 import BeautifulSoup

app = FastAPI()

# Setup templates and static files
templates = Jinja2Templates(directory="templates")

session_data = {}

async def create_playwright_browser(headless=True):
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(headless=headless)
    return browser, playwright

async def capture_screenshot(session_id):
    if session_id not in session_data:
        return None
    if session_data[session_id].get("page"):
        session_data[session_id]['screenshot'] = await session_data[session_id]["page"].screenshot()

async def navigate_to_login_page(username, password, session_id):
    login_url = ('https://login.hs-heilbronn.de/realms/hhn/protocol/openid-connect/auth'
                 '?response_mode=form_post&response_type=id_token&redirect_uri=https%3A%2F%2Filias.hs-heilbronn.de%2Fopenidconnect.php'
                 '&client_id=hhn_common_ilias&nonce=badc63032679bb541ff44ea53eeccb4e&state=2182e131aa3ed4442387157cd1823be0&scope=openid+openid')
    await session_data[session_id]['page'].goto(login_url)
    await session_data[session_id]['page'].fill('input[name="username"]', username)
    await session_data[session_id]['page'].fill('input[name="password"]', password)
    await session_data[session_id]['page'].click('input[name="login"]')
    await session_data[session_id]['page'].wait_for_selector('a[id="try-another-way"]', timeout=6000)
    await session_data[session_id]['page'].click('a[id="try-another-way"]')
    await session_data[session_id]['page'].wait_for_selector("button[name='authenticationExecution']:has-text('Enter a verification code from authenticator application.')", timeout=6000)
    await session_data[session_id]['page'].click("button[name='authenticationExecution']:has-text('Enter a verification code from authenticator application.')")

async def wait_for_dashboard(page):
    """Waits until redirected to the dashboard after login."""
    try:
        await page.wait_for_url("**/ilias.php?baseClass=ilDashboardGUI&cmd=jumpToSelectedItems", timeout=60000)  # Wait up to 60 seconds
        print("Login successful. Redirecting to the target URL...")
    except PlaywrightTimeoutError:
        raise Exception("Login did not complete within the expected time.")

async def navigate_to_main_courses_page(page):
    target_url = 'https://ilias.hs-heilbronn.de/ilias.php?cmdClass=ilmembershipoverviewgui&cmdNode=jr&baseClass=ilmembershipoverviewgui'
    await page.goto(target_url)

def extract_courses(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    courses = []

    def get_ref_id(url):
        match = re.search(r'ref_id=(\d+)', url)
        return match.group(1) if match else ''

    course_rows = soup.select('.il-std-item')
    for course_row in course_rows:
        img_element = course_row.select_one('img.icon')
        if img_element and img_element.get('alt') != 'Symbol Gruppe':
            course_name_element = course_row.select_one('.il-item-title a')
            if course_name_element:
                course_name = course_name_element.get_text(strip=True)
                course_url = course_name_element.get('href')
                course_ref_id = get_ref_id(course_url)
                courses.append({
                    'name': course_name,
                    'refId': course_ref_id,
                    'url': course_url,
                })

    return courses

def extract_email_column_from_table(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    table = soup.find('table', {'class': 'table table-striped fullwidth'})
    email_column_data = []

    if table and table.find('tbody'):
        for row in table.find('tbody').find_all('tr'):
            columns = row.find_all('td')
            if len(columns) >= 5:
                email_column_data.append(columns[4].text.strip())

    return email_column_data

def send_data_to_matrix_server(user_id, room_name):
    url = "http://unifyhn.de/add_user_to_rooms"
    headers = {"Content-Type": "application/json"}
    data = {
        "user_id": "@" + user_id + ":unifyhn.de",
        "rooms": [{"room_name": room_name}]
    }
    response = requests.post(url, json=data, headers=headers)
    return response

async def visit_course_page_and_scrape(page, course):
    dynamic_url = f"https://ilias.hs-heilbronn.de/ilias.php?baseClass=ilrepositorygui&cmdNode=yc:ml:95&cmdClass=ilCourseMembershipGUI&ref_id={course['refId']}"
    print(f"Visiting dynamic URL: {dynamic_url}")
    await page.goto(dynamic_url)

    course_html_content = await page.content()
    emails = extract_email_column_from_table(course_html_content)
    print(f"Email Column Data for {course['name']}:", emails)

    return course_html_content, emails

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/sync", response_class=HTMLResponse)
async def sync(request: Request):
    response = send_data_to_matrix_server('demo_user_1', 'DemoRoom500')
    print('Response Status Code:', response.status_code, flush=True)
    print('Response Content:', response.text, flush=True)
    try:
        print('Response JSON Content:', response.json(), flush=True)
    except ValueError:
        print('Response is not in JSON format', flush=True)
    return templates.TemplateResponse("login.html", {"request": request})

async def perform_sync_thread(session_id, username, password):
    try:
        browser, playwright = await create_playwright_browser(headless=False)
        page = await browser.new_page()
        session_data[session_id] = {
            'browser': browser,
            'page': page,
            'playwright': playwright,
            'screenshot': None,
            'otp_required': False
        }
        await capture_screenshot(session_id)
        await navigate_to_login_page(username, password, session_id)
        await capture_screenshot(session_id)

        # Check for OTP field
        try:
            await session_data[session_id]["page"].wait_for_selector('input[name="otp"]', timeout=3000)
            session_data[session_id]['otp_required'] = True
        except PlaywrightTimeoutError:
            await cleanup_session(session_id)
            print("Login failed: Invalid credentials")
        print(f"Thread {session_id} completed initial sync.")
    except Exception as e:
        await cleanup_session(session_id)
        print(f"Error in thread {session_id}: {str(e)}")

# Define a Pydantic model for the POST request body
class SyncRequest(BaseModel):
    username: str
    password: str

@app.post("/perform-sync")
async def perform_sync(sync_request: SyncRequest):
    username = sync_request.username
    password = sync_request.password

    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required")

    session_id = base64.urlsafe_b64encode(username.encode()).decode()
    await perform_sync_thread(session_id, username, password)
    return JSONResponse({"status": "otp_check_started", "session_id": session_id})

@app.get("/submit-otp")
async def submit_otp(otp: str = Query(...), session_id: str = Query(...)):
    if not otp or not session_id:
        raise HTTPException(status_code=400, detail="OTP and session ID are required")

    if session_id not in session_data:
        raise HTTPException(status_code=404, detail="Invalid session ID")

    # Check if OTP is required and thread is running
    if session_data[session_id]['otp_required']:
        try:
            await session_data[session_id]["page"].fill('input[name="otp"]', otp)
            await capture_screenshot(session_id)
            await session_data[session_id]["page"].click('input[type="submit"]')
            await capture_screenshot(session_id)
            await wait_for_dashboard(session_data[session_id]["page"])
            return await process_courses(session_id)
        except Exception as e:
            await cleanup_session(session_id)
            return JSONResponse({"status": "error", "message": str(e)}, status_code=500)
    else:
        return JSONResponse({"status": "error", "message": "OTP not required or session expired"}, status_code=400)

async def process_courses(session_id):
    await navigate_to_main_courses_page(session_data[session_id]["page"])

    html_content = await session_data[session_id]["page"].content()
    courses = extract_courses(html_content)
    print('Extracted Courses:', courses)

    all_email_column_data = []
    for course in courses:
        try:
            course_html_content, emails = await visit_course_page_and_scrape(session_data[session_id]["page"], course)
            all_email_column_data.append({
                'course_name': course['name'],
                'course_ref_id': course['refId'],
                'emails': emails
            })
        except Exception as e:
            print(f"Error scraping course {course['name']}: {str(e)}")

    await cleanup_session(session_id)
    return JSONResponse({"status": "success", "data": all_email_column_data})

@app.get("/screenshot")
async def get_screenshot(session_id: str = Query(...)):
    """Retrieve the latest screenshot for the given session ID."""
    if session_id not in session_data or 'screenshot' not in session_data[session_id]:
        raise HTTPException(status_code=404, detail="Screenshot not found for the provided session ID")

    screenshot_data = session_data[session_id]['screenshot']
    # Encode screenshot to base64 for returning as JSON
    screenshot_base64 = base64.b64encode(screenshot_data).decode('utf-8')
    return JSONResponse({"screenshot": f"data:image/png;base64,{screenshot_base64}"})


async def cleanup_session(session_id):
    if session_id in session_data:
        await session_data[session_id]['page'].close()
        await session_data[session_id]['browser'].close()
        await session_data[session_id]['playwright'].stop()
        del session_data[session_id]

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
