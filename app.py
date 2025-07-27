from flask import Flask, jsonify
import threading
import time
import json
import logging
import os
import signal
from types import MappingProxyType
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ============= إعداد اللوج =============
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

# ============= إعدادات أساسية =============
app = Flask(__name__)
ACCS_FILE = os.getenv("ACCS_FILE", "accs.txt")  # ملف الحسابات
TOKENS = MappingProxyType({})  # التوكنات في الذاكرة (قراءة فقط)
LOCK = threading.Lock()
STOP = threading.Event()

# ============= إعداد requests مع retry =============
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
        logging.error(f"ملف {ACCS_FILE} غير موجود")
        return {}
    try:
        with open(ACCS_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            data = json.loads(content or "{}")
        return data if isinstance(data, dict) else {}
    except Exception as e:
        logging.error(f"[ERROR] فشل قراءة الملف {ACCS_FILE}: {e}")
        return {}


# ============= جلب JWT من API =============
def get_jwt(uid, password):
    api_url = f"https://jwt-gen-api-v2.onrender.com/token?uid={uid}&password={password}"
    try:
        response = session.get(api_url, verify=False, timeout=30)
        if response.status_code == 200:
            token = response.json().get("token")
            logging.info(f"[get_jwt] {uid} -> Token OK")
            return token
        else:
            logging.error(f"[get_jwt] فشل {uid}, status: {response.status_code}")
    except Exception as e:
        logging.error(f"[get_jwt] Exception: {e}")
    return None


# ============= تحديث التوكنات =============
def refresh_tokens():
    logging.info("[refresh_tokens] تحديث التوكنات...")
    accounts = load_accounts()
    new_tokens = {}
    for uid, pw in accounts.items():
        token = get_jwt(uid, pw)
        if token:
            new_tokens[uid] = token
        else:
            logging.warning(f"[FAILED] {uid}")

    with LOCK:
        global TOKENS
        TOKENS = MappingProxyType(new_tokens)

    logging.info(f"[refresh_tokens] عدد التوكنات: {len(TOKENS)}")


# ============= تحديث التوكنات كل ساعة =============
def refresh_tokens_loop():
    while not STOP.is_set():
        refresh_tokens()
        STOP.wait(3600)  # انتظر ساعة


# ============= المسارات =============
@app.route("/")
def home():
    with LOCK:
        return jsonify({"status": "live", "tokens_loaded": len(TOKENS)})


@app.route("/tokens", methods=["GET"])
def show_tokens():
    with LOCK:
        return jsonify(dict(TOKENS) if TOKENS else {"error": "لا يوجد توكنات بعد"})


@app.route("/force_refresh", methods=["POST", "GET"])
def force_refresh():
    refresh_tokens()
    return jsonify({"status": "تم التحديث", "tokens_loaded": len(TOKENS)})


# ============= الإغلاق الآمن =============
def handle_shutdown(signum, frame):
    logging.info("إيقاف السيرفر...")
    STOP.set()


# ============= تشغيل السيرفر =============
if __name__ == "__main__":
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    # تحديث التوكنات مباشرة عند بدء التشغيل
    refresh_tokens()

    # تشغيل التحديث الدوري في ثريد مستقل
    t = threading.Thread(target=refresh_tokens_loop, daemon=True)
    t.start()

    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
