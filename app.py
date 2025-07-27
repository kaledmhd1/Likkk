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


# ============= تحميل الحسابات =============
def load_accounts():
    if not os.path.exists(ACCS_FILE):
        logging.error(f"{ACCS_FILE} not found!")
        return {}
    with open(ACCS_FILE, "r", encoding="utf-8") as f:
        content = f.read().strip()
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
        if response.status_code == 200:
            token = response.json().get("token")
            logging.info(f"[get_jwt] JWT for {uid}: {token}")
            return token
        else:
            logging.error(f"[get_jwt] Failed for {uid}, status: {response.status_code}, response={response.text}")
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

    # إعادة التحديث بعد ساعة
    threading.Timer(3600, refresh_tokens).start()


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


# ============= الراوت =============
@app.route('/like', methods=['GET'])
def handle_like():
    try:
        uid = request.args.get("uid")
        server_name = request.args.get("server_name", "").upper()
        key = request.args.get("key")

        if key != "jenil":
            return jsonify({"error": "Invalid or missing API key 🔑"}), 403
        if not uid or not server_name:
            return jsonify({"error": "UID and server_name are required"}), 400

        uid_int = int(uid)
        today_midnight = get_today_midnight_timestamp()

        with LOCK:
            if not TOKENS:
                return jsonify({"error": "No tokens loaded yet. Please wait."}), 503
            token = list(TOKENS.values())[0]  # استخدام أول توكن

        count, last_reset = token_tracker[token]
        if last_reset < today_midnight:
            token_tracker[token] = [0, time.time()]
            count = 0

        if count >= KEY_LIMIT:
            return jsonify({
                "error": "Daily request limit reached for this key.",
                "status": 429,
                "remains": f"(0/{KEY_LIMIT})"
            }), 429

        before = make_request(uid_int, server_name)
        if "error" in before:
            return jsonify({"error": "Failed to get player info before liking.", "debug": before})

        before_like = int(before.get('basicInfo', {}).get('liked', 0))
        name = before.get('basicInfo', {}).get('nickname', 'Unknown')

        url_like = "https://client.me.freefiremobile.com/LikeProfile"
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(send_multiple_requests(uid, token, url_like))
        finally:
            loop.close()

        after = make_request(uid_int, server_name)
        if "error" in after:
            return jsonify({"error": "Failed to get player info after liking.", "debug": after})

        after_like = int(after.get('basicInfo', {}).get('liked', 0))
        like_given = after_like - before_like
        status = 1 if like_given != 0 else 2

        if like_given > 0:
            token_tracker[token][0] += 1
            count += 1

        remains = KEY_LIMIT - count
        return jsonify({
            "LikesGivenByAPI": like_given,
            "LikesafterCommand": after_like,
            "LikesbeforeCommand": before_like,
            "PlayerNickname": name,
            "UID": uid_int,
            "status": status,
            "remains": f"({remains}/{KEY_LIMIT})"
        })

    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


# ============= تشغيل السيرفر =============
if __name__ == "__main__":
    logging.info("=== بدء تشغيل السيرفر ===")
    logging.info(f"[main] Working Dir: {os.getcwd()}")
    logging.info(f"[main] accs.txt موجود؟ {os.path.exists(ACCS_FILE)}")

    refresh_tokens()  # جلب التوكنات عند البداية
    app.run(host="0.0.0.0", port=5000)
