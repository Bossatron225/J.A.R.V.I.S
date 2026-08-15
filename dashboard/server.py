"""
dashboard/server.py — JARVIS Local HTTP Dashboard

Plain HTTP on port 8000 (no SSL warnings, no firewall issues).
Security at the application layer: AES-256-CBC with session-key-derived key.
CryptoJS is auto-downloaded once and served locally — no CDN needed after that.

Install deps:  pip install fastapi "uvicorn[standard]" cryptography
"""

import asyncio
import base64
import hashlib
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

TOKEN_TTL_SECS = 60 * 60 * 12
LOGIN_WINDOW_SECS = 60 * 5
LOGIN_MAX_ATTEMPTS = 8
LOGIN_LOCKOUT_SECS = 60 * 10

_DEPS_OK = False
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
    from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
    import uvicorn
    _DEPS_OK = True
except ImportError:
    pass

# python-multipart is required for file uploads — optional dependency
_UPLOAD_OK = False
try:
    from fastapi import UploadFile, File as FastAPIFile
    _UPLOAD_OK = True
except Exception:
    pass

BASE_DIR    = Path(__file__).resolve().parent.parent
STATIC_DIR  = Path(__file__).parent / "static"
PORT        = 8000
MAX_UPLOAD_MB = 500
PHONE_AUDIO_QUEUE_MAX = 24


def _extract_public_url(line: str) -> str | None:
    """Extract a public tunnel URL from Cloudflare/ngrok-style process output."""
    if not line:
        return None
    for candidate in re.findall(r"https://[^\s|]+", line):
        cleaned = candidate.rstrip(").,;:!'\"")
        if cleaned.endswith((".trycloudflare.com", ".cfargotunnel.com", ".ngrok-free.app")):
            return cleaned
        if any(token in cleaned for token in ("trycloudflare", "cfargotunnel", "ngrok-free")):
            return cleaned
    return None


def _make_uploads_dir() -> Path:
    """Return (and create) the cross-platform uploads folder."""
    for candidate in [
        Path.home() / "Downloads" / "JARVIS Uploads",
        Path.home() / "Documents" / "JARVIS Uploads",
        BASE_DIR / "uploads",
    ]:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except Exception:
            pass
    return BASE_DIR / "uploads"


UPLOADS_DIR = _make_uploads_dir()

def _get_gemini_key() -> str | None:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            return _json.load(f).get("gemini_api_key")
    except Exception:
        return None


def _read_api_config() -> dict:
    try:
        import json as _json
        with open(BASE_DIR / "config" / "api_keys.json", "r", encoding="utf-8") as f:
            data = _json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}

_KEY_CHARS = [c for c in (string.ascii_uppercase + string.digits)
              if c not in ('O', 'I', 'L', '0', '1')]

# ── AES-256-CBC ───────────────────────────────────────────────────────────────
_AES_SALT = b'JARVIS-DASHBOARD-v1'


def _derive_key(session_key: str) -> bytes:
    """SHA-256(sessionKey‖salt) → 32-byte AES-256 key (microseconds, no PBKDF2 needed)."""
    return hashlib.sha256(session_key.encode('utf-8') + _AES_SALT).digest()


def _decrypt_cbc(aes_key: bytes, enc_b64: str) -> str:
    """Decrypt base64(IV[16] ‖ ciphertext) with AES-256-CBC + PKCS7."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.primitives import padding as sym_pad
    raw      = base64.b64decode(enc_b64)
    iv, ct   = raw[:16], raw[16:]
    dec      = Cipher(algorithms.AES(aes_key), modes.CBC(iv)).decryptor()
    padded   = dec.update(ct) + dec.finalize()
    unpadder = sym_pad.PKCS7(128).unpadder()
    return (unpadder.update(padded) + unpadder.finalize()).decode('utf-8')


# ── CryptoJS (auto-download once, served locally) ─────────────────────────────
_CRYPTOJS_CDN  = ("https://cdnjs.cloudflare.com/ajax/libs/"
                  "crypto-js/4.2.0/crypto-js.min.js")
_CRYPTOJS_FILE = STATIC_DIR / "crypto-js.min.js"


def _ensure_network_access(port: int) -> None:
    """Cross-platform, best-effort: open port in the OS firewall for LAN access.

    Runs in a background thread — never blocks uvicorn startup.

    Windows : writes a .bat file, runs it elevated via Windows ShellExecuteW
              (native UAC dialog, guaranteed to appear). One-time setup.
    macOS   : osascript admin dialog if the Application Firewall is on.
    Linux   : pkexec GUI → sudo -n → prints manual command as fallback.
    """
    import sys, subprocess, os, tempfile, threading

    # ── Windows ──────────────────────────────────────────────────────────────
    if sys.platform == "win32":
        import ctypes, time

        port_rule = f"JARVIS Dashboard Port {port}"
        prog_rule  = "JARVIS Dashboard Python"
        py_exe     = sys.executable

        def _netsh_rule_exists(name: str) -> bool:
            try:
                r = subprocess.run(
                    ["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"],
                    capture_output=True, text=True, timeout=5,
                )
                return r.returncode == 0 and "No rules match" not in r.stdout
            except Exception:
                return False

        def _network_is_public() -> bool:
            try:
                r = subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "(Get-NetConnectionProfile | "
                     "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                     "Measure-Object).Count"],
                    capture_output=True, text=True, timeout=6,
                )
                return r.stdout.strip() not in ("", "0")
            except Exception:
                return False

        need_port    = not _netsh_rule_exists(port_rule)
        need_prog    = not _netsh_rule_exists(prog_rule)
        need_private = _network_is_public()

        if not need_port and not need_prog and not need_private:
            return  # already fully configured

        # Build a .bat file — netsh + powershell, runs fast when elevated
        bat_lines = ["@echo off"]
        if need_private:
            bat_lines.append(
                'powershell -NoProfile -NonInteractive -Command "'
                'Get-NetConnectionProfile | '
                "Where-Object {$_.NetworkCategory -eq 'Public'} | "
                'Set-NetConnectionProfile -NetworkCategory Private"'
            )
        if need_port:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{port_rule}" protocol=TCP dir=in '
                f'localport={port} action=allow'
            )
        if need_prog:
            bat_lines.append(
                f'netsh advfirewall firewall add rule '
                f'name="{prog_rule}" dir=in action=allow '
                f'program="{py_exe}" enable=yes'
            )

        bat_body = "\r\n".join(bat_lines) + "\r\n"
        fd, bat_path = tempfile.mkstemp(suffix=".bat", prefix="jarvis_fw_")
        try:
            os.write(fd, bat_body.encode("mbcs"))   # Windows cmd.exe expects ANSI
            os.close(fd)
        except Exception:
            try:
                os.close(fd)
            except Exception:
                pass
            return

        # ── Try running directly (succeeds when already admin) ────────────────
        try:
            r = subprocess.run(
                [bat_path], capture_output=True, timeout=8, shell=True
            )
            if r.returncode == 0:
                print(f"[Dashboard] Firewall configured for port {port}.")
                try:
                    os.unlink(bat_path)
                except Exception:
                    pass
                return
        except Exception:
            pass

        # ── ShellExecuteW: native UAC elevation (most reliable on Windows) ────
        # ShellExecuteW with verb "runas" always shows the UAC dialog regardless
        # of UAC level settings. Non-blocking — uvicorn is already running.
        print("[Dashboard] One-time network setup required.")
        print("[Dashboard] >>> A Windows security dialog will appear — click 'Yes' <<<")
        try:
            ret = ctypes.windll.shell32.ShellExecuteW(
                None,       # hwnd  (no parent window)
                "runas",    # verb  (request elevation)
                bat_path,   # file  (our .bat)
                None,       # params
                None,       # working dir
                0,          # SW_HIDE (run without a visible cmd window)
            )
            if int(ret) > 32:
                # ShellExecuteW returns immediately; bat finishes in ~1 second.
                # Sleep briefly so the rules are in place before the first retry.
                time.sleep(2)
                print(f"[Dashboard] Network setup complete — port {port} is open.")
                print("[Dashboard] Refresh your phone browser to connect.")
            else:
                print("[Dashboard] Setup was not allowed.")
                print("[Dashboard] Phone connections may fail until JARVIS is run as Administrator.")
        except Exception as e:
            print(f"[Dashboard] Firewall setup error: {e}")
        finally:
            # Cleanup after the bat has had time to run
            def _cleanup(path: str) -> None:
                time.sleep(5)
                try:
                    os.unlink(path)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, args=(bat_path,), daemon=True).start()
        return

    # ── macOS ─────────────────────────────────────────────────────────────────
    if sys.platform == "darwin":
        fw_ctl = "/usr/libexec/ApplicationFirewall/socketfilterfw"
        try:
            r = subprocess.run(
                [fw_ctl, "--getglobalstate"], capture_output=True, text=True, timeout=5,
            )
            if "disabled" in r.stdout.lower():
                return  # firewall off — nothing to do

            py = sys.executable
            listed = subprocess.run(
                [fw_ctl, "--listapps"], capture_output=True, text=True, timeout=5,
            )
            if py in listed.stdout:
                return  # already allowed

            print("[Dashboard] One-time network setup — enter your password in the macOS dialog.")
            subprocess.run(
                ["osascript", "-e",
                 f'do shell script "{fw_ctl} --add {py} && {fw_ctl} --unblockapp {py}"'
                 f' with administrator privileges'],
                timeout=60,
            )
        except Exception:
            pass  # macOS firewall is off by default — silent failure is fine
        return

    # ── Linux ─────────────────────────────────────────────────────────────────
    def _privileged(cmd: list[str]) -> bool:
        for prefix in (["pkexec"], ["sudo", "-n"]):
            try:
                r = subprocess.run(prefix + cmd, capture_output=True, timeout=30)
                if r.returncode == 0:
                    return True
            except Exception:
                pass
        return False

    try:  # ufw
        r = subprocess.run(["ufw", "status"], capture_output=True, text=True, timeout=5)
        if "active" in r.stdout.lower():
            if _privileged(["ufw", "allow", f"{port}/tcp"]):
                print(f"[Dashboard] ufw: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo ufw allow {port}/tcp")
            return
    except FileNotFoundError:
        pass

    try:  # firewalld
        r = subprocess.run(
            ["firewall-cmd", "--state"], capture_output=True, text=True, timeout=5,
        )
        if "running" in r.stdout.lower():
            ok = (_privileged(["firewall-cmd", "--add-port", f"{port}/tcp", "--permanent"])
                  and _privileged(["firewall-cmd", "--reload"]))
            if ok:
                print(f"[Dashboard] firewalld: port {port} allowed.")
            else:
                print(f"[Dashboard] Run manually:  sudo firewall-cmd --add-port={port}/tcp --permanent && sudo firewall-cmd --reload")
            return
    except FileNotFoundError:
        pass

    try:  # iptables (not persistent but works until reboot)
        r = subprocess.run(["iptables", "-L", "INPUT", "-n"], capture_output=True, timeout=5)
        if r.returncode == 0:
            if _privileged(["iptables", "-A", "INPUT", "-p", "tcp", "--dport", str(port), "-j", "ACCEPT"]):
                print(f"[Dashboard] iptables: port {port} opened.")
            else:
                print(f"[Dashboard] Run manually:  sudo iptables -A INPUT -p tcp --dport {port} -j ACCEPT")
    except FileNotFoundError:
        pass  # no iptables means firewall is probably off — nothing to do


def _ensure_crypto_js() -> None:
    if _CRYPTOJS_FILE.exists():
        return
    try:
        import urllib.request
        print("[Dashboard] Downloading CryptoJS (one-time setup)…")
        urllib.request.urlretrieve(_CRYPTOJS_CDN, str(_CRYPTOJS_FILE))
        print("[Dashboard] CryptoJS cached — will serve locally from now on.")
    except Exception as e:
        print(f"[Dashboard] CryptoJS download failed: {e}")
        print(f"[Dashboard] Encryption will fall back to CDN load on client.")


_ensure_crypto_js()


# ── helpers ───────────────────────────────────────────────────────────────────

def _local_ip() -> str:
    """Return the best LAN-facing IPv4 address, no internet required."""
    # Method 1: route trick (fast, works when internet is available)
    for probe in ("8.8.8.8", "1.1.1.1", "192.168.1.1"):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.settimeout(0.5)
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            s.close()
            if not ip.startswith("127."):
                return ip
        except Exception:
            pass

    # Method 2: hostname resolution (works offline on most systems)
    try:
        ip = socket.gethostbyname(socket.gethostname())
        if not ip.startswith("127."):
            return ip
    except Exception:
        pass

    # Method 3: enumerate all interfaces (fully offline, no external deps)
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and not ip.startswith("169.254."):
                return ip
    except Exception:
        pass

    return "127.0.0.1"


def _read(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


# ── DashboardServer ───────────────────────────────────────────────────────────

class DashboardServer:

    def __init__(self):
        self._ip                          = _local_ip()
        self._public_url: str | None      = None
        self._tunnel_proc: subprocess.Popen | None = None
        self._tokens: set[str]            = set()
        self._token_keys: dict[str, str]  = {}   # auth_token → session_key
        self._token_expiry: dict[str, float] = {}  # auth_token → unix expiry
        self._aes_cache:  dict[str, bytes]= {}   # session_key → AES bytes
        self._public_url_callback = None
        self._clients: set[WebSocket]     = set()
        self._audio_clients: set[WebSocket] = set()
        self._history: list[dict]         = []
        self._command_queue               = asyncio.Queue()
        self._wake_callback               = None
        self._connect_callback            = None
        self._event_sink                  = None
        self._audio_sink                  = None
        self._audio_available              = None
        self._pending_keys: dict[str, float] = {}
        self._device_sessions: dict[str, dict] = {}  # device_token → {session_key}
        self._phone_audio_queue: asyncio.Queue    = asyncio.Queue(maxsize=PHONE_AUDIO_QUEUE_MAX)
        self._login_attempts: dict[str, list[float]] = {}
        self._login_blocked_until: dict[str, float] = {}
        self._uploads_dir                 = UPLOADS_DIR
        self._login_html                  = _read("login.html")
        self._app_html                    = _read("app.html")
        self.app                          = self._build_app()

    # ── one-time key management ───────────────────────────────────────────

    def new_key(self, expiry_secs: int = 600) -> str:
        now = time.time()
        self._pending_keys = {k: v for k, v in self._pending_keys.items() if v > now}
        key = ''.join(secrets.choice(_KEY_CHARS) for _ in range(6))
        self._pending_keys[key] = now + expiry_secs
        return key

    @staticmethod
    def _ssl_enabled() -> bool:
        certs = BASE_DIR / "config" / "certs"
        return (certs / "jarvis.key").exists() and (certs / "jarvis.crt").exists()

    @staticmethod
    def _normalize_public_url(url: str) -> str:
        u = (url or "").strip()
        if not u:
            return ""
        if not u.startswith(("http://", "https://")):
            u = f"https://{u}"
        u = u.rstrip("/")

        try:
            p = urlparse(u)
            host = p.hostname or ""
            if p.scheme not in ("http", "https"):
                return ""
            if not host:
                return ""
            if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
                return ""
        except Exception:
            return ""

        return u

    def set_public_url_callback(self, cb) -> None:
        self._public_url_callback = cb

    def _notify_public_url(self, url: str | None) -> None:
        if not self._public_url_callback:
            return
        try:
            self._public_url_callback(url)
        except Exception as e:
            print(f"[Dashboard] Public URL callback error: {e}")

    def _set_public_url(self, url: str) -> None:
        clean = self._normalize_public_url(url)
        if clean:
            self._public_url = clean
            self._notify_public_url(clean)

    def _start_public_tunnel(self) -> None:
        """Optional public access mode for remote dashboard.

        Enable with one of:
        - JARVIS_PUBLIC_URL=https://your-public-url
        - JARVIS_ENABLE_TUNNEL=1 (uses cloudflared or ngrok when available)
        """
        cfg = _read_api_config()

        env_public = self._normalize_public_url(os.getenv("JARVIS_PUBLIC_URL", ""))
        raw_cfg_public = str(cfg.get("public_remote_url", "") or "").strip()
        cfg_public = self._normalize_public_url(raw_cfg_public)

        if env_public:
            self._public_url = env_public
            self._notify_public_url(self._public_url)
            print(f"[Dashboard] Public URL: {self._public_url}")
            return

        if raw_cfg_public and not cfg_public:
            print("[Dashboard] Ignoring invalid public_remote_url in config; falling back to tunnel/local URL.")

        if cfg_public:
            self._public_url = cfg_public
            self._notify_public_url(self._public_url)
            print(f"[Dashboard] Public URL (config): {self._public_url}")
            return

        env_enable = os.getenv("JARVIS_ENABLE_TUNNEL", "").strip().lower() in ("1", "true", "yes", "on")
        cfg_enable = bool(cfg.get("public_remote_enabled", False))
        os.environ.setdefault("JARVIS_ENABLE_TUNNEL", "1")
        os.environ.setdefault("JARVIS_PUBLIC_URL", "")
        if not (env_enable or cfg_enable):
            return

        if self._tunnel_proc and self._tunnel_proc.poll() is None:
            return

        target_proto = "https" if self._ssl_enabled() else "http"
        target = f"{target_proto}://127.0.0.1:{PORT}"

        cloudflared_bin = (
            os.getenv("JARVIS_CLOUDFLARED_BIN", "").strip()
            or shutil.which("cloudflared")
            or str(Path.home() / ".local" / "bin" / "cloudflared")
        )
        ngrok_bin = (
            os.getenv("JARVIS_NGROK_BIN", "").strip()
            or shutil.which("ngrok")
            or str(Path(__file__).resolve().parent.parent / ".venv-1" / "bin" / "ngrok")
        )

        tunnel_cmd = None
        tunnel_label = None
        if cloudflared_bin and os.path.exists(cloudflared_bin):
            tunnel_cmd = [cloudflared_bin, "tunnel", "--url", target, "--no-autoupdate", "--metrics", "localhost:0"]
            if target_proto == "https":
                tunnel_cmd.append("--no-tls-verify")
            tunnel_label = "cloudflared"
        elif ngrok_bin and os.path.exists(ngrok_bin):
            tunnel_cmd = [ngrok_bin, "http", str(PORT)]
            tunnel_label = "ngrok"

        if not tunnel_cmd:
            print("[Dashboard] No public tunnel binary found. Install cloudflared or ngrok or set JARVIS_PUBLIC_URL.")
            return

        try:
            self._tunnel_proc = subprocess.Popen(
                tunnel_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
        except FileNotFoundError:
            print(f"[Dashboard] {tunnel_label} not found. Install it or set JARVIS_PUBLIC_URL.")
            return
        except Exception as e:
            print(f"[Dashboard] Tunnel start failed: {e}")
            return

        print(f"[Dashboard] Starting public tunnel ({tunnel_label})...")

        def _read_tunnel_output() -> None:
            if not self._tunnel_proc or not self._tunnel_proc.stdout:
                return
            for line in self._tunnel_proc.stdout:
                line = (line or "").strip()
                if not line:
                    continue
                found = _extract_public_url(line)
                if found:
                    if self._public_url != found:
                        self._public_url = found
                        os.environ["JARVIS_PUBLIC_URL"] = found
                        os.environ["PUBLIC_ENTRY_URL"] = found
                        self._notify_public_url(found)
                        print(f"[Dashboard] Public URL: {found}")
                        continue
                if "quick Tunnel" in line or "Requesting new quick Tunnel" in line or "forwarding" in line.lower():
                    print(f"[Dashboard] Tunnel status: {line}")

        threading.Thread(target=_read_tunnel_output, daemon=True, name="jarvis-tunnel-reader").start()

    def get_url(self) -> str:
        proto = "https" if self._ssl_enabled() else "http"
        return f"{proto}://{self._ip}:{PORT}"

    def get_remote_url(self) -> str:
        """Preferred remote URL (public tunnel/custom URL when available)."""
        return self._public_url or self.get_url()

    def get_manual_url(self) -> str:
        """URL for manual browser entry. When HTTPS active, points to alias port (also HTTPS)."""
        if self._public_url:
            return self._public_url
        if self._ssl_enabled():
            return f"{self._ip}:{PORT + 1}"
        return f"{self._ip}:{PORT}"

    def _aes_key(self, session_key: str) -> bytes:
        if session_key not in self._aes_cache:
            self._aes_cache[session_key] = _derive_key(session_key)
        return self._aes_cache[session_key]

    def _hash_remote_pin(self, pin: str) -> str:
        return hashlib.sha256(pin.encode("utf-8")).hexdigest()

    def _remote_pin_hash(self) -> str:
        cfg = _read_api_config()
        return str(cfg.get("remote_access_pin_hash", "") or "").strip().lower()

    def _remote_pin_required(self) -> bool:
        return bool(self._remote_pin_hash())

    def _verify_remote_pin(self, candidate: str) -> bool:
        stored = self._remote_pin_hash()
        if not stored:
            return True
        check = self._hash_remote_pin((candidate or "").strip())
        return secrets.compare_digest(stored, check)

    def _issue_token(self, session_key: str) -> str:
        tok = secrets.token_urlsafe(32)
        self._tokens.add(tok)
        self._token_keys[tok] = session_key
        self._token_expiry[tok] = time.time() + TOKEN_TTL_SECS
        self._aes_key(session_key)
        return tok

    def _prune_tokens(self) -> None:
        now = time.time()
        expired = [t for t, exp in self._token_expiry.items() if exp <= now]
        for tok in expired:
            self._tokens.discard(tok)
            self._token_expiry.pop(tok, None)
            self._token_keys.pop(tok, None)

    def _is_token_valid(self, tok: str) -> bool:
        self._prune_tokens()
        exp = self._token_expiry.get(tok, 0)
        if exp <= time.time():
            self._tokens.discard(tok)
            self._token_expiry.pop(tok, None)
            self._token_keys.pop(tok, None)
            return False
        return tok in self._tokens

    def _client_ip(self, req: Request) -> str:
        xff = (req.headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if xff:
            return xff
        if req.client and req.client.host:
            return req.client.host
        return "unknown"

    def _is_login_blocked(self, ip: str) -> tuple[bool, int]:
        now = time.time()
        until = self._login_blocked_until.get(ip, 0)
        if until > now:
            return True, int(until - now)
        if ip in self._login_blocked_until:
            self._login_blocked_until.pop(ip, None)
        return False, 0

    def _record_login_attempt(self, ip: str, success: bool) -> None:
        now = time.time()
        if success:
            self._login_attempts.pop(ip, None)
            self._login_blocked_until.pop(ip, None)
            return
        bucket = [t for t in self._login_attempts.get(ip, []) if now - t <= LOGIN_WINDOW_SECS]
        bucket.append(now)
        self._login_attempts[ip] = bucket
        if len(bucket) >= LOGIN_MAX_ATTEMPTS:
            self._login_blocked_until[ip] = now + LOGIN_LOCKOUT_SECS

    def get_auto_login_url(self, key: str) -> str:
        if self._remote_pin_required():
            return ""
        url = self.get_remote_url()
        return f"{url}/auto-login?key={key}"

    def get_remote_security_status(self) -> str:
        public_url = (self._public_url or "").strip()
        public_like_url = bool(public_url) and not public_url.startswith((
            "http://127.0.0.1",
            "http://localhost",
            "http://10.",
            "http://172.",
            "http://192.168.",
            "https://127.0.0.1",
            "https://localhost",
            "https://10.",
            "https://172.",
            "https://192.168.",
        ))
        public_state = "ON" if public_like_url else "OFF"
        pin_state = "REQUIRED" if self._remote_pin_required() else "OFF"
        ttl_hours = max(1, int(TOKEN_TTL_SECS // 3600))
        return f"SECURITY: PUBLIC={public_state}  |  PIN={pin_state}  |  TOKEN_TTL={ttl_hours}h"

    def _decrypt(self, token: str, enc_b64: str) -> str | None:
        sk = self._token_keys.get(token)
        if not sk:
            return None
        try:
            return _decrypt_cbc(self._aes_key(sk), enc_b64)
        except Exception:
            return None

    # ── callbacks ────────────────────────────────────────────────────────

    def set_wake_callback(self, fn) -> None:
        self._wake_callback = fn

    def set_connect_callback(self, fn) -> None:
        self._connect_callback = fn

    def set_remote_output_sinks(self, *, event_sink=None, audio_sink=None, audio_available=None) -> None:
        """Forward live output when this dashboard is embedded in the VPS worker."""
        self._event_sink = event_sink
        self._audio_sink = audio_sink
        self._audio_available = audio_available

    def has_remote_audio_sink(self) -> bool:
        if not callable(self._audio_sink):
            return False
        if not callable(self._audio_available):
            return True
        try:
            return bool(self._audio_available())
        except Exception:
            return False

    # ── broadcast ────────────────────────────────────────────────────────

    async def broadcast(self, msg: dict) -> None:
        self._history.append(msg)
        if len(self._history) > 300:
            self._history = self._history[-300:]
        if callable(self._event_sink):
            try:
                self._event_sink(msg)
            except Exception:
                pass
        dead: set[WebSocket] = set()
        for ws in list(self._clients):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.add(ws)
        self._clients -= dead

    async def send_audio_to_clients(self, payload: bytes) -> None:
        if not payload:
            return
        if callable(self._audio_sink):
            try:
                self._audio_sink(payload)
            except Exception:
                pass
        dead: set[WebSocket] = set()
        for ws in list(self._audio_clients):
            try:
                await ws.send_bytes(payload)
            except Exception:
                dead.add(ws)
        self._audio_clients -= dead

    # ── FastAPI app ───────────────────────────────────────────────────────

    def _build_app(self) -> "FastAPI":
        app = FastAPI(docs_url=None, redoc_url=None)

        def _auth(req: Request) -> bool:
            tok = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            return bool(tok) and self._is_token_valid(tok)

        # serve CryptoJS from local cache, fallback to CDN redirect
        @app.get("/static/crypto.js")
        async def serve_crypto():
            if _CRYPTOJS_FILE.exists():
                return FileResponse(str(_CRYPTOJS_FILE),
                                    media_type="application/javascript")
            from fastapi.responses import RedirectResponse
            return RedirectResponse(_CRYPTOJS_CDN)

        @app.get("/login", response_class=HTMLResponse)
        async def login_page():
            return HTMLResponse(self._login_html)

        @app.get("/", response_class=HTMLResponse)
        async def index():
            # Auth is handled client-side via sessionStorage bearer token.
            # Server-side header auth can't work here because browser navigations
            # don't send custom headers (location.href doesn't carry Authorization).
            html = (self._app_html
                    .replace("__IP__", self._ip)
                    .replace("__PORT__", str(PORT)))
            return HTMLResponse(html)

        @app.post("/login")
        async def login(req: Request):
            ip = self._client_ip(req)
            blocked, wait_s = self._is_login_blocked(ip)
            if blocked:
                return JSONResponse(
                    {"ok": False, "error": f"Too many attempts. Try again in {wait_s}s."},
                    status_code=429,
                )

            body    = await req.json()
            entered_key = str(body.get("key", body.get("pin", ""))).strip().upper()
            entered_pin = str(body.get("remote_pin", "")).strip()
            now     = time.time()
            key_ok = entered_key in self._pending_keys and self._pending_keys[entered_key] > now
            pin_ok = self._verify_remote_pin(entered_pin)

            if key_ok and pin_ok:
                del self._pending_keys[entered_key]      # one-time use
                tok = self._issue_token(entered_key)
                if self._connect_callback:
                    self._connect_callback()
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Remote connection established."}
                ))
                self._record_login_attempt(ip, success=True)
                # Bearer token in response body — no cookies needed (works on any browser/HTTP)
                return JSONResponse({"ok": True, "token": tok})

            self._record_login_attempt(ip, success=False)
            return JSONResponse({"ok": False, "error": "Invalid or expired key"},
                                status_code=401)

        @app.get("/auto-login")
        async def auto_login(key: str = ""):
            """QR code target — validates one-time key, creates session, redirects phone."""
            now = time.time()

            if self._remote_pin_required():
                from urllib.parse import quote as _q
                return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset=\"UTF-8\"><meta name=\"viewport\" content=\"width=device-width\">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center;padding:18px}}
  p{{color:#5e6a7e;font-size:14px;line-height:1.5}}a{{color:#8ab4ff;text-decoration:none;font-weight:600}}
</style></head>
<body><div>
  <h2 style=\"margin-bottom:10px\">PIN Required</h2>
  <p>Manual login is required for this remote session. Enter your one-time key and private PIN.</p>
  <p><a href=\"/login?key={_q(key)}\">Open secure login</a></p>
</div></body></html>""")

            if not key or key not in self._pending_keys or self._pending_keys[key] <= now:
                return HTMLResponse("""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}
  h2{color:#f87171;margin-bottom:12px}p{color:#5e6a7e;font-size:14px}
</style></head>
<body><div><h2>Link Expired</h2>
<p>Press <strong style="color:#dde3ed">Remote Control</strong> in JARVIS to get a new QR code.</p>
</div></body></html>""")

            del self._pending_keys[key]
            tok     = self._issue_token(key)
            dev_tok = secrets.token_urlsafe(32)
            self._device_sessions[dev_tok] = {"session_key": key}

            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Remote connection established via QR code."}
            ))

            return HTMLResponse(f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width">
<style>
  body{{background:#07090f;color:#dde3ed;font-family:sans-serif;
       display:flex;align-items:center;justify-content:center;height:100vh;margin:0;text-align:center}}
  p{{color:#5e6a7e;font-size:14px}}
</style></head>
<body>
<script>
  sessionStorage.setItem('jarvis_token','{tok}');
  sessionStorage.setItem('jarvis_key','{key}');
  localStorage.setItem('jarvis_device_token','{dev_tok}');
  setTimeout(function(){{location.replace('/')}},400);
</script>
<p>Connecting to JARVIS…</p>
</body></html>""")

        @app.post("/api/device-login")
        async def device_login_ep(req: Request):
            """Return a fresh auth token for a previously paired device token."""
            try:
                body = await req.json()
            except Exception:
                return JSONResponse({"ok": False}, status_code=400)
            dev_tok = (body.get("device_token") or "").strip()
            if not dev_tok or dev_tok not in self._device_sessions:
                return JSONResponse({"ok": False}, status_code=401)
            session_key = self._device_sessions[dev_tok]["session_key"]
            tok = self._issue_token(session_key)
            if self._connect_callback:
                self._connect_callback()
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Known device reconnected automatically."}
            ))
            return JSONResponse({"ok": True, "token": tok, "key": session_key})

        @app.post("/api/revoke-devices")
        async def revoke_devices(req: Request):
            """Invalidate all persistent device tokens (admin action)."""
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            count = len(self._device_sessions)
            self._device_sessions.clear()
            return JSONResponse({"ok": True, "revoked": count})

        @app.post("/api/command")
        async def command(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            body  = await req.json()
            token = req.headers.get("authorization", "").removeprefix("Bearer ").strip()
            enc   = body.get("enc", "")
            if enc:
                text = self._decrypt(token, enc)
                if text is None:
                    return JSONResponse({"error": "Decryption failed"}, status_code=400)
            else:
                text = (body.get("text") or "").strip()
            if text:
                await self._command_queue.put(text)
                if self._wake_callback:
                    self._wake_callback()
            return JSONResponse({"ok": True})

        @app.post("/api/wake")
        async def wake_ep(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            if self._wake_callback:
                self._wake_callback()
            return JSONResponse({"ok": True})

        # ── Phone mic real-time audio → Gemini Live ──────────────────────────

        @app.websocket("/ws/phone-audio")
        async def phone_audio_ws(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or not self._is_token_valid(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._audio_clients.add(websocket)
            asyncio.create_task(self.broadcast(
                {"type": "sys", "text": "Phone microphone live."}
            ))
            try:
                while True:
                    data = await websocket.receive_bytes()
                    try:
                        self._phone_audio_queue.put_nowait(
                            {"data": data, "mime_type": "audio/pcm"}
                        )
                    except asyncio.QueueFull:
                        try:
                            self._phone_audio_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            pass
                        try:
                            self._phone_audio_queue.put_nowait(
                                {"data": data, "mime_type": "audio/pcm"}
                            )
                        except asyncio.QueueFull:
                            pass  # if still full, skip this frame
            except WebSocketDisconnect:
                pass
            finally:
                self._audio_clients.discard(websocket)
                asyncio.create_task(self.broadcast(
                    {"type": "sys", "text": "Phone microphone stopped."}
                ))

        # ── File sharing ──────────────────────────────────────────────────────

        def _safe_filename(raw: str) -> str:
            name = Path(raw).name                          # strip path components
            name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', name).strip(". ")
            return name or "upload"

        if _UPLOAD_OK:
            @app.post("/api/upload")
            async def upload_file(req: Request, file: UploadFile = FastAPIFile(...)):
                if not _auth(req):
                    return JSONResponse({"error": "Unauthorized"}, status_code=401)

                safe = _safe_filename(file.filename or "upload")
                dest = self._uploads_dir / safe
                stem, suffix = Path(safe).stem, Path(safe).suffix
                counter = 1
                while dest.exists():
                    dest = self._uploads_dir / f"{stem}_{counter}{suffix}"
                    counter += 1

                size = 0
                max_bytes = MAX_UPLOAD_MB * 1024 * 1024
                try:
                    with open(dest, "wb") as fout:
                        while True:
                            chunk = await file.read(65536)
                            if not chunk:
                                break
                            size += len(chunk)
                            if size > max_bytes:
                                fout.close()
                                dest.unlink(missing_ok=True)
                                return JSONResponse(
                                    {"error": f"File too large (max {MAX_UPLOAD_MB} MB)"},
                                    status_code=413,
                                )
                            fout.write(chunk)
                except Exception as exc:
                    try:
                        dest.unlink(missing_ok=True)
                    except Exception:
                        pass
                    return JSONResponse({"error": str(exc)}, status_code=500)

                asyncio.create_task(self.broadcast({
                    "type": "file_received",
                    "name": dest.name,
                    "size": size,
                    "saved_to": str(self._uploads_dir),
                }))
                return JSONResponse({"ok": True, "name": dest.name, "size": size})
        else:
            @app.post("/api/upload")
            async def upload_unavailable(req: Request):
                return JSONResponse(
                    {"error": "File uploads require: pip install python-multipart"},
                    status_code=503,
                )

        @app.get("/api/files")
        async def list_files(req: Request):
            if not _auth(req):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            files = []
            try:
                for f in sorted(
                    (p for p in self._uploads_dir.iterdir() if p.is_file()),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                ):
                    files.append({"name": f.name, "size": f.stat().st_size})
            except Exception:
                pass
            return JSONResponse({"files": files})

        @app.get("/uploads/{filename}")
        async def download_file(filename: str, token: str = ""):
            # Auth via query param — browser <a download> can't send custom headers
            tok = token.strip()
            if not tok or not self._is_token_valid(tok):
                return JSONResponse({"error": "Unauthorized"}, status_code=401)
            safe = re.sub(r'[/\\]', '', filename)
            path = self._uploads_dir / safe
            if not path.exists() or not path.is_file():
                return JSONResponse({"error": "Not found"}, status_code=404)
            return FileResponse(str(path), filename=safe)

        @app.websocket("/ws")
        async def ws_ep(websocket: WebSocket, token: str = ""):
            tok = token.strip()
            if not tok or not self._is_token_valid(tok):
                await websocket.close(code=4001)
                return
            await websocket.accept()
            self._clients.add(websocket)
            for entry in self._history[-50:]:
                try:
                    await websocket.send_json(entry)
                except Exception:
                    break
            try:
                while True:
                    data = await websocket.receive_json()
                    message_type = str(data.get("type") or "").strip().lower()
                    if message_type == "ping":
                        await websocket.send_json({"type": "pong"})
                        continue
                    if message_type == "pong":
                        continue
                    if message_type == "command":
                        enc = data.get("enc", "")
                        t   = self._decrypt(tok, enc) if enc else (data.get("text") or "").strip()
                        if t:
                            await self._command_queue.put(t)
                            if self._wake_callback:
                                self._wake_callback()
            except WebSocketDisconnect:
                pass
            finally:
                self._clients.discard(websocket)

        return app

    # ── serve ─────────────────────────────────────────────────────────────

    async def _serve_alias(self) -> None:
        """Second HTTPS server on PORT+1 sharing the same app and in-memory state.
        Chrome HTTPS-upgrades any bare IP:PORT the user types, so this port also needs TLS.
        User types IP:8001 → Chrome tries https → self-signed cert warning → accept once → done."""
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT + 1)
        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT + 1, log_level="warning",
            ssl_keyfile=str(ssl_key), ssl_certfile=str(ssl_cert),
        )
        print(f"[Dashboard] Manual entry:  {self._ip}:{PORT + 1}  (type in browser, accept cert once)")
        await uvicorn.Server(cfg).serve()

    async def serve(self) -> None:
        if not _DEPS_OK:
            print("[Dashboard] fastapi/uvicorn not installed — dashboard disabled.")
            print("[Dashboard] Run:  pip install fastapi 'uvicorn[standard]' cryptography")
            return

        # Firewall setup runs in a thread — uvicorn starts immediately,
        # no waiting for UAC dialogs or subprocess timeouts.
        asyncio.get_event_loop().run_in_executor(None, _ensure_network_access, PORT)
        self._start_public_tunnel()

        use_ssl  = self._ssl_enabled()
        ssl_key  = BASE_DIR / "config" / "certs" / "jarvis.key"
        ssl_cert = BASE_DIR / "config" / "certs" / "jarvis.crt"

        if use_ssl:
            asyncio.create_task(self._serve_alias())

        cfg = uvicorn.Config(
            self.app, host="0.0.0.0", port=PORT, log_level="warning",
            **({"ssl_keyfile": str(ssl_key), "ssl_certfile": str(ssl_cert)} if use_ssl else {}),
        )

        proto = "https" if use_ssl else "http"
        print(f"[Dashboard] {proto}://{self._ip}:{PORT}")

        if not self._public_url and self._tunnel_proc and self._tunnel_proc.poll() is None:
            deadline = time.time() + 8
            while not self._public_url and time.time() < deadline:
                if self._tunnel_proc.poll() is not None:
                    break
                time.sleep(0.25)

        if self._public_url:
            print(f"[Dashboard] Public access: {self._public_url}")
        else:
            print("[Dashboard] Public access disabled. Set JARVIS_ENABLE_TUNNEL=1 or JARVIS_PUBLIC_URL.")
        print("[Dashboard] Press 'Remote Control' in JARVIS UI to get the QR code.")
        await uvicorn.Server(cfg).serve()
