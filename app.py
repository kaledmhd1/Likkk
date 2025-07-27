from flask import Flask, request, jsonify
import threading
import time
import json
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import logging
import asyncio
import aiohttp
from collections import defaultdict
from datetime import datetime
import os
import traceback

# ============= إعداد اللوج =============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ============= إعدادات أساسية =============
app = Flask(__name__)
ACCS_FILE = "accs.txt"  # ملف الحسابات
TOKENS = {}             # التوكنات في الذاكرة
LOCK = threading.Lock()
KEY_LIMIT = 150
token_tracker = defaultdict(lambda: [0, time.time()])

# ============= إعداد requests مع retry =============
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
retry_strategy = Retry(
    total=5,
    backoff_factor=1,
    status_forcelist=[500, 502, 503, 504],
    allowed_methods=["GET"],
)
session = requests.Session()
adapter = HTTPAdapter(max_retries=retry_strategy)
session.mount("https://", adapter)
session.mount("http://", adapter)


# ============= اختبار الاتصال مع API =============
def test_api_connection():
    try:
        url = "https://jwt-gen-api-v2.onrender.com/token?uid=3686947614&password=TEST"
        logging.info(f"[TEST] Testing API connection: {url}")
        r = session.get(url, verify=False, timeout=10)
        logging.info(f"[TEST] Status: {r.status_code}, Response: {r.text[:200]}")
    except Exception as e:
        logging.error(f"[TEST] API connection failed: {e}")


# ============= تحميل الحسابات =============
def load_accounts():
    if not os.path.exists(ACCS_FILE):
        logging.error(f"[DEBUG] {ACCS_FILE} not found in {os.getcwd()}")
        logging.info(f"[DEBUG] Files here: {os.listdir(os.getcwd())}")
        return {}
    with open(ACCS_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
        logging.info(f"[DEBUG] accs.txt content: {content}")
        try:
            data = json.loads(content or "{}")
            logging.info(f"[DEBUG] Loaded {len(data)} accounts from accs.txt")
            return data
        except json.JSONDecodeError as e:
            logging.error(f"[ERROR] Failed to parse JSON: {e}")
            return {}


# ============= جلب JWT من API =============
def get_jwt(uid, password):
    api_url = f"https://jwt-gen-api-v2.onrender.com/token?uid={uid}&password={password}"
    try:
        logging.info(f"[get_jwt] Requesting JWT for {uid}")
        response = session.get(api_url, verify=False, timeout=30)
        logging.info(f"[get_jwt] Status: {response.status_code}, Response: {response.text[:150]}")
        if response.status_code == 200:
            token = response.json().get("token")
            logging.info(f"[get_jwt] JWT for {uid}: {token}")
            return token
        else:
            logging.error(f"[get_jwt] Failed for {uid}, status: {response.status_code}")
    except Exception as e:
        logging.error(f"[get_jwt] Exception for {uid}: {e}")
    return None


# ============= تحديث التوكنات كل ساعة =============
def refresh_tokens():
    logging.info("[refresh_tokens] Starting refresh...")
    accounts = load_accounts()
    new_tokens = {}
    for uid, pw in accounts.items():
        token = get_jwt(uid, pw)
        if token:
            new_tokens[uid] = token
            logging.info(f"[REFRESHED] {uid}")
        else:
            logging.warning(f"[FAILED] {uid}")

    with LOCK:
        global TOKENS
        TOKENS = new_tokens

    logging.info(f"[INFO] Tokens refreshed: {len(TOKENS)} active.")
    if TOKENS:
        logging.info(f"[TOKENS] {TOKENS}")
    else:
        logging.warning("[TOKENS] No tokens loaded!")

    threading.Timer(3600, refresh_tokens).start()  # إعادة التحديث بعد ساعة


# ============= أدوات مساعدة =============
def get_today_midnight_timestamp():
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return midnight.timestamp()


async def send_request(uid, token, url):
    headers = {
        'User-Agent': "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
        'Connection': "Keep-Alive",
        'Accept-Encoding': "gzip",
        'Authorization': f"Bearer {token}",
        'Content-Type': "application/x-www-form-urlencoded",
        'Expect': "100-continue",
        'X-Unity-Version': "2018.4.11f1",
        'X-GA': "v1 1",
        'ReleaseVersion': "OB49"
    }
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data={"uid": uid}, headers=headers) as response:
            return response.status


async def send_multiple_requests(uid, token, url):
    tasks = [send_request(uid, token, url) for _ in range(100)]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results


def make_request(uid, server_name):
    url = f"https://razor-info.vercel.app/player-info?uid={uid}&region={server_name.lower()}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return {"error": f"Server returned {response.status_code}", "raw_response": response.text}
        return response.json()
    except Exception as e:
        return {"error": "Failed to connect or parse response", "debug": str(e)}


# ============= راوت الفحص =============
@app.route("/tokens", methods=["GET"])
def show_tokens():
    with LOCK:
        return jsonify(TOKENS if TOKENS else {"error": "No tokens loaded yet"})


# ============= تشغيل السيرفر =============
if __name__ == "__main__":
    logging.info("=== بدء تشغيل السيرفر (DEBUG MODE) ===")
    logging.info(f"[main] Working Dir: {os.getcwd()}")
    logging.info(f"[main] accs.txt موجود؟ {os.path.exists(ACCS_FILE)}")

    # فحص ملفات المسار
    logging.info(f"[main] Files in dir: {os.listdir(os.getcwd())}")

    # اختبار الاتصال مع API
    test_api_connection()

    # محاولة جلب التوكنات
    refresh_tokens()

    app.run(host="0.0.0.0", port=5000)
