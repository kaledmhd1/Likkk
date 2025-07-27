from flask import Flask, request, jsonify
import asyncio
import aiohttp
import requests
import json
import time
from collections import defaultdict
from datetime import datetime
import traceback
import os
import threading
from pathlib import Path
import logging

# ------------------------- إعدادات عامة -------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

app = Flask(__name__)

KEY_LIMIT = 150
token_tracker = defaultdict(lambda: [0, time.time()])

# ملفات التوكنات
ACCS_FILE = Path("accs.txt")               # يحتوي uid -> password
TOKEN_IND_FILE = Path("token_ind.json")    # سيُكتب فيه: [ { "token": "..." }, ... ]
TOKEN_REFRESH_INTERVAL = 3600              # ثانية (ساعة)

# إشارة لإدارة الثريد
stop_token_thread_event = threading.Event()
token_thread = None
# ----------------------------------------------------------------


# ====================== جزء: توليد وتجديد التوكنات ======================

def fetch_jwt(uid: str, password: str, timeout: int = 10):
    url = "https://jwt-gen-api-v2.onrender.com/token"
    try:
        r = requests.get(url, params={"uid": uid, "password": password}, timeout=timeout)
        r.raise_for_status()
        data = r.json()
        if "token" not in data:
            return {"error": f"API لم يعطِ 'token' للـ uid={uid}", "raw": data}
        return data
    except Exception as e:
        return {"error": str(e)}

def renew_me_tokens():
    logging.info("تجديد ME tokens من accs.txt ...")

    if not ACCS_FILE.exists():
        logging.error(f"{ACCS_FILE} غير موجود!")
        return

    try:
        with open(ACCS_FILE, "r", encoding="utf-8") as f:
            accs = json.load(f)  # expected: { "uid": "password", ... }
    except Exception:
        logging.exception("فشل قراءة/بارس accs.txt")
        return

    tokens_out = []
    for uid, password in accs.items():
        resp = fetch_jwt(uid, password)
        if "error" in resp:
            logging.error("فشل جلب التوكن لـ uid=%s | error=%s", uid, resp["error"])
            continue
        tokens_out.append({"token": resp["token"]})

    if not tokens_out:
        logging.warning("لم يتم الحصول على أي توكن صالح؛ لن يتم تحديث token_ind.json")
        return

    tmp_file = TOKEN_IND_FILE.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(tokens_out, f, ensure_ascii=False, indent=4)
        os.replace(tmp_file, TOKEN_IND_FILE)
        logging.info("تم تحديث %s بعدد %d توكن.", TOKEN_IND_FILE, len(tokens_out))
    except Exception:
        logging.exception("فشل في كتابة token_ind.json")

def _token_refresher_loop(stop_event: threading.Event):
    try:
        renew_me_tokens()
    except Exception:
        logging.exception("خطأ أثناء التجديد الأولي للتوكنات")

    while not stop_event.wait(TOKEN_REFRESH_INTERVAL):
        try:
            renew_me_tokens()
        except Exception:
            logging.exception("خطأ أثناء التجديد الدوري للتوكنات")

def ensure_token_thread_started():
    global token_thread
    if token_thread is None or not token_thread.is_alive():
        logging.info("بدء خيط تجديد التوكنات...")
        token_thread = threading.Thread(
            target=_token_refresher_loop,
            args=(stop_token_thread_event,),
            daemon=True
        )
        token_thread.start()

# ==================================================================


# ====================== جزء: الأدوات ========================

def get_today_midnight_timestamp():
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return midnight.timestamp()

def load_tokens(server_name):
    file_map = {
        "ME": "token_ind.json",
        "BR": "token_br.json",
        "US": "token_br.json",
        "SAC": "token_br.json",
        "NA": "token_br.json"
    }
    filename = file_map.get(server_name, "token_bd.json")

    try:
        with open(filename, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                data = [{"token": v} for v in data.values()]
            return data
    except FileNotFoundError:
        logging.error("ملف %s غير موجود.", filename)
        return []
    except json.JSONDecodeError:
        logging.error("فشل تفريغ (parse) %s.", filename)
        return []

def make_request(uid, server_name):
    url = f"https://razor-info.vercel.app/player-info?uid={uid}&region={server_name.lower()}"
    try:
        response = requests.get(url, timeout=10)
        if response.status_code != 200:
            return {"error": f"Server returned {response.status_code}", "raw_response": response.text}
        return response.json()
    except Exception as e:
        return {"error": "Failed to connect or parse response", "debug": str(e)}

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

async def send_multiple_requests(uid, server_name, url):
    tasks = []
    tokens = load_tokens(server_name)
    if not tokens:
        return []  # لا يوجد توكنات
    for i in range(100):
        token = tokens[i % len(tokens)]["token"]
        tasks.append(send_request(uid, token, url))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    return results

# ==================================================================


# ========================= الراوت الأساسي =========================

@app.route('/like', methods=['GET'])
def handle_requests():
    try:
        uid = request.args.get("uid")
        server_name = request.args.get("server_name", "").upper()
        key = request.args.get("key")

        if key != "jenil":
            return jsonify({"error": "Invalid or missing API key 🔑"}), 403

        if not uid or not server_name:
            return jsonify({"error": "UID and server_name are required"}), 400

        def process_request():
            uid_int = int(uid)
            today_midnight = get_today_midnight_timestamp()

            data = load_tokens(server_name)
            if not data:
                return {"error": "No tokens available for this server. (Empty file or failed to load)", "status": 503}

            token = data[0]['token']
            count, last_reset = token_tracker[token]

            if last_reset < today_midnight:
                token_tracker[token] = [0, time.time()]
                count = 0

            if count >= KEY_LIMIT:
                return {
                    "error": "Daily request limit reached for this key.",
                    "status": 429,
                    "remains": f"(0/{KEY_LIMIT})"
                }

            before = make_request(uid_int, server_name)
            if "error" in before:
                return {"error": "Failed to get player info before liking.", "debug": before}

            before_like = int(before.get('basicInfo', {}).get('liked', 0))
            name = before.get('basicInfo', {}).get('nickname', 'Unknown')

            if server_name == "ME":
                url = f"https://client.me.freefiremobile.com/LikeProfile}"
            elif server_name in {"BR", "US", "SAC", "NA"}:
                url = "https://client.us.freefiremobile.com/LikeProfile"
            else:
                url = "https://api.jeff-gherab.xyz/api/like/v1/send"

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(send_multiple_requests(uid, server_name, url))
            finally:
                loop.close()

            after = make_request(uid_int, server_name)
            if "error" in after:
                return {"error": "Failed to get player info after liking.", "debug": after}

            after_like = int(after.get('basicInfo', {}).get('liked', 0))
            like_given = after_like - before_like
            status = 1 if like_given != 0 else 2

            if like_given > 0:
                token_tracker[token][0] += 1
                count += 1

            remains = KEY_LIMIT - count

            return {
                "LikesGivenByAPI": like_given,
                "LikesafterCommand": after_like,
                "LikesbeforeCommand": before_like,
                "PlayerNickname": name,
                "UID": uid_int,
                "status": status,
                "remains": f"({remains}/{KEY_LIMIT})"
            }

        result = process_request()
        return jsonify(result)
    except Exception as e:
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500

# ==================================================================


if __name__ == "__main__":
    ensure_token_thread_started()
    app.run(host="0.0.0.0", port=5000)
