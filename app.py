from flask import Flask, request, jsonify
import asyncio
import aiohttp
import requests
import json
import time
from collections import defaultdict
from datetime import datetime
import traceback

app = Flask(__name__)

KEY_LIMIT = 150
token_tracker = defaultdict(lambda: [0, time.time()])

def get_today_midnight_timestamp():
    now = datetime.now()
    midnight = datetime(now.year, now.month, now.day)
    return midnight.timestamp()

def load_tokens(server_name):
    if server_name == "ME":
        with open("token_ind.json", "r") as f:
            return json.load(f)
    elif server_name in {"BR", "US", "SAC", "NA"}:
        with open("token_br.json", "r") as f:
            return json.load(f)
    else:
        with open("token_bd.json", "r") as f:
            return json.load(f)

def make_request(uid, region):
    url = f"https://razor-info.vercel.app/player-info?uid={uid}&region={region.lower()}"
    response = requests.get(url)
    if response.status_code != 200:
        return {"error": f"Server returned {response.status_code}", "raw_response": response.text}
    try:
        return response.json()
    except Exception as e:
        return {"error": "Failed to parse JSON response", "debug": str(e), "raw_response": response.text}

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
    for i in range(100):
        token = tokens[i % len(tokens)]["token"]
        tasks.append(send_request(uid, token, url))
    results = await asyncio.gather(*tasks)
    return results

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

            before_like = int(before.get('likes', 0))
            name = before.get('name', 'Unknown')

            if server_name == "ME":
                url = "https://client.me.freefiremobile.com/LikeProfile"
            elif server_name in {"BR", "US", "SAC", "NA"}:
                url = "https://client.us.freefiremobile.com/LikeProfile"
            else:
                url = "https://clientbp.ggblueshark.com/LikeProfile"

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(send_multiple_requests(uid, server_name, url))
            loop.close()

            after = make_request(uid_int, server_name)
            if "error" in after:
                return {"error": "Failed to get player info after liking.", "debug": after}

            after_like = int(after.get('likes', 0))
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

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)