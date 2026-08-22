#computer_settings.py
import json
import re
import sys
import time
import subprocess
import platform
from pathlib import Path

from actions.open_app import _resolve_macos_app_path

try:
    import pyautogui
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE    = 0.05
    _PYAUTOGUI = True
except Exception:
    _PYAUTOGUI = False

try:
    import pyperclip
    _PYPERCLIP = True
except Exception:
    _PYPERCLIP = False

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

if _OS == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}


def _get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

def _get_api_key() -> str:
    path = _get_base_dir() / "config" / "api_keys.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]

def _get_macos_wifi_interface() -> str:
    try:
        result = subprocess.run(
            ["networksetup", "-listallhardwareports"],
            capture_output=True, text=True, timeout=5
        )
        lines = result.stdout.splitlines()
        for i, line in enumerate(lines):
            if "Wi-Fi" in line or "AirPort" in line:
                for j in range(i, min(i + 4, len(lines))):
                    if lines[j].startswith("Device:"):
                        return lines[j].split(":", 1)[1].strip()
    except Exception:
        pass
    return "en0" 

def volume_up():
    if _OS == "Windows":
        for _ in range(5): pyautogui.press("volumeup")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            "set volume output volume (output volume of (get volume settings) + 10)"],
            capture_output=True)
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "+10%"],
            capture_output=True)

def volume_down():
    if _OS == "Windows":
        for _ in range(5): pyautogui.press("volumedown")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            "set volume output volume (output volume of (get volume settings) - 10)"],
            capture_output=True)
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", "-10%"],
            capture_output=True)

def volume_mute():
    if _OS == "Windows":
        pyautogui.press("volumemute")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", "set volume with output muted"],
            capture_output=True)
    else:
        subprocess.run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "toggle"],
            capture_output=True)

def volume_set(value: int):
    value = max(0, min(100, int(value)))
    if _OS == "Windows":
        try:
            import math
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
            devices   = AudioUtilities.GetSpeakers()
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol       = cast(interface, POINTER(IAudioEndpointVolume))
            vol_db    = -65.25 if value == 0 else max(-65.25, 20 * math.log10(value / 100))
            vol.SetMasterVolumeLevel(vol_db, None)
            return
        except Exception as e:
            print(f"[Settings] pycaw failed, using keypress fallback: {e}")
            pyautogui.press("volumemute")
            pyautogui.press("volumemute")
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e", f"set volume output volume {value}"],
            capture_output=True)
        return
    else:
        subprocess.run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{value}%"],
            capture_output=True)
        return

def brightness_up():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to key code 144'],
            capture_output=True)
    elif _OS == "Linux":
        if subprocess.run(["which", "brightnessctl"],
                capture_output=True).returncode == 0:
            subprocess.run(["brightnessctl", "set", "+10%"], capture_output=True)
        else:
            subprocess.run(
                'xrandr --output $(xrandr | grep " connected" | head -1 | cut -d " " -f1)'
                ' --brightness $(python3 -c "import subprocess; '
                'b=float(subprocess.check_output([\"xrandr\",\"--verbose\"]).decode()'
                '.split(\"Brightness:\")[1].split()[0]); print(min(1.0,b+0.1))")',
                shell=True, capture_output=True
            )
    else:
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
                 ".WmiSetBrightness(1, [math]::Min(100, "
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness + 10))"],
                capture_output=True, timeout=5, **_WIN_HIDE
            )
        except Exception as e:
            print(f"[Settings] Brightness up failed on Windows: {e}")

def brightness_down():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to key code 145'],
            capture_output=True)
    elif _OS == "Linux":
        if subprocess.run(["which", "brightnessctl"],
                capture_output=True).returncode == 0:
            subprocess.run(["brightnessctl", "set", "10%-"], capture_output=True)
        else:
            subprocess.run(
                'xrandr --output $(xrandr | grep " connected" | head -1 | cut -d " " -f1)'
                ' --brightness $(python3 -c "import subprocess; '
                'b=float(subprocess.check_output([\"xrandr\",\"--verbose\"]).decode()'
                '.split(\"Brightness:\")[1].split()[0]); print(max(0.1,b-0.1))")',
                shell=True, capture_output=True
            )
    else:
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightnessMethods)"
                 ".WmiSetBrightness(1, [math]::Max(0, "
                 "(Get-WmiObject -Namespace root/wmi -Class WmiMonitorBrightness).CurrentBrightness - 10))"],
                capture_output=True, timeout=5, **_WIN_HIDE
            )
        except Exception as e:
            print(f"[Settings] Brightness down failed on Windows: {e}")

def close_app():
    if _OS == "Darwin": pyautogui.hotkey("command", "q")
    else:               pyautogui.hotkey("alt", "f4")

def _mac_activate_app(app_name: str) -> None:
    if _OS == "Darwin":
        bundle = _resolve_macos_app_path(app_name)
        if bundle:
            app_name = bundle.stem
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
        capture_output=True,
    )

def close_app_named(app_name: str) -> None:
    app_name = app_name.strip()
    if not app_name:
        close_app()
        return
    if _OS == "Darwin":
        aliases = {
            "vscode": "Visual Studio Code",
            "vs code": "Visual Studio Code",
            "code": "Visual Studio Code",
        }
        try:
            normalized = aliases.get(app_name.lower(), app_name)
            bundle = _resolve_macos_app_path(normalized)
            if bundle:
                normalized = bundle.stem

            proc = subprocess.run(
                ["osascript", "-e", f'tell application "{normalized}" to quit'],
                capture_output=True,
                timeout=5,
            )
            if proc.returncode == 0:
                return

            # Fallback: resolve by process name contains, then quit that app.
            script = f'''
            set targetName to "{normalized.replace('"', '\\"')}"
            tell application "System Events"
                repeat with p in application processes
                    try
                        set pName to name of p
                        if pName is not missing value then
                            if (pName as text) contains targetName then
                                tell application (pName as text) to quit
                                return "ok"
                            end if
                        end if
                    end try
                end repeat
            end tell
            return ""
            '''
            proc2 = subprocess.run(["osascript", "-e", script], capture_output=True, timeout=5)
            if proc2.returncode == 0:
                return

            # VS Code-specific fallback by unix process name.
            if normalized.lower() == "visual studio code":
                subprocess.run(["pkill", "-x", "Code"], capture_output=True, timeout=3)
                subprocess.run(["pkill", "-x", "Visual Studio Code"], capture_output=True, timeout=3)
                return
        except Exception:
            _mac_activate_app(app_name)
    close_app()

def quit_app():
    close_app()

def force_quit():
    close_app()

def close_application():
    close_app()

def close_window():
    if _OS == "Darwin": pyautogui.hotkey("command", "w")
    else:               pyautogui.hotkey("ctrl", "w")

def close_window_named(app_name: str) -> None:
    app_name = app_name.strip()
    if not app_name:
        close_window()
        return
    if _OS == "Darwin":
        try:
            bundle = _resolve_macos_app_path(app_name)
            if bundle:
                app_name = bundle.stem
            _mac_activate_app(app_name)
            pyautogui.hotkey("command", "w")
            return
        except Exception:
            pass
    close_window()

def full_screen():
    if _OS == "Darwin": pyautogui.hotkey("ctrl", "command", "f")
    else:               pyautogui.press("f11")

def minimize_window():
    if _OS == "Darwin": pyautogui.hotkey("command", "m")
    else:               pyautogui.hotkey("win", "down")

def maximize_window():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to keystroke "f" '
            'using {control down, command down}'],
            capture_output=True)
    elif _OS == "Windows":
        pyautogui.hotkey("win", "up")
    else:
        try:
            subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-b", "add,maximized_vert,maximized_horz"],
                capture_output=True)
        except Exception:
            pyautogui.hotkey("super", "up")

def snap_left():
    if _OS == "Windows":
        pyautogui.hotkey("win", "left")
    elif _OS == "Darwin":
        # macOS has no built-in snap; try Rectangle app shortcut if installed
        try:
            subprocess.run(["open", "-a", "Rectangle"], capture_output=True, timeout=1)
        except Exception:
            pass
        pyautogui.hotkey("ctrl", "option", "left")
    else:  # Linux
        try:
            subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-e", "0,0,0,960,1080"],
                capture_output=True)
        except Exception:
            pass

def snap_right():
    if _OS == "Windows":
        pyautogui.hotkey("win", "right")
    elif _OS == "Darwin":
        try:
            subprocess.run(["open", "-a", "Rectangle"], capture_output=True, timeout=1)
        except Exception:
            pass
        pyautogui.hotkey("ctrl", "option", "right")
    else:  # Linux
        try:
            subprocess.run(["wmctrl", "-r", ":ACTIVE:", "-e", "0,960,0,960,1080"],
                capture_output=True)
        except Exception:
            pass

def switch_window():
    if _OS == "Darwin": pyautogui.hotkey("command", "tab")
    else:               pyautogui.hotkey("alt", "tab")

def show_desktop():
    if _OS == "Darwin":   pyautogui.hotkey("fn", "f11")
    elif _OS == "Windows": pyautogui.hotkey("win", "d")
    else:                  pyautogui.hotkey("super", "d")

def open_task_manager():
    if _OS == "Windows":
        pyautogui.hotkey("ctrl", "shift", "esc")
    elif _OS == "Darwin":
        subprocess.Popen(["open", "-a", "Activity Monitor"])
    else:
        for cmd in [["gnome-system-monitor"], ["xfce4-taskmanager"], ["htop"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                break


def focus_search():
    if _OS == "Darwin": pyautogui.hotkey("command", "l")
    else:               pyautogui.hotkey("ctrl", "l")

def pause_video():      pyautogui.press("space")

def refresh_page():
    if _OS == "Darwin": pyautogui.hotkey("command", "r")
    else:               pyautogui.press("f5")

def _run_osascript(script: str, timeout: int = 8):
    return subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _list_macos_apps_and_windows() -> str:
    if _OS != "Darwin":
        return "App/window inventory is currently supported on macOS only."

    script = r'''
set outputLines to {}
tell application "System Events"
    repeat with p in application processes
        try
            if background only of p is false then
                set pName to name of p
                set isFront to frontmost of p
                set winCount to count of windows of p
                set end of outputLines to "APP" & tab & pName & tab & (winCount as text) & tab & (isFront as text)
                repeat with w in windows of p
                    try
                        set wName to name of w
                        if wName is missing value or (wName as text) is "" then set wName to "(untitled)"
                        set end of outputLines to "WIN" & tab & pName & tab & (wName as text)
                    end try
                end repeat
            end if
        end try
    end repeat
end tell
if (count of outputLines) is 0 then return ""
set AppleScript's text item delimiters to linefeed
return outputLines as text
'''

    try:
        proc = _run_osascript(script, timeout=10)
    except Exception as e:
        return f"Could not read app inventory: {e}"

    raw = (proc.stdout or proc.stderr or "").strip()
    if not raw:
        return "No visible app windows found."

    apps: dict[str, dict] = {}
    order: list[str] = []
    for line in raw.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        rec_type = parts[0]
        if rec_type == "APP" and len(parts) >= 4:
            app = parts[1].strip()
            if not app:
                continue
            if app not in apps:
                order.append(app)
                apps[app] = {"windows": [], "front": False, "count": 0}
            try:
                apps[app]["count"] = int(parts[2])
            except Exception:
                apps[app]["count"] = 0
            apps[app]["front"] = parts[3].strip().lower() == "true"
        elif rec_type == "WIN" and len(parts) >= 3:
            app = parts[1].strip()
            win = parts[2].strip()
            if not app:
                continue
            if app not in apps:
                order.append(app)
                apps[app] = {"windows": [], "front": False, "count": 0}
            if win and win not in apps[app]["windows"]:
                apps[app]["windows"].append(win)

    if not apps:
        return "No visible app windows found."

    lines = ["Running apps and windows:"]
    for idx, app in enumerate(order, 1):
        item = apps[app]
        marker = " [frontmost]" if item["front"] else ""
        count = item["count"] if item["count"] else len(item["windows"])
        lines.append(f"{idx}. {app}{marker} · windows: {count}")
        for widx, wname in enumerate(item["windows"][:5], 1):
            lines.append(f"   {idx}.{widx} {wname}")
        if len(item["windows"]) > 5:
            lines.append(f"   {idx}.+ ... +{len(item['windows']) - 5} more")

    return "\n".join(lines)

def _safari_tab_inventory() -> list[dict]:
    if _OS != "Darwin":
        return []

    script = r'''
set outputLines to {}
tell application "Safari"
    if not running then return ""
    repeat with wIndex from 1 to count of windows
        repeat with tIndex from 1 to count of tabs of window wIndex
            set theTab to tab tIndex of window wIndex
            set theTitle to name of theTab
            set theURL to URL of theTab
            set end of outputLines to (wIndex as text) & tab & (tIndex as text) & tab & theTitle & tab & theURL
        end repeat
    end repeat
end tell
if (count of outputLines) is 0 then return ""
set AppleScript's text item delimiters to linefeed
return outputLines as text
'''

    try:
        proc = _run_osascript(script)
    except Exception as e:
        print(f"[Settings] Safari tab inventory failed: {e}")
        return []

    raw = (proc.stdout or proc.stderr or "").strip()
    if not raw:
        return []

    inventory: list[dict] = []
    for line in raw.splitlines():
        parts = line.split("\t", 3)
        if len(parts) != 4:
            continue
        try:
            inventory.append(
                {
                    "window": int(parts[0]),
                    "tab": int(parts[1]),
                    "title": parts[2].strip(),
                    "url": parts[3].strip(),
                }
            )
        except Exception:
            continue
    return inventory

def list_safari_tabs() -> str:
    if _OS != "Darwin":
        return "Safari tabs are only supported on macOS."

    tabs = _safari_tab_inventory()
    if not tabs:
        return "No Safari tabs found."

    lines = ["Open Safari tabs:"]
    for idx, tab in enumerate(tabs, 1):
        lines.append(f"{idx}. [window {tab['window']}, tab {tab['tab']}] {tab['title']} — {tab['url']}")
    return "\n".join(lines)

def close_safari_tab(target: str = "", tab_index: int | None = None) -> str:
    if _OS != "Darwin":
        return "Safari tab closing is only supported on macOS."

    if not target and tab_index is None:
        try:
            _mac_activate_app("Safari")
            pyautogui.hotkey("command", "w")
            return "Closed active Safari tab."
        except Exception as e:
            return f"Safari close failed: {e}"

    tabs = _safari_tab_inventory()
    if not tabs:
        return "No Safari tabs found."

    match = None
    if tab_index is not None:
        idx = max(1, int(tab_index)) - 1
        if 0 <= idx < len(tabs):
            match = tabs[idx]
    else:
        needle = (target or "").strip().lower()
        if needle:
            if needle.isdigit():
                idx = int(needle) - 1
                if 0 <= idx < len(tabs):
                    match = tabs[idx]
            else:
                for tab in tabs:
                    title = tab["title"].lower()
                    url = tab["url"].lower()
                    if needle in title or needle in url:
                        match = tab
                        break

    if not match:
        return f"Could not find Safari tab: {target or tab_index}."

    try:
        _run_osascript(
            f'tell application "Safari" to close tab {match["tab"]} of window {match["window"]}'
        )
        return f"Closed Safari tab: {match['title']}"
    except Exception as e:
        return f"Safari close failed: {e}"

def close_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "w")
    else:               pyautogui.hotkey("ctrl", "w")

def new_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "t")
    else:               pyautogui.hotkey("ctrl", "t")

def next_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "shift", "bracketright")
    else:               pyautogui.hotkey("ctrl", "tab")

def prev_tab():
    if _OS == "Darwin": pyautogui.hotkey("command", "shift", "bracketleft")
    else:               pyautogui.hotkey("ctrl", "shift", "tab")

def go_back():
    if _OS == "Darwin": pyautogui.hotkey("command", "left")
    else:               pyautogui.hotkey("alt", "left")

def go_forward():
    if _OS == "Darwin": pyautogui.hotkey("command", "right")
    else:               pyautogui.hotkey("alt", "right")

def zoom_in():
    if _OS == "Darwin": pyautogui.hotkey("command", "equal")
    else:               pyautogui.hotkey("ctrl", "equal")

def zoom_out():
    if _OS == "Darwin": pyautogui.hotkey("command", "minus")
    else:               pyautogui.hotkey("ctrl", "minus")

def zoom_reset():
    if _OS == "Darwin": pyautogui.hotkey("command", "0")
    else:               pyautogui.hotkey("ctrl", "0")

def find_on_page():
    if _OS == "Darwin": pyautogui.hotkey("command", "f")
    else:               pyautogui.hotkey("ctrl", "f")

def reload_page_n(n: int):
    for _ in range(max(1, n)):
        refresh_page()
        time.sleep(0.8)


def scroll_up(amount: int = 500):    pyautogui.scroll(amount)
def scroll_down(amount: int = 500):  pyautogui.scroll(-amount)

def scroll_top():
    if _OS == "Darwin": pyautogui.hotkey("command", "up")
    else:               pyautogui.hotkey("ctrl", "home")

def scroll_bottom():
    if _OS == "Darwin": pyautogui.hotkey("command", "down")
    else:               pyautogui.hotkey("ctrl", "end")

def page_up():   pyautogui.press("pageup")
def page_down(): pyautogui.press("pagedown")


def copy():
    if _OS == "Darwin": pyautogui.hotkey("command", "c")
    else:               pyautogui.hotkey("ctrl", "c")

def paste():
    if _OS == "Darwin": pyautogui.hotkey("command", "v")
    else:               pyautogui.hotkey("ctrl", "v")

def cut():
    if _OS == "Darwin": pyautogui.hotkey("command", "x")
    else:               pyautogui.hotkey("ctrl", "x")

def undo():
    if _OS == "Darwin": pyautogui.hotkey("command", "z")
    else:               pyautogui.hotkey("ctrl", "z")

def redo():
    if _OS == "Darwin": pyautogui.hotkey("command", "shift", "z")
    else:               pyautogui.hotkey("ctrl", "y")

def select_all():
    if _OS == "Darwin": pyautogui.hotkey("command", "a")
    else:               pyautogui.hotkey("ctrl", "a")

def save_file():
    if _OS == "Darwin": pyautogui.hotkey("command", "s")
    else:               pyautogui.hotkey("ctrl", "s")

def press_enter():   pyautogui.press("enter")
def press_escape():  pyautogui.press("escape")
def press_key(key: str): pyautogui.press(key)

def type_text(text: str, press_enter_after: bool = False):
    if not text:
        return
    if _PYPERCLIP:
        pyperclip.copy(str(text))
        time.sleep(0.15)
        paste()
    else:
        pyautogui.write(str(text), interval=0.03)
    if press_enter_after:
        time.sleep(0.1)
        pyautogui.press("enter")

def take_screenshot():
    if _OS == "Windows":
        pyautogui.hotkey("win", "shift", "s")
    elif _OS == "Darwin":
        pyautogui.hotkey("command", "shift", "3")
    else:
        for cmd in [["scrot"], ["gnome-screenshot"], ["import", "-window", "root", "screenshot.png"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                return
        pyautogui.hotkey("ctrl", "print_screen")

def lock_screen():
    if _OS == "Windows":
        pyautogui.hotkey("win", "l")
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"], capture_output=True)
    else:
        for cmd in [
            ["gnome-screensaver-command", "-l"],
            ["xdg-screensaver", "lock"],
            ["loginctl", "lock-session"],
        ]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.run(cmd, capture_output=True)
                return

def open_system_settings():
    if _OS == "Windows":
        pyautogui.hotkey("win", "i")
    elif _OS == "Darwin":
        subprocess.Popen(["open", "-a", "System Preferences"])
    else:
        for cmd in [["gnome-control-center"], ["xfce4-settings-manager"], ["kcmshell5"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                return

def open_file_explorer():
    if _OS == "Windows":
        pyautogui.hotkey("win", "e")
    elif _OS == "Darwin":
        subprocess.Popen(["open", str(Path.home())])
    else:
        for cmd in [["nautilus"], ["thunar"], ["dolphin"], ["nemo"]]:
            if subprocess.run(["which", cmd[0]], capture_output=True).returncode == 0:
                subprocess.Popen(cmd)
                return
        subprocess.Popen(["xdg-open", str(Path.home())])

def sleep_display():
    if _OS == "Windows":
        try:
            import ctypes
            ctypes.windll.user32.SendMessageW(0xFFFF, 0x0112, 0xF170, 2)
        except Exception as e:
            print(f"[Settings] sleep_display failed: {e}")
    elif _OS == "Darwin":
        subprocess.run(["pmset", "displaysleepnow"], capture_output=True)
    else:
        subprocess.run(["xset", "dpms", "force", "off"], capture_output=True)

def open_run():
    if _OS == "Windows":
        pyautogui.hotkey("win", "r")

def dark_mode():
    if _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell app "System Events" to tell appearance preferences '
            'to set dark mode to not dark mode'],
            capture_output=True)
    elif _OS == "Windows":
        try:
            import winreg
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"
            key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS)
            current, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            winreg.SetValueEx(key, "AppsUseLightTheme", 0, winreg.REG_DWORD, 1 - current)
            winreg.SetValueEx(key, "SystemUsesLightTheme", 0, winreg.REG_DWORD, 1 - current)
            winreg.CloseKey(key)
        except Exception as e:
            print(f"[Settings] dark_mode registry failed: {e}")
    else:
        try:
            result = subprocess.run(
                ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
                capture_output=True, text=True
            )
            current = result.stdout.strip()
            new_scheme = "'default'" if "dark" in current else "'prefer-dark'"
            subprocess.run(
                ["gsettings", "set", "org.gnome.desktop.interface", "color-scheme", new_scheme],
                capture_output=True
            )
        except Exception as e:
            print(f"[Settings] dark_mode Linux failed: {e}")

def toggle_wifi():
    if _OS == "Darwin":
        iface = _get_macos_wifi_interface()
        result = subprocess.run(
            ["networksetup", "-getairportpower", iface],
            capture_output=True, text=True
        )
        state = "off" if "On" in result.stdout else "on"
        subprocess.run(["networksetup", "-setairportpower", iface, state],
            capture_output=True)
    elif _OS == "Windows":
        try:
            subprocess.run(
                ["powershell", "-Command",
                 "$adapter = Get-NetAdapter | Where-Object {$_.PhysicalMediaType -eq 'Native 802.11'};"
                 "if ($adapter.Status -eq 'Up') { Disable-NetAdapter -Name $adapter.Name -Confirm:$false }"
                 "else { Enable-NetAdapter -Name $adapter.Name -Confirm:$false }"],
                capture_output=True, timeout=10, **_WIN_HIDE
            )
        except Exception as e:
            print(f"[Settings] toggle_wifi Windows failed: {e}")
    else:
        try:
            result = subprocess.run(["nmcli", "radio", "wifi"], capture_output=True, text=True)
            state  = "off" if "enabled" in result.stdout else "on"
            subprocess.run(["nmcli", "radio", "wifi", state], capture_output=True)
        except Exception as e:
            print(f"[Settings] toggle_wifi Linux failed: {e}")

def restart_computer():
    if _OS == "Windows":
        subprocess.run(["shutdown", "/r", "/t", "10"], capture_output=True, **_WIN_HIDE)
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to restart'],
            capture_output=True)
    else:
        subprocess.run(["systemctl", "reboot"], capture_output=True)

def shutdown_computer():
    if _OS == "Windows":
        subprocess.run(["shutdown", "/s", "/t", "10"], capture_output=True)
    elif _OS == "Darwin":
        subprocess.run(["osascript", "-e",
            'tell application "System Events" to shut down'],
            capture_output=True)
    else:
        subprocess.run(["systemctl", "poweroff"], capture_output=True)

def get_volume() -> dict:
    """Current output volume as {"level": 0-100, "muted": bool}.

    Jarvis could set the volume but had no way to read it, so he had to tell
    the user he didn't know what it was currently at."""
    if _OS == "Darwin":
        try:
            level = subprocess.run(
                ["osascript", "-e", "output volume of (get volume settings)"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            muted = subprocess.run(
                ["osascript", "-e", "output muted of (get volume settings)"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip().lower()
            return {"level": int(level), "muted": muted == "true"}
        except Exception:
            return {"level": None, "muted": None}

    if _OS == "Linux":
        try:
            out = subprocess.run(
                ["pactl", "get-sink-volume", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r"(\d+)%", out)
            muted_out = subprocess.run(
                ["pactl", "get-sink-mute", "@DEFAULT_SINK@"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            return {"level": int(m.group(1)) if m else None, "muted": "yes" in muted_out.lower()}
        except Exception:
            return {"level": None, "muted": None}

    return {"level": None, "muted": None}


def get_brightness() -> int | None:
    """Current display brightness as a percentage, or None if unreadable.

    Read from IOKit via ioreg — no extra tooling required. Apple Silicon
    exposes AppleARMBacklight; Intel Macs use AppleBacklightDisplay, so both
    are tried. External monitors generally report neither, hence None being a
    valid, expected answer rather than an error."""
    if _OS != "Darwin":
        if _OS == "Linux":
            try:
                out = subprocess.run(["brightnessctl", "-m"], capture_output=True, text=True, timeout=5).stdout
                m = re.search(r"(\d+)%", out)
                return int(m.group(1)) if m else None
            except Exception:
                return None
        return None

    for service in ("AppleARMBacklight", "AppleBacklightDisplay"):
        try:
            out = subprocess.run(
                ["ioreg", "-c", service, "-r", "-d", "1"],
                capture_output=True, text=True, timeout=5,
            ).stdout
            m = re.search(r'"brightness"=\{[^}]*"max"=(\d+)[^}]*"value"=(\d+)', out)
            if m:
                max_v, value = int(m.group(1)), int(m.group(2))
                if max_v > 0:
                    return max(0, min(100, round(value / max_v * 100)))
        except Exception:
            continue
    return None


# Each brightness key press moves roughly 1/16 of the range on macOS.
_BRIGHTNESS_STEP_PERCENT = 100 / 16


def brightness_set(value: int) -> str:
    """Set brightness to an approximate percentage.

    macOS exposes no supported way to set brightness directly without extra
    tooling, but now that the CURRENT level is readable, the gap can be closed
    by stepping the media keys and re-reading — which also self-corrects if a
    step lands differently than expected."""
    target = max(0, min(100, int(value)))
    current = get_brightness()
    if current is None:
        return "I can't read the current brightness on this display, sir, so I can't set it precisely."

    for _ in range(24):  # bounded: never loop on an unresponsive display
        current = get_brightness()
        if current is None:
            break
        delta = target - current
        if abs(delta) <= _BRIGHTNESS_STEP_PERCENT / 2:
            break
        try:
            pyautogui.press("brightnessup" if delta > 0 else "brightnessdown")
        except Exception:
            break
        time.sleep(0.12)

    final = get_brightness()
    return f"Brightness is now about {final}%, sir." if final is not None else "Brightness adjusted, sir."


def report_levels() -> str:
    """Spoken-friendly summary of the current display and audio levels."""
    vol = get_volume()
    bright = get_brightness()

    parts: list[str] = []
    if vol.get("level") is None:
        parts.append("I can't read the volume on this system, sir")
    elif vol.get("muted"):
        parts.append(f"Volume is muted (set to {vol['level']}%)")
    else:
        parts.append(f"Volume is at {vol['level']}%")

    if bright is None:
        parts.append("brightness isn't readable on this display")
    else:
        parts.append(f"brightness is at {bright}%")

    return ", and ".join(parts) + "."


ACTION_MAP: dict[str, callable] = {
    "get_volume":          report_levels,
    "get_brightness":      report_levels,
    "get_levels":          report_levels,
    "levels":              report_levels,
    "volume_up":           volume_up,
    "volume_down":         volume_down,
    "mute":                volume_mute,
    "unmute":              volume_mute,
    "toggle_mute":         volume_mute,
    "brightness_up":       brightness_up,
    "brightness_down":     brightness_down,
    "sleep_display":       sleep_display,
    "screen_off":          sleep_display,
    "pause_video":         pause_video,
    "play_pause":          pause_video,
    "close_app":           close_app,
    "close_application":   close_application,
    "quit_app":            quit_app,
    "force_quit":          force_quit,
    "close_window":        close_window,
    "full_screen":         full_screen,
    "fullscreen":          full_screen,
    "minimize":            minimize_window,
    "maximize":            maximize_window,
    "snap_left":           snap_left,
    "snap_right":          snap_right,
    "switch_window":       switch_window,
    "show_desktop":        show_desktop,
    "task_manager":        open_task_manager,
    "focus_search":        focus_search,
    "refresh_page":        refresh_page,
    "reload":              refresh_page,
    "list_tabs":           list_safari_tabs,
    "list_safari_tabs":    list_safari_tabs,
    "close_tab":           close_tab,
    "new_tab":             new_tab,
    "next_tab":            next_tab,
    "prev_tab":            prev_tab,
    "go_back":             go_back,
    "go_forward":          go_forward,
    "zoom_in":             zoom_in,
    "zoom_out":            zoom_out,
    "zoom_reset":          zoom_reset,
    "find_on_page":        find_on_page,
    "scroll_up":           scroll_up,
    "scroll_down":         scroll_down,
    "scroll_top":          scroll_top,
    "scroll_bottom":       scroll_bottom,
    "page_up":             page_up,
    "page_down":           page_down,
    "copy":                copy,
    "paste":               paste,
    "cut":                 cut,
    "undo":                undo,
    "redo":                redo,
    "select_all":          select_all,
    "save":                save_file,
    "enter":               press_enter,
    "escape":              press_escape,
    "screenshot":          take_screenshot,
    "lock_screen":         lock_screen,
    "open_settings":       open_system_settings,
    "file_explorer":       open_file_explorer,
    "open_run":            open_run,
    "dark_mode":           dark_mode,
    "toggle_wifi":         toggle_wifi,
    "restart":             restart_computer,
    "shutdown":            shutdown_computer,
}

_DANGEROUS_ACTIONS = {"restart", "shutdown"}



def _detect_action(description: str) -> dict:

    from google import genai as _genai
    _client = _genai.Client(api_key=_get_api_key())

    available = ", ".join(sorted(ACTION_MAP.keys())) + \
                ", volume_set, brightness_set, type_text, press_key, reload_n, list_apps, list_windows"

    prompt = f"""You are an intent detector for a computer control assistant.

The user issued a command (possibly in any language): "{description}"

Available actions: {available}

Return ONLY a valid JSON object:
{{"action": "action_name", "value": null_or_value}}

Rules:
- Pick the single best matching action from the available list.
- For volume_set and brightness_set: value is an integer 0-100.
- Asking what the volume or brightness currently IS (not changing it) is get_levels.
- For type_text: value is the exact text to type.
- For press_key: value is the key name (e.g. "f5", "tab", "enter").
- For reload_n: value is an integer (number of times to reload).
- If no clear match, pick the closest action.
- Return ONLY the JSON, no explanation, no markdown."""

    try:
        resp = _client.models.generate_content(model="models/gemini-flash-lite-latest", contents=prompt)
        text = re.sub(r"```(?:json)?", "", resp.text).strip().rstrip("`").strip()
        return json.loads(text)
    except Exception as e:
        print(f"[Settings] Intent detection failed: {e}")
        return {"action": description.lower().replace(" ", "_"), "value": None}

def computer_settings(
    parameters: dict = None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    if not _PYAUTOGUI:
        return "pyautogui is not installed. Run: pip install pyautogui"

    params      = parameters or {}
    raw_action  = params.get("action", "").strip()
    description = params.get("description", "").strip()
    value       = params.get("value", None)

    if not raw_action and description:
        detected   = _detect_action(description)
        raw_action = detected.get("action", "")
        if value is None:
            value = detected.get("value")

    action = raw_action.lower().strip().replace(" ", "_").replace("-", "_")

    if not action:
        return "No action could be determined."

    print(f"[Settings] Action: {action}  Value: {value}  OS: {_OS}")
    if player:
        player.write_log(f"[Settings] {action}")

    if action in _DANGEROUS_ACTIONS:
        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return (
                f"This will {action} the computer. "
                f"Please confirm by calling again with confirmed=yes."
            )

    if action == "volume_set":
        try:
            volume_set(int(value or 50))
            return f"Volume set to {value}%."
        except Exception as e:
            return f"Could not set volume: {e}"

    # Handled here rather than via ACTION_MAP because it takes a target value.
    if action in ("brightness_set", "set_brightness"):
        try:
            return brightness_set(int(value or 50))
        except Exception as e:
            return f"Could not set brightness: {e}"

    app_name = (
        str(params.get("app_name", "") or params.get("target", "") or params.get("window", "") or params.get("description", "") or params.get("value", ""))
        .strip()
    )

    if action in ("type_text", "write_on_screen", "type", "write"):
        text = str(value or params.get("text", "")).strip()
        if not text:
            return "No text provided to type."
        enter_after = str(params.get("press_enter", "false")).lower() in ("true", "1", "yes")
        type_text(text, press_enter_after=enter_after)
        return f"Typed: {text[:80]}"

    if action == "press_key":
        key = str(value or params.get("key", "")).strip()
        if not key:
            return "No key specified."
        press_key(key)
        return f"Pressed: {key}"

    if action in ("reload_n", "refresh_n", "reload_page_n"):
        try:
            reload_page_n(int(value or 1))
            return f"Reloaded {value or 1} time(s)."
        except Exception as e:
            return f"Reload failed: {e}"

    if action == "close_app":
        close_app_named(app_name)
        return f"Closed app: {app_name or 'active app'}."

    if action == "close_window":
        close_window_named(app_name)
        return f"Closed window: {app_name or 'active window'}."

    if action == "close_tab":
        safari_target = (
            app_name.lower() == "safari"
            or "safari" in description.lower()
            or "safari" in str(params.get("target", "")).lower()
        )
        if _OS == "Darwin" and safari_target:
            return close_safari_tab(str(params.get("target", "") or description or value or ""))
        close_tab()
        return f"Closed tab: {app_name or 'active tab'}."

    if action in ("list_apps", "list_windows", "list_apps_windows"):
        return _list_macos_apps_and_windows()

    if action in ("list_tabs", "list_safari_tabs"):
        safari_target = not app_name or app_name.lower() == "safari" or "safari" in description.lower()
        if _OS == "Darwin" and safari_target:
            return list_safari_tabs()
        return "Tab listing is only supported for Safari on macOS."

    if action == "scroll_up":
        scroll_up(int(value or 500))
        return "Scrolled up."

    if action == "scroll_down":
        scroll_down(int(value or 500))
        return "Scrolled down."

    func = ACTION_MAP.get(action)
    if not func:
        return f"Unknown action: '{raw_action}'."

    try:
        # Most actions just do a thing; readers (get_volume, get_levels…) return
        # the answer, which must reach the user rather than a generic "Done".
        result = func()
        return result if isinstance(result, str) and result.strip() else f"Done: {action}."
    except Exception as e:
        print(f"[Settings] Action failed ({action}): {e}")
        return f"Action failed ({action}): {e}"