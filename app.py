from flask import Flask, request, jsonify
import threading
import time
import json
import requests
from pathlib import Path
import logging
import asyncio
import aiohttp
from collections import defaultdict
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

ACCS_FILE = Path("accs.txt")  # ملف uid: password بصيغة JSON
TOKEN_REFRESH_INTERVAL = 3600  # 1 ساعة
KEY_LIMIT = 150

# في الذاكرة: dict uid -> token
tokens_cache = {}
# لتعقب عدد الطلبات اليومية لكل token
token_tracker = defaultdict(lambda: [0, time.time()])

stop_token_thread_event = threading.Event()
token_thread = None

def fetch_jwt(uid: str, password: str, timeout=10):
    url = "https://jwt-gen-api-v2.onrender.com/token"
    try:
        r = requests.get(url, params={"uid": uid, "password": password}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if "token" not in data:
            logging.error(f"API لم يعطِ 'token' للـ uid={uid}, الرد: {data}")
            return None
        return data["token"]
    except Exception as e:
        logging.error(f"خطأ أثناء جلب التوكن للـ uid={uid}: {e}")
        return None

def load_accs():
    if not ACCS_FILE.exists():
        logging.error(f"{ACCS_FILE} غير موجود!")
        return {}
    try:
        with open(ACCS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error(f"فشل قراءة {ACCS_FILE}: {e}")
        return {}

def renew_tokens():
    logging.info("بدء تجديد التوكنات...")
    accs = load_accs()
    if not accs:
        logging.warning("لا حسابات في accs.txt")
        return
    new_tokens = {}
    for uid, password in accs.items():
        token = fetch_jwt(uid, password)
        if token:
            new_tokens[uid] = token
    if new_tokens:
        global tokens_cache
        tokens_cache = new_tokens
        logging.info(f"تم تحديث التوكنات لعدد {len(new_tokens)} حسابات")
        logging.info(f"التوكنات المحملة: {list(tokens_cache.values())}")
    else:
        logging.warning("لم يتم الحصول على أي توكن صالح")

def _token_refresher_loop(stop_event):
    while not stop_event.wait(TOKEN_REFRESH_INTERVAL):
        try:
            renew_tokens()
        except Exception as e:
            logging.error(f"خطأ أثناء تجديد التوكنات: {e}")

def ensure_token_thread_started():
    global token_thread
    if token_thread is None or not token_thread.is_alive():
        logging.info("بدء خيط تجديد التوكنات")
        token_thread = threading.Thread(target=_token_refresher_loop, args=(stop_token_thread_event,), daemon=True)
        token_thread.start()

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
    tasks = []
    for _ in range(100):
        tasks.append(send_request(uid, token, url))
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

        if not tokens_cache:
            return jsonify({"error": "No tokens loaded, please wait or check accs.txt"}), 503

        # اختيار أول توكن موجود في الذاكرة
        token = list(tokens_cache.values())[0]

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

        url_like = "https://clientbp.ggblueshark.com/LikeProfile"

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
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    renew_tokens()  # أول تجديد + لوج الطباعة
    ensure_token_thread_started()
    app.run(host="0.0.0.0", port=5000)
