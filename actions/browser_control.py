
from __future__ import annotations

import asyncio
import concurrent.futures
import http.client
import json
import os
import platform
import shutil
import subprocess
import threading
import time
import webbrowser
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
)

from config import get_config

_OS = platform.system()   # "Windows" | "Darwin" | "Linux"

# Fixed remote-debugging port per browser. JARVIS launches browsers with this
# port open (see _open_native) so it can later attach via Chrome DevTools
# Protocol and see every tab across every window of the user's REAL browser
# session — not just tabs opened through automation. Safari has no CDP
# support and isn't listed here; it keeps its AppleScript-based capture path.
_CDP_PORTS: dict[str, int] = {
    "chrome":   9222,
    "edge":     9223,
    "brave":    9224,
    "vivaldi":  9225,
    "opera":    9226,
    "operagx":  9226,
    "firefox":  9227,
}


def _cdp_attach_enabled() -> bool:
    return bool(get_config().get("browser_cdp_attach_enabled", True))


def _cdp_endpoint_reachable(port: int) -> bool:
    """True if a real CDP debugger is listening on this port (not just any process)."""
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=0.35)
        conn.request("GET", "/json/version")
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        if resp.status != 200:
            return False
        return "webSocketDebuggerUrl" in json.loads(data)
    except Exception:
        return False


def _is_mac_app_running(app_name: str) -> bool:
    if _OS != "Darwin":
        return False
    try:
        script = f'tell application "System Events" to (name of processes) contains "{app_name}"'
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
        return (proc.stdout or "").strip().lower() == "true"
    except Exception:
        return False

def _normalize_url(url: str) -> str:
    """
    Bare words like "instagram" → "https://instagram.com"
    Domains like "instagram.com" → "https://instagram.com"
    Full URLs pass through unchanged.
    """
    url = url.strip()
    if not url:
        return "about:blank"
    if "://" in url:
        return url
    # No dot at all → assume .com  (e.g. "instagram" → "instagram.com")
    if "." not in url:
        url = url + ".com"
    return "https://" + url


def _user_agent() -> str:
    if _OS == "Windows":
        return (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    if _OS == "Darwin":
        return (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    return (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )


def _real_profile_dir(browser: str) -> str:
    home  = Path.home()
    local = os.environ.get("LOCALAPPDATA", "")
    roam  = os.environ.get("APPDATA", "")

    candidates: list[Path] = []

    if _OS == "Windows":
        m = {
            "chrome":   [Path(local) / "Google"          / "Chrome"          / "User Data"],
            "edge":     [Path(local) / "Microsoft"        / "Edge"            / "User Data"],
            "brave":    [Path(local) / "BraveSoftware"    / "Brave-Browser"   / "User Data"],
            "vivaldi":  [Path(local) / "Vivaldi"          / "User Data"],
            "opera":    [Path(roam)  / "Opera Software"   / "Opera Stable",
                         Path(local) / "Opera Software"   / "Opera Stable"],
            "operagx":  [Path(roam)  / "Opera Software"   / "Opera GX Stable",
                         Path(local) / "Opera Software"   / "Opera GX Stable"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Darwin":
        lib = home / "Library" / "Application Support"
        m = {
            "chrome":   [lib / "Google"             / "Chrome"],
            "edge":     [lib / "Microsoft Edge"],
            "brave":    [lib / "BraveSoftware"       / "Brave-Browser"],
            "vivaldi":  [lib / "Vivaldi"],
            "opera":    [lib / "com.operasoftware.Opera"],
            "operagx":  [lib / "com.operasoftware.OperaGX"],
        }
        candidates = m.get(browser, [])

    elif _OS == "Linux":
        cfg = home / ".config"
        m = {
            "chrome":   [cfg / "google-chrome", cfg / "chromium"],
            "edge":     [cfg / "microsoft-edge"],
            "brave":    [cfg / "BraveSoftware" / "Brave-Browser"],
            "vivaldi":  [cfg / "vivaldi"],
            "opera":    [cfg / "opera"],
            "operagx":  [cfg / "opera-gx"],
        }
        candidates = m.get(browser, [])

    for p in candidates:
        if p.exists():
            print(f"[Browser] ✅ Real profile found for {browser}: {p}")
            return str(p)

    fallback = home / ".jarvis_profiles" / browser
    fallback.mkdir(parents=True, exist_ok=True)
    print(f"[Browser] ⚠️  Real profile not found for {browser}, using: {fallback}")
    return str(fallback)

def _firefox_profile_dir() -> Optional[str]:
    home = Path.home()

    if _OS == "Windows":
        base = Path(os.environ.get("APPDATA", "")) / "Mozilla" / "Firefox"
    elif _OS == "Darwin":
        base = home / "Library" / "Application Support" / "Firefox"
    else:
        base = home / ".mozilla" / "firefox"

    ini = base / "profiles.ini"
    if not ini.exists():
        return None

    current: dict[str, str] = {}
    default_path: Optional[str] = None

    for line in ini.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if line.startswith("["):
            p = current.get("Path", "")
            if p and current.get("Default") == "1":
                is_rel = current.get("IsRelative", "1") == "1"
                default_path = str(base / p) if is_rel else p
            current = {}
        elif "=" in line:
            k, _, v = line.partition("=")
            current[k.strip()] = v.strip()

    p = current.get("Path", "")
    if p and current.get("Default") == "1":
        is_rel = current.get("IsRelative", "1") == "1"
        default_path = str(base / p) if is_rel else p

    if default_path and Path(default_path).exists():
        print(f"[Browser] Firefox real profile: {default_path}")
        return default_path
    return None

def _find_opera_windows() -> Optional[str]:
    local  = os.environ.get("LOCALAPPDATA", "")
    prog   = os.environ.get("PROGRAMFILES", "")
    prog86 = os.environ.get("PROGRAMFILES(X86)", "")

    candidates = [
        Path(local)  / "Programs" / "Opera"    / "opera.exe",
        Path(local)  / "Programs" / "Opera GX" / "opera.exe",
        Path(prog)   / "Opera"    / "opera.exe",
        Path(prog86) / "Opera"    / "opera.exe",
    ]
    for p in candidates:
        if p.exists():
            print(f"[Browser] Opera found at: {p}")
            return str(p)

    try:
        import winreg
        keys = [
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\opera.exe",
            r"SOFTWARE\Clients\StartMenuInternet\OperaStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\OperaGXStable\shell\open\command",
            r"SOFTWARE\Clients\StartMenuInternet\opera\shell\open\command",
        ]
        for key_path in keys:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        print(f"[Browser] Opera found via registry: {exe}")
                        return exe
                except Exception:
                    continue
    except Exception:
        pass

    return shutil.which("opera") or None

def _find_exe_windows(prog_name: str) -> Optional[str]:
    try:
        import winreg
        paths_to_try = [
            rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{prog_name}.exe",
            rf"SOFTWARE\Clients\StartMenuInternet\{prog_name}\shell\open\command",
        ]
        for key_path in paths_to_try:
            for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
                try:
                    k   = winreg.OpenKey(hive, key_path)
                    val = winreg.QueryValue(k, None)
                    winreg.CloseKey(k)
                    exe = val.strip().strip('"').split('"')[0].split(" --")[0].strip()
                    if exe and Path(exe).exists():
                        return exe
                except Exception:
                    continue
    except Exception:
        pass
    return None

_BROWSER_SPECS: dict[str, dict] = {
    "Windows": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": []},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox.exe"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera.exe"],  "special": "opera_windows"},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": [],             "special": "opera_windows"},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave.exe"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi.exe"]},
        "safari":   None,
    },
    "Darwin": {
        "chrome":   {"engine": "chromium", "channel": "chrome",  "bins": []},
        "edge":     {"engine": "chromium", "channel": "msedge",  "bins": ["microsoft-edge"]},
        "firefox":  {"engine": "firefox",  "channel": None,      "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "operagx":  {"engine": "chromium", "channel": None,      "bins": ["opera"]},
        "brave":    {"engine": "chromium", "channel": None,      "bins": ["brave browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None,      "bins": ["vivaldi"]},
        "safari":   {"engine": "webkit",   "channel": None,      "bins": []},
    },
    "Linux": {
        "chrome":   {"engine": "chromium", "channel": None,
                     "bins": ["google-chrome", "google-chrome-stable", "chromium-browser", "chromium"]},
        "edge":     {"engine": "chromium", "channel": None,
                     "bins": ["microsoft-edge", "microsoft-edge-stable"]},
        "firefox":  {"engine": "firefox",  "channel": None, "bins": ["firefox"]},
        "opera":    {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "operagx":  {"engine": "chromium", "channel": None, "bins": ["opera", "opera-stable"]},
        "brave":    {"engine": "chromium", "channel": None, "bins": ["brave-browser", "brave"]},
        "vivaldi":  {"engine": "chromium", "channel": None, "bins": ["vivaldi-stable", "vivaldi"]},
        "safari":   None,
    },
}

_ALIASES: dict[str, str] = {
    "google chrome":   "chrome",
    "google-chrome":   "chrome",
    "microsoft edge":  "edge",
    "ms edge":         "edge",
    "msedge":          "edge",
    "mozilla firefox": "firefox",
    "opera gx":        "operagx",
    "opera_gx":        "operagx",
}


def _resolve_browser(name: str) -> dict | None:
    name   = _ALIASES.get(name.lower().strip(), name.lower().strip())
    os_map = _BROWSER_SPECS.get(_OS, {})
    spec   = os_map.get(name)
    if spec is None:
        return None

    engine  = spec["engine"]
    channel = spec.get("channel")
    bins    = spec.get("bins", [])
    exe     = None

    if spec.get("special") == "opera_windows":
        exe = _find_opera_windows()
        if not exe:
            print(f"[Browser] ⚠️  Opera executable not found on Windows.")
        return {"engine": engine, "exe": exe, "channel": channel}

    for b in bins:
        found = shutil.which(b)
        if found:
            exe = found
            break

    if not exe and _OS == "Darwin":
        app_names = {
            "chrome":  ["Google Chrome.app"],
            "edge":    ["Microsoft Edge.app"],
            "firefox": ["Firefox.app"],
            "opera":   ["Opera.app", "Opera GX.app"],
            "brave":   ["Brave Browser.app"],
            "vivaldi": ["Vivaldi.app"],
        }
        for app in app_names.get(name, []):
            app_dir = Path("/Applications") / app / "Contents" / "MacOS"
            if app_dir.exists():
                found_bins = list(app_dir.iterdir())
                if found_bins:
                    exe = str(found_bins[0])
                    break

    if not exe and _OS == "Windows" and not channel:
        exe = _find_exe_windows(name)

    return {"engine": engine, "exe": exe, "channel": channel}


def _detect_default_browser() -> str:
    try:
        if _OS == "Windows":
            import winreg
            k = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations"
                r"\UrlAssociations\http\UserChoice",
            )
            prog_id = winreg.QueryValueEx(k, "ProgId")[0].lower()
            winreg.CloseKey(k)
            for kw in ("edge", "firefox", "opera", "brave", "vivaldi", "chrome"):
                if kw in prog_id:
                    return kw
        elif _OS == "Darwin":
            out = subprocess.run(
                ["defaults", "read",
                 "com.apple.LaunchServices/com.apple.launchservices.secure",
                 "LSHandlers"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "safari", "chrome", "edge"):
                if kw in out:
                    return kw
        elif _OS == "Linux":
            out = subprocess.run(
                ["xdg-settings", "get", "default-web-browser"],
                capture_output=True, text=True, timeout=5,
            ).stdout.lower()
            for kw in ("firefox", "opera", "brave", "vivaldi", "chrome", "edge"):
                if kw in out:
                    return kw
    except Exception:
        pass
    return "chrome"


_SEARCH_ENGINES: dict[str, str] = {
    "google":     "https://www.google.com/search?q=",
    "bing":       "https://www.bing.com/search?q=",
    "duckduckgo": "https://duckduckgo.com/?q=",
    "yandex":     "https://yandex.com/search/?text=",
}

_MAC_APP_NAMES: dict[str, str] = {
    "chrome":  "Google Chrome",
    "edge":    "Microsoft Edge",
    "firefox": "Firefox",
    "opera":   "Opera",
    "operagx": "Opera GX",
    "brave":   "Brave Browser",
    "vivaldi": "Vivaldi",
    "safari":  "Safari",
}

_MAC_CLOSE_BROWSER_ORDER = ("safari", "chrome", "edge", "brave", "vivaldi", "opera", "operagx", "firefox")

# Windows registry lookup names for browsers whose spec has no explicit binary
_WIN_EXE_HINTS: dict[str, str] = {"chrome": "chrome", "edge": "msedge"}


def _is_missing_browser_channel_error(exc: Exception) -> bool:
    """True when Playwright channel-based launch failed because browser build is absent."""
    msg = str(exc).lower()
    return (
        "distribution" in msg
        and "is not found" in msg
        and ("chrome" in msg or "msedge" in msg)
    )


def _open_native(url: str, browser_name: Optional[str]) -> str:
    """
    Kullanıcının GERÇEK tarayıcısını normal şekilde açar — kendi profili,
    giriş yapılmış hesapları ve eklentileriyle. Otomasyon bağlanmaz, bu yüzden
    about:blank sekmesi veya boş profil ASLA görünmez.
    url boş ise tarayıcı URL'siz başlatılır (kendi açılış sayfası /
    oturum geri yükleme ile) — tıpkı kullanıcının kendisi açmış gibi.
    Windows / macOS / Linux üçünde de çalışır.
    """
    url = _normalize_url(url) if url and url.strip() else ""
    if url == "about:blank":
        url = ""

    name = None
    if browser_name:
        name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
    elif not url:
        # URL yok → sadece pencere açılacak; varsayılan tarayıcının exe'si gerekir
        name = _detect_default_browser()

    # Specific browser → launch its own executable, exactly like the user would.
    if name:
        cdp_port = _CDP_PORTS.get(name) if _cdp_attach_enabled() else None

        if _OS == "Darwin":
            app = _MAC_APP_NAMES.get(name)
            if app:
                cmd = ["open", "-a", app]
                if cdp_port:
                    # Leaves a CDP debugger port open — no visible effect on
                    # launch (no banner, no UI change) — so a later automation
                    # session can attach and see every tab in the background,
                    # including ones the user opens by hand.
                    cmd += ["--args", f"--remote-debugging-port={cdp_port}"] + ([url] if url else [])
                elif url:
                    cmd += [url]
                try:
                    subprocess.run(cmd, check=True, timeout=10)
                    return f"Opened in {name}: {url}" if url else f"Opened {name}."
                except Exception as e:
                    print(f"[Browser] 'open -a {app}' failed ({e}), trying binary…")

        spec = _resolve_browser(name)
        exe  = spec.get("exe") if spec else None
        if not exe and _OS == "Windows":
            if name in ("opera", "operagx"):
                exe = _find_opera_windows()
            else:
                exe = _find_exe_windows(_WIN_EXE_HINTS.get(name, name))
        if exe:
            try:
                argv = [exe]
                if cdp_port:
                    argv.append(f"--remote-debugging-port={cdp_port}")
                if url:
                    argv.append(url)
                subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return f"Opened in {name}: {url}" if url else f"Opened {name}."
            except Exception as e:
                print(f"[Browser] Native launch failed for {name}: {e}")
        print(f"[Browser] '{name}' not found — falling back to default browser.")

    if not url:
        return "Could not find a browser to open."

    # Default browser via the OS — exactly like the user clicking a link.
    try:
        if _OS == "Windows":
            os.startfile(url)                       # ShellExecute → default browser
        elif _OS == "Darwin":
            subprocess.run(["open", url], check=True, timeout=10)
        else:
            subprocess.Popen(
                ["xdg-open", url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        return f"Opened in your default browser: {url}"
    except Exception:
        try:
            if webbrowser.open(url):
                return f"Opened in your default browser: {url}"
        except Exception:
            pass
        return f"Could not open a browser for: {url}"


class _BrowserSession:
    """
    Bir tarayıcı örneği için tam oturum.
    Tüm tarayıcılar launch_persistent_context ile gerçek profil üzerinde açılır.
    """

    def __init__(self, browser_name: str):
        self.browser_name = browser_name
        self._spec        = _resolve_browser(browser_name)

        self._loop:    asyncio.AbstractEventLoop | None = None
        self._thread:  threading.Thread | None          = None
        self._ready    = threading.Event()

        self._pw:      Playwright     | None = None
        self._context: BrowserContext | None = None
        self._page:    Page           | None = None

        # Set when self._context comes from connect_over_cdp against the
        # user's real, already-running browser rather than a fresh
        # automation-only window. Closing must then only disconnect — never
        # quit the user's actual browser.
        self._cdp_attached: bool             = False
        self._cdp_browser:  Browser | None   = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name=f"BrowserThread-{self.browser_name}",
        )
        self._thread.start()
        self._ready.wait(timeout=20)

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._async_init())
        self._ready.set()
        self._loop.run_forever()

    async def _async_init(self):
        self._pw = await async_playwright().start()

    def run(self, coro, timeout: int = 60) -> str:
        if not self._loop:
            raise RuntimeError(f"Session for '{self.browser_name}' not started.")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout=timeout)

    def close(self):
        if self._loop:
            asyncio.run_coroutine_threadsafe(self._async_close(), self._loop).result(10)

    async def _async_close(self):
        if self._cdp_attached:
            # This Browser came from connect_over_cdp — .close() here only
            # drops JARVIS's automation connection, it does not quit the
            # user's real browser or any of its tabs.
            if self._cdp_browser:
                try:
                    await self._cdp_browser.close()
                except Exception:
                    pass
            self._cdp_browser = None
            self._cdp_attached = False
        elif self._context:
            try:
                await self._context.close()
            except Exception:
                pass
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
        self._context = self._page = None

    async def _adopt_page(self) -> Page:
        """
        launch_persistent_context zaten bir başlangıç sekmesi açar.
        Yeni bir boş sekme (about:blank) açmak yerine o sekmeyi devralır —
        böylece kullanıcı fazladan boş sekme görmez.
        """
        await asyncio.sleep(0.3)
        pages = self._context.pages
        return pages[0] if pages else await self._context.new_page()

    async def _try_cdp_attach(self) -> bool:
        """
        Attach to an already-running real browser via Chrome DevTools
        Protocol instead of launching a fresh automation-only window. Once
        attached, every tab across every window of that real browser becomes
        visible and individually screenshot-able in the background — this is
        what lets JARVIS see tabs the user opened themselves, not only ones
        it launched. Safari/WebKit has no CDP support and never reaches here.
        """
        if self._spec is None or self._spec.get("engine") not in ("chromium", "firefox"):
            return False
        if not _cdp_attach_enabled():
            return False
        port = _CDP_PORTS.get(self.browser_name)
        if port is None or not await asyncio.to_thread(_cdp_endpoint_reachable, port):
            return False

        engine_obj = getattr(self._pw, self._spec["engine"])
        try:
            browser = await engine_obj.connect_over_cdp(f"http://127.0.0.1:{port}")
        except Exception as e:
            print(f"[Browser] CDP attach failed for {self.browser_name}: {e}")
            return False

        context = browser.contexts[0] if browser.contexts else await browser.new_context(no_viewport=True)
        pages   = context.pages
        page    = pages[0] if pages else await context.new_page()

        self._cdp_browser  = browser
        self._context      = context
        self._page         = page
        self._cdp_attached = True
        print(f"[Browser] ✅ Attached to real {self.browser_name} via CDP (port {port}) — {len(pages)} tab(s) visible")
        return True

    async def _launch(self):
        """
        Tarayıcıyı gerçek kullanıcı profiliyle başlatır.
        Context zaten açıksa hiçbir şey yapmaz.
        """
        if self._context is not None:
            return

        if self._spec is None:
            raise RuntimeError(
                f"'{self.browser_name}' bu platformda ({_OS}) desteklenmiyor."
            )

        if await self._try_cdp_attach():
            return

        engine_name = self._spec["engine"]
        exe         = self._spec["exe"]
        channel     = self._spec["channel"]
        engine_obj  = getattr(self._pw, engine_name)

        if engine_name == "firefox":
            profile = _firefox_profile_dir() or str(
                Path.home() / ".jarvis_profiles" / "firefox"
            )
            kwargs: dict = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            if exe:
                kwargs["executable_path"] = exe
            try:
                self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            except Exception as e:
                print(f"[Browser] Firefox real profile failed ({e}), using JARVIS profile")
                jarvis = str(Path.home() / ".jarvis_profiles" / "firefox_jarvis")
                Path(jarvis).mkdir(parents=True, exist_ok=True)
                self._context = await engine_obj.launch_persistent_context(jarvis, **kwargs)

            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Firefox launched")
            return

        if engine_name == "webkit":
            safari_profile = str(Path.home() / ".jarvis_profiles" / "safari")
            Path(safari_profile).mkdir(parents=True, exist_ok=True)
            kwargs = {
                "headless":    False,
                "slow_mo":     0,
                "viewport":    None,
                "no_viewport": True,
                "timeout":     25_000,
            }
            self._context = await engine_obj.launch_persistent_context(safari_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Safari launched")
            return

        profile = _real_profile_dir(self.browser_name)

        kwargs = {
            "headless":    False,
            "slow_mo":     0,
            "viewport":    None,
            "no_viewport": True,
            "timeout":     25_000,
            "args": [
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--disable-default-apps",
                "--no-default-browser-check",
            ],
        }

        if exe:
            kwargs["executable_path"] = exe
        elif channel:
            kwargs["channel"] = channel

        label = (
            f"{self.browser_name}"
            + (f"/{channel}" if channel else "")
            + (f" @ {exe}" if exe else "")
        )
        channel_missing_after_retry = False

        try:
            self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Launched [{label}] profile={profile}")
            return
        except Exception as e:
            print(f"[Browser] ⚠️  Real profile failed for {label}: {e}")

            # If channel-based launch failed (e.g. Chrome not installed),
            # retry with bundled Chromium instead of hard-failing.
            if channel and not exe and _is_missing_browser_channel_error(e):
                channel_missing_after_retry = True
                kwargs.pop("channel", None)
                label = f"{self.browser_name}/playwright"
                print(
                    "[Browser] ⚠️  Browser channel unavailable; "
                    "retrying with Playwright Chromium."
                )
                try:
                    self._context = await engine_obj.launch_persistent_context(profile, **kwargs)
                    self._page = await self._adopt_page()
                    print(f"[Browser] ✅ Launched [{label}] profile={profile}")
                    return
                except Exception as e_retry:
                    print(f"[Browser] ⚠️  Playwright Chromium retry failed: {e_retry}")

        # Gerçek profil açılamadı (tarayıcı zaten açık / kilitli profil / yeni
        # Chrome sürümleri otomasyonla gerçek profili engelliyor). Kalıcı
        # JARVIS otomasyon profiline geçilir — buraya bir kez giriş yapılan
        # hesaplar sonraki oturumlarda da açık kalır.
        jarvis_profile = str(Path.home() / ".jarvis_profiles" / self.browser_name)
        Path(jarvis_profile).mkdir(parents=True, exist_ok=True)
        print(f"[Browser] Retrying with JARVIS profile: {jarvis_profile}")

        try:
            self._context = await engine_obj.launch_persistent_context(jarvis_profile, **kwargs)
            self._page = await self._adopt_page()
            print(f"[Browser] ✅ Launched [{label}] with JARVIS profile "
                  f"(sign-ins persist across sessions)")
        except Exception as e2:
            hint = ""
            if channel_missing_after_retry:
                install_target = "chrome" if self.browser_name == "chrome" else "msedge"
                hint = f" Tip: run 'playwright install {install_target}'."
            raise RuntimeError(f"Could not launch {self.browser_name}: {e2}{hint}") from e2


    async def _get_page(self) -> Page:
        await self._launch()
        # If somehow page got closed, open a fresh one
        if self._page is None or self._page.is_closed():
            self._page = await self._context.new_page()
            await asyncio.sleep(0.2)
        return self._page

    async def go_to(self, url: str) -> str:

        url      = _normalize_url(url)
        page     = await self._get_page()
        prev_url = page.url

        async def _do_goto(p: Page) -> str:
            """Attempt navigation and return the resulting URL (may still be blank)."""
            try:
                await p.goto(url, wait_until="domcontentloaded", timeout=30_000)
                await asyncio.sleep(0.3)
            except PlaywrightTimeout:
                pass   # page may have partially loaded — check URL below
            except Exception as e:
                print(f"[Browser] goto exception (non-fatal): {e}")
            return p.url

        result_url = await _do_goto(page)

        if result_url in ("about:blank", "", None, prev_url) and prev_url in ("about:blank", "", None):
            print(f"[Browser] Still blank after goto — retrying on new tab: {url}")
            try:
                new_page   = await self._context.new_page()
                self._page = new_page
                result_url = await _do_goto(new_page)
            except Exception as e:
                print(f"[Browser] New-tab retry failed: {e}")

        if result_url and result_url not in ("about:blank", "", None):
            return f"Opened: {result_url}"
        return f"Could not open: {url}"

    async def search(self, query: str, engine: str = "google") -> str:
        base = _SEARCH_ENGINES.get(engine.lower(), _SEARCH_ENGINES["google"])
        return await self.go_to(base + query.replace(" ", "+"))

    async def click(self, selector: str = None, text: str = None) -> str:
        page = await self._get_page()
        try:
            if text:
                await page.get_by_text(text, exact=False).first.click(timeout=8_000)
                return f"Clicked text: '{text}'"
            if selector:
                await page.click(selector, timeout=8_000)
                return f"Clicked selector: {selector}"
            return "No selector or text provided."
        except PlaywrightTimeout:
            return "Element not found (timeout)."
        except Exception as e:
            return f"Click error: {e}"

    async def type_text(self, selector: str = None, text: str = "",
                        clear_first: bool = True) -> str:
        page = await self._get_page()
        try:
            el = page.locator(selector).first if selector else page.locator(":focus")
            if clear_first:
                await el.clear()
            await el.type(text, delay=50)
            return "Text typed."
        except Exception as e:
            return f"Type error: {e}"

    async def scroll(self, direction: str = "down", amount: int = 500) -> str:
        page = await self._get_page()
        try:
            y = amount if direction == "down" else -amount
            await page.mouse.wheel(0, y)
            return f"Scrolled {direction}."
        except Exception as e:
            return f"Scroll error: {e}"

    async def press(self, key: str) -> str:
        page = await self._get_page()
        try:
            await page.keyboard.press(key)
            return f"Pressed: {key}"
        except Exception as e:
            return f"Key error: {e}"

    async def get_text(self) -> str:
        page = await self._get_page()
        try:
            text = await page.inner_text("body")
            return text[:4_000]
        except Exception as e:
            return f"Could not get page text: {e}"

    async def get_url(self) -> str:
        page = await self._get_page()
        return page.url

    async def fill_form(self, fields: dict) -> str:
        page    = await self._get_page()
        results = []
        for selector, value in fields.items():
            try:
                el = page.locator(selector).first
                await el.clear()
                await el.type(str(value), delay=40)
                results.append(f"✓ {selector}")
            except Exception as e:
                results.append(f"✗ {selector}: {e}")
        return "Form filled: " + ", ".join(results)

    async def smart_click(self, description: str) -> str:
        page = await self._get_page()
        for role in ("button", "link", "searchbox", "textbox", "menuitem", "tab"):
            try:
                loc = page.get_by_role(role, name=description)
                if await loc.count() > 0:
                    await loc.first.click(timeout=5_000)
                    return f"Clicked ({role}): '{description}'"
            except Exception:
                pass
        for attempt in (
            lambda: page.get_by_text(description, exact=False).first.click(timeout=5_000),
            lambda: page.get_by_placeholder(description, exact=False).first.click(timeout=5_000),
            lambda: page.locator(
                f'[alt*="{description}" i],[title*="{description}" i],'
                f'[aria-label*="{description}" i]'
            ).first.click(timeout=5_000),
        ):
            try:
                await attempt()
                return f"Clicked: '{description}'"
            except Exception:
                pass
        return f"Could not find element: '{description}'"

    async def smart_type(self, description: str, text: str) -> str:
        page = await self._get_page()
        candidates = [
            ("placeholder", page.get_by_placeholder(description, exact=False)),
            ("label",       page.get_by_label(description, exact=False)),
            ("role",        page.get_by_role("textbox", name=description)),
            ("searchbox",   page.get_by_role("searchbox")),
            ("combobox",    page.get_by_role("combobox", name=description)),
        ]
        for method, loc in candidates:
            try:
                el = loc.first
                if await el.count() == 0:
                    continue
                await el.clear()
                await el.type(text, delay=50)
                return f"Typed into ({method}): '{description}'"
            except Exception:
                continue
        return f"Could not find input: '{description}'"

    async def new_tab(self, url: str = "") -> str:
        page = await self._get_page()
        ctx  = page.context
        new  = await ctx.new_page()
        self._page = new
        if url:
            return await self.go_to(url)
        return "New tab opened."

    async def _page_info(self, page: Page) -> tuple[str, str]:
        title = ""
        try:
            title = (await page.title()).strip()
        except Exception:
            pass
        url = ""
        try:
            url = page.url or ""
        except Exception:
            pass
        return (title or "(untitled)", url or "about:blank")

    async def list_tabs(self) -> str:
        await self._launch()
        if not self._context:
            return "No browser tabs available."

        pages = [page for page in self._context.pages if not page.is_closed()]
        if not pages:
            return "No browser tabs available."

        lines = ["Open tabs:"]
        active_page = self._page if self._page and not self._page.is_closed() else None
        for idx, page in enumerate(pages, 1):
            title, url = await self._page_info(page)
            marker = " ◀ active" if page == active_page else ""
            lines.append(f"{idx}. {title} — {url}{marker}")
        return "\n".join(lines)

    async def list_tabs_struct(self) -> list[dict]:
        """Structured tab inventory (browser/index/title/url) for bulk watch registration."""
        await self._launch()
        if not self._context:
            return []
        pages = [page for page in self._context.pages if not page.is_closed()]
        out = []
        for idx, page in enumerate(pages, 1):
            title, url = await self._page_info(page)
            out.append({"browser": self.browser_name, "index": idx, "title": title, "url": url})
        return out

    async def _find_tab(self, index: int | None = None, target: str = "") -> tuple[Page | None, int | None]:
        if not self._context:
            return None, None

        pages = [page for page in self._context.pages if not page.is_closed()]
        if not pages:
            return None, None

        if index is not None:
            idx = max(1, index) - 1
            if 0 <= idx < len(pages):
                return pages[idx], idx
            return None, None

        active_page = self._page if self._page and not self._page.is_closed() else None
        if active_page:
            try:
                idx = pages.index(active_page)
                return active_page, idx
            except ValueError:
                pass

        target = target.strip().lower()
        if not target:
            return None, None

        if target.isdigit():
            idx = int(target) - 1
            if 0 <= idx < len(pages):
                return pages[idx], idx

        for idx, page in enumerate(pages):
            title, url = await self._page_info(page)
            title_l = title.lower()
            url_l   = url.lower()
            if target == title_l or target == url_l or target in title_l or target in url_l:
                return page, idx

        return None, None

    async def switch_tab(self, target: str = "", index: int | None = None) -> str:
        await self._launch()
        page, idx = await self._find_tab(index=index, target=target)
        if not page:
            return f"Could not find tab: '{target or index}'."

        try:
            await page.bring_to_front()
        except Exception:
            pass
        self._page = page
        title, url = await self._page_info(page)
        return f"Switched to tab {idx + 1}: {title} — {url}"

    async def close_tab(self, target: str = "", index: int | None = None) -> str:
        await self._launch()
        if not self._context:
            return "No active tab to close."

        page, idx = await self._find_tab(index=index, target=target)
        if not page:
            page = self._page if self._page and not self._page.is_closed() else None
            if page and self._context:
                pages = [p for p in self._context.pages if not p.is_closed()]
                try:
                    idx = pages.index(page)
                except ValueError:
                    idx = None

        if not page or page.is_closed():
            return "No active tab to close."

        ctx = page.context
        try:
            await page.close()
        except Exception as e:
            return f"Tab close error: {e}"

        pages = [p for p in ctx.pages if not p.is_closed()]
        if pages:
            next_index = idx if idx is not None else len(pages) - 1
            next_index = max(0, min(next_index, len(pages) - 1))
            self._page = pages[next_index]
            try:
                await self._page.bring_to_front()
            except Exception:
                pass
        else:
            self._page = None
        return "Tab closed."

    async def screenshot(self, path: str = None) -> str:
        page = await self._get_page()
        try:
            save_path = path or str(Path.home() / "Desktop" / "jarvis_screenshot.png")
            await page.screenshot(path=save_path, full_page=False)
            return f"Screenshot saved: {save_path}"
        except Exception as e:
            return f"Screenshot error: {e}"

    async def screenshot_bytes(self, index: int | None = None, target: str = "") -> tuple[bytes, str, str]:
        await self._launch()
        if not self._context:
            raise RuntimeError("No browser session available.")

        page, idx = await self._find_tab(index=index, target=target)
        if not page:
            raise RuntimeError(f"Could not find tab: '{target or index}'.")

        try:
            data = await page.screenshot(full_page=False)
        except Exception as e:
            raise RuntimeError(f"Tab screenshot error: {e}") from e

        title, url = await self._page_info(page)
        label = f"browser-tab:{self.browser_name}:{idx + 1}:{title}"
        return data, "image/png", label

    async def back(self) -> str:
        page = await self._get_page()
        try:
            await page.go_back(timeout=10_000)
            return f"Navigated back: {page.url}"
        except Exception as e:
            return f"Back error: {e}"

    async def forward(self) -> str:
        page = await self._get_page()
        try:
            await page.go_forward(timeout=10_000)
            return f"Navigated forward: {page.url}"
        except Exception as e:
            return f"Forward error: {e}"

    async def reload(self) -> str:
        page = await self._get_page()
        try:
            await page.reload(timeout=15_000)
            return f"Page reloaded: {page.url}"
        except Exception as e:
            return f"Reload error: {e}"

    async def close_browser(self) -> str:
        await self._async_close()
        return f"{self.browser_name} closed."

class _SessionRegistry:
    """Tüm aktif tarayıcı oturumlarını yönetir."""

    def __init__(self):
        self._sessions:        dict[str, _BrowserSession] = {}
        self._active_browser:  str                        = ""
        self._lock             = threading.Lock()
        self._last_native_url: str                        = ""

    def has(self, browser_name: str | None = None) -> bool:
        """Bu tarayıcı için (veya hiç) aktif bir otomasyon oturumu var mı?"""
        with self._lock:
            if not browser_name:
                return bool(self._sessions)
            name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
            return name in self._sessions

    def note_native_url(self, url: str) -> None:
        self._last_native_url = url

    def pop_native_url(self) -> str:
        """Son native açılan URL'yi bir kez döndürür (tekrarı önlemek için tüketilir)."""
        url, self._last_native_url = self._last_native_url, ""
        return url

    def _get_or_create(self, browser_name: str) -> _BrowserSession:
        with self._lock:
            if browser_name not in self._sessions:
                sess = _BrowserSession(browser_name)
                sess.start()
                self._sessions[browser_name] = sess
                print(f"[Registry] New session: {browser_name}")
            return self._sessions[browser_name]

    def get(self, browser_name: str | None = None) -> _BrowserSession:
        if not browser_name:
            browser_name = self._active_browser or _detect_default_browser()
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        sess = self._get_or_create(browser_name)
        self._active_browser = browser_name
        return sess

    def switch(self, browser_name: str) -> str:
        browser_name = _ALIASES.get(browser_name.lower().strip(), browser_name.lower().strip())
        self._get_or_create(browser_name)
        self._active_browser = browser_name
        return f"Active browser → {browser_name}"

    def close_one(self, browser_name: str) -> str:
        with self._lock:
            sess = self._sessions.pop(browser_name, None)
        if sess:
            sess.close()
            if self._active_browser == browser_name:
                self._active_browser = ""
            return f"{browser_name} closed."
        return f"No active session for: {browser_name}"

    def close_all(self) -> str:
        with self._lock:
            names    = list(self._sessions.keys())
            sessions = list(self._sessions.values())
            self._sessions.clear()
            self._active_browser = ""
        for s in sessions:
            try:
                s.close()
            except Exception:
                pass

        native_note = ""
        if _OS == "Darwin":
            try:
                native_note = _close_all_native_browsers_macos()
            except Exception:
                native_note = ""

        base = "All browsers closed: " + (", ".join(names) if names else "none")
        return f"{base}. {native_note}".strip()

    def list_sessions(self) -> str:
        with self._lock:
            if not self._sessions:
                return "No active browser sessions."
            lines = []
            for name in self._sessions:
                marker = " ◀ active" if name == self._active_browser else ""
                lines.append(f"  • {name}{marker}")
            return "Open browsers:\n" + "\n".join(lines)


_registry = _SessionRegistry()


def _close_safari_all_native() -> str:
    if _OS != "Darwin":
        return "close_safari_all is only supported on macOS."
    try:
        safari_script = '''
        tell application "Safari"
            if running then
                quit saving no
                return "Closed all Safari windows and quit Safari."
            end if
        end tell
        return "Safari was not running."
        '''
        proc = subprocess.run(
            ["osascript", "-e", safari_script],
            capture_output=True,
            text=True,
            timeout=8,
        )
        msg = (proc.stdout or proc.stderr or "").strip()
        return msg or "Closed all Safari windows and quit Safari."
    except subprocess.TimeoutExpired:
        return "Safari close timed out. Please close Safari manually once, then retry."
    except Exception as e:
        return f"Safari close failed: {e}"


def _close_native_browser_all_tabs(browser_name: str) -> str:
    if _OS != "Darwin":
        return "Native browser tab close is currently supported on macOS only."
    name = _ALIASES.get((browser_name or "").lower().strip(), (browser_name or "").lower().strip())
    if not name:
        return "Please specify a browser for close_browser_all_tabs."
    if name == "safari":
        return _close_safari_all_native()

    app = _MAC_APP_NAMES.get(name)
    if not app:
        return f"Unsupported browser for native tab close: {browser_name}"

    try:
        script = f'''
        tell application "{app}"
            if running then
                try
                    quit saving no
                on error
                    quit
                end try
                return "Closed all {app} tabs/windows."
            end if
        end tell
        return "{app} was not running."
        '''
        proc = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=8)
        msg = (proc.stdout or proc.stderr or "").strip()
        return msg or f"Closed all {app} tabs/windows."
    except subprocess.TimeoutExpired:
        return f"{app} close timed out. Please close it manually once, then retry."
    except Exception as e:
        return f"{app} close failed: {e}"


def _close_all_native_browsers_macos() -> str:
    if _OS != "Darwin":
        return ""
    messages: list[str] = []
    for name in _MAC_CLOSE_BROWSER_ORDER:
        msg = _close_native_browser_all_tabs(name)
        if msg:
            messages.append(msg)
    return " | ".join(messages)


def has_active_browser_session(browser: str | None = None) -> bool:
    """
    True when JARVIS already has a Playwright automation session driving this
    browser. That session can screenshot any tab by index/title via CDP
    without bringing it to the front — unlike OS-level window capture, which
    only ever sees whatever tab is currently active on screen.
    """
    return _registry.has(browser)


def capture_browser_tab(parameters: dict | None = None) -> tuple[bytes, str, str]:
    """Capture a focused browser tab screenshot as image bytes for vision analysis."""
    params = parameters or {}
    browser = str(params.get("browser", "") or "").lower().strip() or None
    target = str(params.get("target", "") or "").strip()
    index = params.get("index")
    if index in ("", None):
        index = None
    else:
        try:
            index = int(index)
        except Exception:
            index = None

    if not _registry.has(browser):
        raise RuntimeError("No active browser session for targeted tab capture. Open the browser through JARVIS first.")

    sess = _registry.get(browser)
    return sess.run(sess.screenshot_bytes(index=index, target=target))

def browser_control(
    parameters:    dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    params  = parameters or {}
    action  = params.get("action", "").lower().strip()
    browser = params.get("browser", "").lower().strip() or None
    result  = "Unknown action."

    if action == "switch":
        target = browser or params.get("target", "").lower().strip()
        result = _registry.switch(target) if target else "Please specify a browser."
        _log(player, result)
        return result

    if action == "list_browsers":
        result = _registry.list_sessions()
        _log(player, result)
        return result

    if action == "list_tabs":
        target_browser = browser
        if _registry.has(target_browser):
            sess = _registry.get(target_browser)
            try:
                result = sess.run(sess.list_tabs())
            except concurrent.futures.TimeoutError:
                result = "Browser action 'list_tabs' timed out (60s)."
            except Exception as e:
                result = f"Browser error (list_tabs): {e}"
        else:
            result = "No active browser session. Open a browser first, then ask again."
        _log(player, result)
        return result

    if action == "close_tab":
        target_browser = browser
        if _registry.has(target_browser):
            sess = _registry.get(target_browser)
            try:
                result = sess.run(sess.close_tab(params.get("target", ""), params.get("index")))
            except concurrent.futures.TimeoutError:
                result = "Browser action 'close_tab' timed out (60s)."
            except Exception as e:
                result = f"Browser error (close_tab): {e}"
        else:
            if _OS == "Darwin" and target_browser:
                result = _close_native_browser_all_tabs(target_browser)
            else:
                result = "No active browser session. Nothing to close."
        _log(player, result)
        return result

    if action == "close_all":
        result = _registry.close_all()
        _log(player, result)
        return result

    if action == "close_safari_all":
        result = _close_safari_all_native()
        _log(player, result)
        return result

    if action == "close_browser_all_tabs":
        target = browser or params.get("target", "")
        result = _close_native_browser_all_tabs(str(target or ""))
        _log(player, result)
        return result

    if action == "close":
        target = browser or _registry._active_browser
        result = _registry.close_one(target) if target else "No browser specified."
        _log(player, result)
        return result

    # ── Gezinme HER ZAMAN native ─────────────────────────────────────────────
    # go_to / search / new_tab siteyi kullanıcının kendi tarayıcısında açar —
    # kendi profili, giriş yapılmış hesapları ve açılış sayfasıyla; tıpkı
    # kullanıcının kendisi açmış gibi. about:blank'li kontrollü pencere burada
    # asla açılmaz. Tek istisna: hâlihazırda süren bir otomasyon akışı varsa
    # gezinme o pencerede devam eder (çok adımlı görevler bölünmesin diye).
    if action in ("go_to", "search", "new_tab"):
        if _registry.has(browser):
            sess = _registry.get(browser)
            try:
                if action == "search":
                    result = sess.run(sess.search(params.get("query", ""),
                                                  params.get("engine", "google")))
                elif action == "new_tab":
                    result = sess.run(sess.new_tab(params.get("url", "")))
                else:
                    result = sess.run(sess.go_to(params.get("url", "")))
            except concurrent.futures.TimeoutError:
                result = f"Browser action '{action}' timed out (60s)."
            except Exception as e:
                result = f"Browser error ({action}): {e}"
            _log(player, result)
            return result

        if action == "search":
            base    = _SEARCH_ENGINES.get(params.get("engine", "google").lower(),
                                          _SEARCH_ENGINES["google"])
            nav_url = base + params.get("query", "").replace(" ", "+")
        else:
            nav_url = params.get("url", "").strip()

        result = _open_native(nav_url, browser)
        if result.startswith("Opened") and nav_url:
            _registry.note_native_url(_normalize_url(nav_url))
        _log(player, result)
        return result

    # ── Etkileşimli aksiyonlar (tıklama/yazma/okuma…) ────────────────────────
    # Bunlar fiziksel olarak kontrol edilebilir bir tarayıcı gerektirir;
    # yalnızca burada otomasyon penceresi açılır ve açılır açılmaz kullanıcının
    # son gezindiği sayfaya gider — boş sayfada beklemez.
    try:
        sess = _registry.get(browser)
    except Exception as e:
        result = f"Could not start browser session: {e}"
        _log(player, result)
        return result

    try:
        last = _registry.pop_native_url()
        if last:
            try:
                sess.run(sess.go_to(last))
            except Exception as e:
                print(f"[Browser] Could not resume last page ({last}): {e}")

        if action == "click":
            result = sess.run(sess.click(params.get("selector"), params.get("text")))
        elif action == "type":
            result = sess.run(sess.type_text(
                params.get("selector"), params.get("text", ""), params.get("clear_first", True)))
        elif action == "scroll":
            result = sess.run(sess.scroll(params.get("direction", "down"), int(params.get("amount", 500))))
        elif action == "fill_form":
            result = sess.run(sess.fill_form(params.get("fields", {})))
        elif action == "smart_click":
            result = sess.run(sess.smart_click(params.get("description", "")))
        elif action == "smart_type":
            result = sess.run(sess.smart_type(params.get("description", ""), params.get("text", "")))
        elif action == "get_text":
            result = sess.run(sess.get_text())
        elif action == "get_url":
            result = sess.run(sess.get_url())
        elif action == "press":
            result = sess.run(sess.press(params.get("key", "Enter")))
        elif action == "switch_tab":
            result = sess.run(sess.switch_tab(params.get("target", ""), params.get("index")))
        elif action == "screenshot":
            result = sess.run(sess.screenshot(params.get("path")))
        elif action == "back":
            result = sess.run(sess.back())
        elif action == "forward":
            result = sess.run(sess.forward())
        elif action == "reload":
            result = sess.run(sess.reload())
        else:
            result = f"Unknown browser action: '{action}'"

    except concurrent.futures.TimeoutError:
        result = f"Browser action '{action}' timed out (60s)."
    except Exception as e:
        result = f"Browser error ({action}): {e}"

    _log(player, result)
    return result


def _log(player, text: str):
    short = str(text)[:80]
    print(f"[Browser] {short}")
    if player:
        player.write_log(f"[browser] {short[:60]}")