import re
import json
import uuid
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler

session = requests.Session()

MOBILE_UA = (
    "Mozilla/5.0 (Linux; Android 8.1.0; MI 8 Build/OPM1.171019.011) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/69.0.3497.86 Mobile Safari/537.36"
)
DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
BASE = {
    "User-Agent": MOBILE_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}


def extract_dtsg(html):
    for pat in [
        r'name="fb_dtsg" value="([^"]+)"',
        r'"DTSGInitialData",\[\],\{"token":"([^"]+)"',
        r'"DTSGInitialData":\{"token":"([^"]+)"',
        r'"token":"(AQ[^"]{10,})"',
        r'fb_dtsg=([^&"\']+)',
        r'"fb_dtsg","([^"]+)"',
        r'dtsg_ag=\{"token":"([^"]+)"',
    ]:
        m = re.search(pat, html)
        if m and len(m.group(1)) > 10:
            return m.group(1)
    return None


def extract_lsd(html):
    m = re.search(r'name="lsd" value="([^"]+)"', html)
    if m: return m.group(1)
    m = re.search(r'\["LSD",\[\],\{"token":"(.+?)"\}\]', html)
    if m: return m.group(1)
    m = re.search(r'"LSD",\[\],\{"token":"([^"]+)"', html)
    if m: return m.group(1)
    return None


def get_token(cookie):
    headers = {**BASE, "Cookie": cookie, "Host": "business.facebook.com", "Origin": "https://business.facebook.com", "Referer": "https://www.facebook.com/"}
    r = session.get("https://business.facebook.com/business_locations", headers=headers, timeout=20)
    m = re.search(r"(EAAG\w+)", r.text)
    if not m:
        raise ValueError("Invalid cookie or session expired.")
    return m.group(1), r.text


def get_user(token, cookie):
    r = session.get(
        f"https://b-graph.facebook.com/me?fields=name,id,picture.width(200).height(200)&access_token={token}",
        headers={**BASE, "Cookie": cookie},
        timeout=15,
    )
    data = r.json()
    if "error" in data:
        raise ValueError(data["error"].get("message", "Failed to get user info."))
    pic = data.get("picture", {}).get("data", {}).get("url", "")
    return data.get("name", "Unknown"), data.get("id", ""), pic


def get_dtsg_lsd(cookie, fallback_html=""):
    if fallback_html:
        dtsg = extract_dtsg(fallback_html)
        lsd  = extract_lsd(fallback_html)
        if dtsg: return dtsg, lsd

    dh = {"User-Agent": DESKTOP_UA, "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8", "Accept-Language": "en-US,en;q=0.9", "Cookie": cookie, "Referer": "https://www.facebook.com/"}
    for url in ["https://www.facebook.com/", "https://www.facebook.com/profile.php", "https://m.facebook.com/"]:
        try:
            r = session.get(url, headers=dh, timeout=15, allow_redirects=True)
            dtsg = extract_dtsg(r.text)
            lsd  = extract_lsd(r.text)
            if dtsg: return dtsg, lsd
        except Exception:
            continue
    return None, None


def toggle_shield(cookie, uid, dtsg, lsd, enable):
    jazoest = "2" + str(sum(ord(c) for c in dtsg))
    variables = {"input": {"is_shielded": enable, "session_id": str(uuid.uuid4()), "actor_id": uid, "client_mutation_id": str(uuid.uuid4())}}
    payload = {
        "av": uid, "__user": uid, "__a": "1",
        "fb_dtsg": dtsg,
        "fb_api_caller_class": "RelayModern",
        "fb_api_req_friendly_name": "IsShieldedSetMutation",
        "variables": json.dumps(variables),
        "server_timestamps": "true",
        "doc_id": "1477043292367183",
        "jazoest": jazoest,
    }
    if lsd: payload["lsd"] = lsd
    headers = {**BASE, "Cookie": cookie, "Content-Type": "application/x-www-form-urlencoded", "Host": "www.facebook.com", "Origin": "https://www.facebook.com", "Referer": "https://www.facebook.com/", "X-FB-Friendly-Name": "IsShieldedSetMutation"}
    r = session.post("https://www.facebook.com/api/graphql/", data=payload, headers=headers, timeout=20)
    if r.status_code == 429:
        raise ValueError("Rate limited. Wait a moment.")
    if not r.ok:
        raise ValueError(f"HTTP {r.status_code} from Facebook.")
    text = r.text.replace("for (;;);", "").strip()
    result = json.loads(text)
    if "errors" in result and result["errors"]:
        raise ValueError(result["errors"][0].get("message", "Facebook error."))
    return True


class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        print(f"  [{self.address_string()}] {fmt % args}")

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path == "/ping":
            self._json({"status": "ok"})
        else:
            self._json({"error": "Not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(length))
        except Exception:
            self._json({"error": "Invalid JSON"}, 400)
            return
        if self.path == "/api/login":
            self._login(body)
        elif self.path == "/api/toggle":
            self._toggle(body)
        else:
            self._json({"error": "Not found"}, 404)

    def _login(self, body):
        cookie = (body.get("cookie") or "").strip()
        if not cookie:
            self._json({"error": "Cookie is required."})
            return
        try:
            token, biz_html = get_token(cookie)
            name, uid, pic  = get_user(token, cookie)
            dtsg, lsd       = get_dtsg_lsd(cookie, fallback_html=biz_html)
            if not dtsg:
                self._json({"error": "Could not extract dtsg token. Cookie may be restricted."})
                return
            self._json({"ok": True, "name": name, "uid": uid, "picture": pic, "dtsg": dtsg, "lsd": lsd or "", "token": token})
        except ValueError as e:
            self._json({"error": str(e)})
        except requests.exceptions.ConnectionError:
            self._json({"error": "No internet connection."})
        except requests.exceptions.Timeout:
            self._json({"error": "Request timed out."})
        except Exception as e:
            self._json({"error": f"Error: {e}"})

    def _toggle(self, body):
        cookie = (body.get("cookie") or "").strip()
        uid    = (body.get("uid") or "").strip()
        dtsg   = (body.get("dtsg") or "").strip()
        lsd    = body.get("lsd") or ""
        enable = bool(body.get("enable", True))
        if not all([cookie, uid, dtsg]):
            self._json({"error": "Missing required fields."})
            return
        try:
            toggle_shield(cookie, uid, dtsg, lsd, enable)
            self._json({"ok": True, "shielded": enable})
        except ValueError as e:
            self._json({"error": str(e)})
        except Exception as e:
            self._json({"error": f"Error: {e}"})

    def _json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    PORT = 5000
    httpd = HTTPServer(("0.0.0.0", PORT), Handler)
    print(f"\n  Avatar Guard API  —  http://localhost:{PORT}\n")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
