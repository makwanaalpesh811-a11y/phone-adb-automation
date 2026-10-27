#!/usr/bin/env python3
"""
phone.py  -  Phone ko PC jaisa control karo (cursor + keyboard + screen padhna). Hermes ke liye.
Ek hi file: setup wizard + API server + client CLI.  Sirf Python stdlib + adb.

================ PEHLI BAAR SETUP (Termux) ================
  pkg install python android-tools
  Settings > Developer options > Wireless debugging ON
  python phone.py setup          # wizard: pair + connect + token sab khud save karta hai

================ ROZ KA USE ================
  python phone.py serve --bg     # server background me (client khud bhi auto-start kar leta hai)
  python phone.py stop
  python phone.py status

  python phone.py ui [text]      # screen ke elements (text, id, clickable, x,y)  <-- Hermes ke liye best
  python phone.py clicktext "Add Product"   # text dekh ke us pe tap
  python phone.py waittext "Submit" [sec]   # text aane tak wait
  python phone.py shot [file]    # screenshot png
  python phone.py current        # abhi kaunsa app khula hai

  python phone.py move DX DY | set X Y | click [X Y] | dclick [X Y] | long [X Y] [ms]
  python phone.py drag X1 Y1 X2 Y2 [ms] | scroll down|up|left|right [0.1-0.9]
  python phone.py type "text" [--clear]     # --clear = pehle field khali
  python phone.py clear
  python phone.py key ENTER|BACK|HOME|TAB|DEL|ESC|PASTE|COPY|WAKEUP|<keycode>
  python phone.py open com.flipkart.android
  python phone.py wait 1.5

Config ~/.phone_api.json (token, port, adb target) - env set karne ki zarurat nahi.
Hindi/emoji typing: ADBKeyBoard app install + active ho to chalegi, warna error aayega.
"""
import base64
import hmac
import json
import os
import re
import secrets
import shlex
import signal
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = os.path.expanduser("~")
CONF_PATH = os.path.join(HOME, ".phone_api.json")
PID_PATH = os.path.join(HOME, ".phone_api.pid")
LOG_PATH = os.path.join(HOME, "phone_api.log")
HOST = os.environ.get("PHONE_API_HOST", "127.0.0.1")
ALLOW_SHELL = os.environ.get("PHONE_API_ALLOW_SHELL", "0") == "1"
BOUNDS = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")

KEYS = {
    "ENTER": 66, "BACK": 4, "HOME": 3, "TAB": 61, "DEL": 67, "ESC": 111, "SEARCH": 84,
    "APPSWITCH": 187, "VOLUP": 24, "VOLDOWN": 25, "POWER": 26, "SPACE": 62, "UP": 19,
    "DOWN": 20, "LEFT": 21, "RIGHT": 22, "MENU": 82, "PASTE": 279, "COPY": 278, "CUT": 277,
    "MOVE_END": 123, "MOVE_HOME": 122, "PAGEUP": 92, "PAGEDOWN": 93, "WAKEUP": 224,
}

# ------------------------------------------------------------------ config
def load_conf():
    c = {"token": "", "target": "", "port": 8765}
    try:
        with open(CONF_PATH) as f:
            c.update(json.load(f))
    except Exception:
        pass
    if os.environ.get("PHONE_API_TOKEN"):
        c["token"] = os.environ["PHONE_API_TOKEN"]
    if os.environ.get("PHONE_API_PORT"):
        c["port"] = int(os.environ["PHONE_API_PORT"])
    if os.environ.get("ADB_SERIAL"):
        c["target"] = os.environ["ADB_SERIAL"]
    return c


def save_conf(c):
    with open(CONF_PATH, "w") as f:
        json.dump(c, f, indent=2)
    try:
        os.chmod(CONF_PATH, 0o600)
    except Exception:
        pass


# ------------------------------------------------------------------ adb layer
class ADBError(Exception):
    pass


state = {"x": 540, "y": 960, "w": 1080, "h": 1920, "serial": ""}
lock = threading.RLock()
TOKEN = ""


def _adb_raw(args, use_serial=True, binary=False, timeout=20):
    cmd = ["adb"]
    if use_serial and state["serial"]:
        cmd += ["-s", state["serial"]]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    except FileNotFoundError:
        raise ADBError("adb nahi mila. Termux me chalao: pkg install android-tools")
    except subprocess.TimeoutExpired:
        raise ADBError("adb timeout: " + " ".join(map(str, args[:3])))
    if r.returncode != 0:
        msg = (r.stderr or r.stdout).decode(errors="ignore").strip()
        raise ADBError(msg or "adb error")
    return r.stdout if binary else r.stdout.decode(errors="ignore").strip()


def list_devices():
    out = _adb_raw(["devices"], use_serial=False)
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) == 2 and parts[1].strip() == "device":
            devs.append(parts[0].strip())
    return devs


def connect():
    tgt = load_conf().get("target", "")
    devs = list_devices()
    if tgt and tgt not in devs:
        try:
            _adb_raw(["connect", tgt], use_serial=False, timeout=10)
        except ADBError:
            pass
        devs = list_devices()
    if tgt and tgt in devs:
        state["serial"] = tgt
    elif devs:
        state["serial"] = devs[0]
    else:
        state["serial"] = ""
        raise ADBError("Phone adb se connected nahi hai. 'python phone.py setup' chalao "
                       "aur Wireless debugging ON rakho.")
    return state["serial"]


RETRY_WORDS = ("offline", "not found", "no devices", "closed", "unauthorized",
               "cannot connect", "device still authorizing")


def adb(args, **kw):
    if not state["serial"]:
        connect()
    try:
        return _adb_raw(args, **kw)
    except ADBError as e:
        if any(k in str(e).lower() for k in RETRY_WORDS):
            connect()
            return _adb_raw(args, **kw)
        raise


def sh(cmd, **kw):
    return adb(["shell", cmd], **kw)


def set_dims_from_png(png):
    if png[:8] == b"\x89PNG\r\n\x1a\n":
        w, h = struct.unpack(">II", png[16:24])
        if w and h:
            state["w"], state["h"] = w, h


def refresh_size():
    try:
        png = adb(["exec-out", "screencap", "-p"], binary=True, timeout=30)
        set_dims_from_png(png)
    except ADBError:
        out = sh("wm size")
        m = re.findall(r"(\d+)x(\d+)", out)
        if m:
            state["w"], state["h"] = int(m[-1][0]), int(m[-1][1])
    state["x"] = min(state["x"], state["w"] - 1)
    state["y"] = min(state["y"], state["h"] - 1)


# ------------------------------------------------------------------ helpers
def clamp(v, lo, hi):
    return max(lo, min(hi, int(float(v))))


def pos(d):
    x = d.get("x")
    y = d.get("y")
    x = state["x"] if x is None else x
    y = state["y"] if y is None else y
    return clamp(x, 0, state["w"] - 1), clamp(y, 0, state["h"] - 1)


def press(key):
    k = str(key).strip()
    code = KEYS.get(k.upper())
    if code is None:
        code = k if k.isdigit() else (k.upper() if k.upper().startswith("KEYCODE_") else "KEYCODE_" + k.upper())
    sh(f"input keyevent {shlex.quote(str(code))}")


def type_text(text, clear=False):
    if clear:
        clear_field()
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line:
            if line.isascii():
                for j in range(0, len(line), 40):
                    chunk = line[j:j + 40].replace(" ", "%s")
                    sh("input text " + shlex.quote(chunk))
            else:
                b64 = base64.b64encode(line.encode()).decode()
                out = sh(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}")
                if "Broadcast completed" not in out:
                    raise ADBError("Unicode typing ke liye ADBKeyBoard app install karke active karo")
        if i < len(lines) - 1:
            press("ENTER")


def clear_field():
    try:
        sh("input keycombination 113 29")  # Ctrl+A (Android 12+)
        press("DEL")
    except ADBError:
        sh("input keyevent 123 " + " ".join(["67"] * 60))  # END + 60 x DEL


def current_app():
    out = sh("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'")
    m = re.search(r"([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", out)
    return {"package": m.group(1), "activity": m.group(2)} if m else {"raw": out[:200]}


# ------------------------------------------------------------------ UI dump (screen padhna)
def ui_dump():
    last = None
    for _ in range(3):
        try:
            out = adb(["exec-out", "uiautomator", "dump", "/dev/tty"], timeout=25)
            i, j = out.find("<?xml"), out.rfind("</hierarchy>")
            if i >= 0 and j >= 0:
                return out[i:j + 12]
        except ADBError as e:
            last = e
        try:
            sh("uiautomator dump /sdcard/.ui.xml >/dev/null 2>&1")
            out = sh("cat /sdcard/.ui.xml")
            if "<hierarchy" in out:
                return out
        except ADBError as e:
            last = e
        time.sleep(0.6)
    raise ADBError(f"UI dump fail (secure/animated screen?): {last}")


def ui_nodes():
    root = ET.fromstring(ui_dump())
    res = []
    for n in root.iter("node"):
        a = n.attrib
        m = BOUNDS.match(a.get("bounds", ""))
        if not m:
            continue
        l, t, r, b = map(int, m.groups())
        if r <= l or b <= t:
            continue
        text, desc = a.get("text", ""), a.get("content-desc", "")
        cls = a.get("class", "").split(".")[-1]
        click = a.get("clickable") == "true" or a.get("long-clickable") == "true"
        edit = cls in ("EditText", "AutoCompleteTextView")
        scroll = a.get("scrollable") == "true"
        if not (text or desc or click or edit or scroll):
            continue
        res.append({"t": text, "d": desc, "id": a.get("resource-id", "").split("/")[-1],
                    "c": cls, "k": click, "e": edit, "s": scroll,
                    "x": (l + r) // 2, "y": (t + b) // 2, "b": [l, t, r, b]})
    return res


def find_nodes(q, exact=False):
    q = q.lower()
    out = []
    for n in ui_nodes():
        fields = [n["t"].lower(), n["d"].lower(), n["id"].lower()]
        if (exact and q in fields) or (not exact and any(q in f for f in fields if f)):
            out.append(n)
    return out


# ------------------------------------------------------------------ actions
def handle(path, d):
    if path == "/status":
        try:
            connect()
        except ADBError as e:
            return {"ok": False, "error": str(e)}
        info = {"ok": True, "serial": state["serial"], "screen": [state["w"], state["h"]],
                "cursor": [state["x"], state["y"]]}
        try:
            info["app"] = current_app()
        except Exception as e:
            info["app_error"] = str(e)
        return info
    if path == "/size":
        refresh_size()
        return {"w": state["w"], "h": state["h"]}
    if path == "/cursor":
        return {"x": state["x"], "y": state["y"]}
    if path == "/cursor/move":
        state["x"] = clamp(state["x"] + float(d.get("dx", 0)), 0, state["w"] - 1)
        state["y"] = clamp(state["y"] + float(d.get("dy", 0)), 0, state["h"] - 1)
    elif path == "/cursor/set":
        state["x"], state["y"] = pos(d)
    elif path == "/click":
        x, y = pos(d)
        state["x"], state["y"] = x, y
        sh(f"input tap {x} {y}")
    elif path == "/double_click":
        x, y = pos(d)
        state["x"], state["y"] = x, y
        sh(f"input tap {x} {y}; sleep 0.08; input tap {x} {y}")
    elif path == "/long_press":
        x, y = pos(d)
        state["x"], state["y"] = x, y
        sh(f"input swipe {x} {y} {x} {y} {int(d.get('ms', 800))}")
    elif path == "/drag":
        x1, y1 = clamp(d["x1"], 0, state["w"] - 1), clamp(d["y1"], 0, state["h"] - 1)
        x2, y2 = clamp(d["x2"], 0, state["w"] - 1), clamp(d["y2"], 0, state["h"] - 1)
        sh(f"input swipe {x1} {y1} {x2} {y2} {int(d.get('ms', 300))}")
        state["x"], state["y"] = x2, y2
    elif path == "/scroll":
        w, h = state["w"], state["h"]
        amt = min(max(float(d.get("amount", 0.5)), 0.1), 0.9)
        cx, cy = w // 2, h // 2
        direction = d.get("dir", "down")
        if direction in ("down", "up"):
            span = int(h * amt)
            a, b = cy + span // 2, cy - span // 2
            if direction == "up":
                a, b = b, a
            sh(f"input swipe {cx} {a} {cx} {b} 300")
        elif direction in ("left", "right"):
            span = int(w * amt)
            a, b = cx + span // 2, cx - span // 2
            if direction == "left":
                a, b = b, a
            sh(f"input swipe {a} {cy} {b} {cy} 300")
        else:
            raise ValueError("dir = up|down|left|right")
    elif path == "/type":
        type_text(str(d.get("text", "")), bool(d.get("clear", False)))
    elif path == "/clear":
        clear_field()
    elif path == "/key":
        press(d["key"])
    elif path == "/open_app":
        sh(f"monkey -p {shlex.quote(d['package'])} -c android.intent.category.LAUNCHER 1 >/dev/null 2>&1")
    elif path == "/current_app":
        return current_app()
    elif path == "/wait":
        time.sleep(min(float(d.get("sec", 1)), 30))
    elif path == "/ui":
        nodes = ui_nodes()
        q = str(d.get("query", "")).lower()
        if q:
            nodes = [n for n in nodes if q in (n["t"] + " " + n["d"] + " " + n["id"]).lower()]
        if d.get("clickable"):
            nodes = [n for n in nodes if n["k"] or n["e"]]
        limit = int(d.get("limit", 120))
        return {"count": len(nodes), "screen": [state["w"], state["h"]], "nodes": nodes[:limit]}
    elif path == "/click_text":
        found = find_nodes(str(d["text"]), bool(d.get("exact", False)))
        idx = int(d.get("index", 0))
        if idx >= len(found):
            return {"ok": False, "error": f"'{d['text']}' nahi mila", "matches": len(found)}
        n = found[idx]
        state["x"], state["y"] = n["x"], n["y"]
        sh(f"input tap {n['x']} {n['y']}")
        return {"ok": True, "clicked": n["t"] or n["d"] or n["id"], "x": n["x"], "y": n["y"], "matches": len(found)}
    elif path == "/wait_text":
        end = time.time() + min(float(d.get("timeout", 10)), 60)
        while time.time() < end:
            try:
                found = find_nodes(str(d["text"]), bool(d.get("exact", False)))
            except ADBError:
                found = []
            if found:
                return {"ok": True, "found": True, "x": found[0]["x"], "y": found[0]["y"]}
            time.sleep(0.8)
        return {"ok": False, "found": False}
    elif path == "/batch":
        results = []
        for act in d.get("actions", []):
            act = dict(act)
            p = "/" + str(act.pop("do")).lstrip("/")
            r = handle(p, act)
            results.append(r)
            if isinstance(r, dict) and r.get("ok") is False:
                break
        return {"ok": True, "results": results}
    elif path == "/shell":
        if not ALLOW_SHELL:
            raise PermissionError("shell band hai (PHONE_API_ALLOW_SHELL=1 set karke server chalao)")
        return {"output": sh(d["cmd"])}
    else:
        raise KeyError(path)
    return {"ok": True, "cursor": [state["x"], state["y"]]}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def send(self, code, obj=None, raw=None, ctype="application/json"):
        body = raw if raw is not None else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        if not hmac.compare_digest(self.headers.get("X-Token", ""), TOKEN):
            self.send(401, {"error": "bad token"})
            return False
        return True

    def route(self, data):
        path = self.path.split("?")[0]
        try:
            with lock:
                if path == "/screenshot":
                    png = adb(["exec-out", "screencap", "-p"], binary=True, timeout=30)
                    set_dims_from_png(png)
                    return self.send(200, raw=png, ctype="image/png")
                self.send(200, handle(path, data))
        except KeyError as e:
            self.send(404, {"error": f"unknown/missing: {e}"})
        except PermissionError as e:
            self.send(403, {"error": str(e)})
        except Exception as e:
            self.send(500, {"error": str(e)})

    def do_GET(self):
        if self.authed():
            self.route({})

    def do_POST(self):
        if not self.authed():
            return
        try:
            n = int(self.headers.get("Content-Length") or 0)
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self.send(400, {"error": "bad json"})
        self.route(data)


def serve():
    global TOKEN
    conf = load_conf()
    if not conf["token"]:
        conf["token"] = secrets.token_urlsafe(16)
        save_conf(conf)
    TOKEN = conf["token"]
    try:
        connect()
        refresh_size()
        state["x"], state["y"] = state["w"] // 2, state["h"] // 2
    except ADBError as e:
        print("WARNING:", e)
    try:
        srv = ThreadingHTTPServer((HOST, int(conf["port"])), Handler)
    except OSError as e:
        sys.exit(f"Port {conf['port']} busy ({e}). Server shayad pehle se chal raha hai.")
    with open(PID_PATH, "w") as f:
        f.write(str(os.getpid()))
    print(f"Phone API http://{HOST}:{conf['port']}  screen={state['w']}x{state['h']}  device={state['serial']}")
    srv.serve_forever()


def start_bg():
    log = open(LOG_PATH, "a")
    subprocess.Popen([sys.executable, os.path.abspath(__file__), "serve"], stdout=log, stderr=log,
                     stdin=subprocess.DEVNULL, start_new_session=True)
    time.sleep(2)


def stop():
    try:
        pid = int(open(PID_PATH).read())
        os.kill(pid, signal.SIGTERM)
        os.remove(PID_PATH)
        print("stopped")
    except Exception:
        print("server chal nahi raha")


# ------------------------------------------------------------------ setup wizard
def setup():
    print("== Phone control setup ==")
    try:
        _adb_raw(["version"], use_serial=False)
    except ADBError as e:
        sys.exit(str(e))
    conf = load_conf()
    print("\n1) Settings > Developer options > Wireless debugging ON")
    print("2) 'Pair device with pairing code' dabao. Wahan IP:port aur 6-digit code dikhega.")
    pair = input("\nPairing port (sirf number, skip karne ke liye Enter): ").strip()
    if pair:
        code = input("Pairing code: ").strip()
        try:
            print(_adb_raw(["pair", f"localhost:{pair}", code], use_serial=False, timeout=30))
        except ADBError as e:
            sys.exit(f"Pair fail: {e}")
    port = input("\nWireless debugging main screen ka 'IP address & Port' wala PORT (pairing wala nahi): ").strip()
    target = f"localhost:{port}"
    try:
        print(_adb_raw(["connect", target], use_serial=False, timeout=15))
    except ADBError as e:
        sys.exit(f"Connect fail: {e}")
    if target not in list_devices():
        sys.exit("Device 'device' state me nahi aaya. Pairing dobara try karo.")
    print("\nStable port 5555 try kar raha hu (reboot tak same rahega)...")
    try:
        _adb_raw(["-s", target, "tcpip", "5555"], use_serial=False, timeout=15)
        time.sleep(3)
        _adb_raw(["connect", "localhost:5555"], use_serial=False, timeout=15)
        if "localhost:5555" in list_devices():
            target = "localhost:5555"
    except ADBError:
        pass
    if target not in list_devices():  # tcpip ke baad purana wapas connect
        try:
            _adb_raw(["connect", f"localhost:{port}"], use_serial=False, timeout=15)
            target = f"localhost:{port}"
        except ADBError:
            pass
    conf["target"] = target
    if not conf["token"]:
        conf["token"] = secrets.token_urlsafe(16)
    save_conf(conf)
    state["serial"] = target
    try:
        print("Test:", sh("echo adb-ok"))
    except ADBError as e:
        sys.exit(f"Test fail: {e}")
    print(f"\nDONE. target={target}\nConfig: {CONF_PATH}\nAb chalao: python phone.py serve --bg ; python phone.py status")


# ------------------------------------------------------------------ client
def api(path, data=None, raw=False, _retry=True):
    conf = load_conf()
    url = f"http://127.0.0.1:{conf['port']}{path}"
    req = urllib.request.Request(
        url, data=None if data is None else json.dumps(data).encode(),
        headers={"X-Token": conf["token"], "Content-Type": "application/json"},
        method="GET" if data is None else "POST")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=60) as r:
            body = r.read()
            return body if raw else json.loads(body)
    except urllib.error.HTTPError as e:
        try:
            return json.loads(e.read())
        except Exception:
            return {"error": f"HTTP {e.code}"}
    except urllib.error.URLError:
        if _retry and conf["token"]:
            start_bg()
            return api(path, data, raw, False)
        return {"error": "server nahi chal raha. Pehle: python phone.py setup"}


def xy(a):
    return {"x": int(a[0]), "y": int(a[1])} if len(a) >= 2 else {}


def show_ui(res):
    if "nodes" not in res:
        return json.dumps(res, ensure_ascii=False)
    lines = [f"screen {res['screen'][0]}x{res['screen'][1]}  elements={res['count']}"]
    for n in res["nodes"]:
        label = n["t"] or n["d"] or n["id"] or n["c"]
        flags = "".join(f for f, on in (("C", n["k"]), ("E", n["e"]), ("S", n["s"])) if on)
        lines.append(f"{label[:60]!r} [{flags}] id={n['id']} @{n['x']},{n['y']}")
    lines.append("flags: C=clickable E=editable S=scrollable")
    return "\n".join(lines)


def cli(c, a):
    if c == "shot":
        png = api("/screenshot", raw=True)
        if isinstance(png, dict):
            return json.dumps(png)
        f = os.path.abspath(a[0] if a else os.path.join(HOME, "screen.png"))
        with open(f, "wb") as fh:
            fh.write(png)
        return json.dumps({"saved": f, "bytes": len(png)})
    if c == "ui":
        return show_ui(api("/ui", {"query": a[0]} if a else {}))
    simple = {
        "status": lambda: api("/status"),
        "current": lambda: api("/current_app"),
        "move": lambda: api("/cursor/move", {"dx": float(a[0]), "dy": float(a[1])}),
        "set": lambda: api("/cursor/set", xy(a)),
        "click": lambda: api("/click", xy(a)),
        "dclick": lambda: api("/double_click", xy(a)),
        "long": lambda: api("/long_press", {**xy(a), **({"ms": int(a[-1])} if len(a) in (1, 3) else {})}),
        "drag": lambda: api("/drag", {**dict(zip(["x1", "y1", "x2", "y2"], map(int, a[:4]))),
                                      **({"ms": int(a[4])} if len(a) > 4 else {})}),
        "scroll": lambda: api("/scroll", {"dir": a[0], **({"amount": float(a[1])} if len(a) > 1 else {})}),
        "type": lambda: api("/type", {"text": " ".join(x for x in a if x != "--clear"), "clear": "--clear" in a}),
        "clear": lambda: api("/clear", {}),
        "key": lambda: api("/key", {"key": a[0]}),
        "open": lambda: api("/open_app", {"package": a[0]}),
        "clicktext": lambda: api("/click_text", {"text": a[0], **({"index": int(a[1])} if len(a) > 1 else {})}),
        "waittext": lambda: api("/wait_text", {"text": a[0], **({"timeout": float(a[1])} if len(a) > 1 else {})}),
        "wait": lambda: api("/wait", {"sec": float(a[0]) if a else 1}),
    }
    if c not in simple:
        return __doc__
    return json.dumps(simple[c](), ensure_ascii=False)


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        return print(__doc__)
    c, a = sys.argv[1], sys.argv[2:]
    if c == "setup":
        return setup()
    if c == "serve":
        return start_bg() if "--bg" in a else serve()
    if c == "stop":
        return stop()
    try:
        print(cli(c, a))
    except (IndexError, ValueError):
        print("Arguments galat. 'python phone.py help' dekho.")


if __name__ == "__main__":
    main()
