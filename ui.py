from __future__ import annotations

import json
import hashlib
import math
import os
import platform
import random
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlparse
import re
import functools

import psutil

if (
    os.environ.get("QT_QPA_PLATFORM") is None
    and os.environ.get("DISPLAY") is None
    and platform.system() == "Linux"
):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


if platform.system() == "Windows":
    _WIN_HIDE: dict = {"creationflags": subprocess.CREATE_NO_WINDOW}
else:
    _WIN_HIDE: dict = {}

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QPointF, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QConicalGradient, QDragEnterEvent, QDropEvent, QFont,
    QFontDatabase, QImage, QKeySequence, QLinearGradient, QPainter, QPainterPath,
    QPen, QPixmap, QRadialGradient, QShortcut,
)
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QInputDialog, QMainWindow, QPushButton, QScrollArea, QSizePolicy, QSplitter,
    QStackedWidget, QTextEdit, QVBoxLayout, QWidget, QProgressBar,
)

from actions.file_controller import (
    check_override_rate_limit,
    enroll_biometric_profile,
    establish_biometric_baseline,
    evaluate_live_biometric_security,
    get_authorized_profiles,
    has_override_code_configured,
    record_override_attempt,
    set_override_code,
    verify_biometric_security,
    verify_override_code,
    _append_override_audit_log,
)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"
PROFILES_FILE = CONFIG_DIR / "authorized_profiles.json"

_config_cache: dict = {}
_config_cache_ts: float = 0.0
_config_lock = threading.Lock()

def _read_full_config() -> dict:
    """Read api_keys.json config dict with memory caching for high frequency polling efficiency."""
    global _config_cache, _config_cache_ts
    with _config_lock:
        now = time.time()
        if now - _config_cache_ts < 2.0 and _config_cache:
            return _config_cache
        try:
            if API_FILE.exists():
                _config_cache = json.loads(API_FILE.read_text(encoding="utf-8"))
            else:
                _config_cache = {}
            _config_cache_ts = now
            return _config_cache
        except Exception:
            return _config_cache if _config_cache else {}


_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#00060a"
    PANEL     = "#010d14"
    PANEL2    = "#010f18"
    BORDER    = "#0d3347"
    BORDER_B  = "#1a5c7a"
    BORDER_A  = "#0f4060"
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#8ffcff"
    TEXT_DIM  = "#3a8a9a"
    TEXT_MED  = "#5ab8cc"
    WHITE     = "#d8f8ff"
    DARK      = "#000d14"
    BAR_BG    = "#011520"


_HUE_LINKED = (
    "BG", "PANEL", "PANEL2", "BORDER", "BORDER_B", "BORDER_A",
    "PRI", "PRI_DIM", "PRI_GHO", "TEXT", "TEXT_DIM", "TEXT_MED",
    "WHITE", "DARK", "BAR_BG",
)
_PALETTE_DEFAULTS: dict[str, str] = {k: getattr(C, k) for k in _HUE_LINKED}

DEFAULT_UI_COLOR = _PALETTE_DEFAULTS["PRI"]


def apply_ui_accent(accent_hex: str) -> bool:
    import colorsys

    accent_hex = (accent_hex or "").strip().lower()
    if not (accent_hex.startswith("#") and len(accent_hex) == 7):
        return False
    try:
        int(accent_hex[1:], 16)
    except ValueError:
        return False

    def _hsv(h: str) -> tuple[float, float, float]:
        r = int(h[1:3], 16) / 255
        g = int(h[3:5], 16) / 255
        b = int(h[5:7], 16) / 255
        return colorsys.rgb_to_hsv(r, g, b)

    base_h            = _hsv(_PALETTE_DEFAULTS["PRI"])[0]
    acc_h, acc_s, _av = _hsv(accent_hex)
    dh   = acc_h - base_h
    grey = acc_s < 0.08

    for key, hex0 in _PALETTE_DEFAULTS.items():
        h, s, v = _hsv(hex0)
        if grey:
            s *= 0.15
        r, g, b = colorsys.hsv_to_rgb((h + dh) % 1.0, s, v)
        setattr(C, key, "#{:02x}{:02x}{:02x}".format(
            int(r * 255 + 0.5), int(g * 255 + 0.5), int(b * 255 + 0.5)))
    return True


def current_palette() -> dict[str, str]:
    return {k: getattr(C, k) for k in _HUE_LINKED}


def _ensure_qapplication() -> QApplication | None:
    app = QApplication.instance()
    if app is None:
        if (
            os.environ.get("QT_QPA_PLATFORM") is None
            and os.environ.get("DISPLAY") is None
            and platform.system() == "Linux"
        ):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        try:
            app = QApplication([])
        except Exception:
            return None
    return app


def retheme_all_widgets(old: dict[str, str], new: dict[str, str]) -> None:
    mapping = {old[k].lower(): new[k].lower()
               for k in old if old[k].lower() != new.get(k, old[k]).lower()}
    if not mapping:
        return
    app = QApplication.instance()
    if app is None:
        return
    for w in app.allWidgets():
        try:
            ss = w.styleSheet()
            if ss:
                s2 = ss
                for o, n in mapping.items():
                    if o in s2:
                        s2 = s2.replace(o, n)
                if s2 != ss:
                    w.setStyleSheet(s2)
            w.update()
        except Exception:
            pass


@functools.lru_cache(maxsize=128)
def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


_nvml_lib: object = None
_nvml_ok:  object = None


def _nvml_gpu_windows() -> float:
    global _nvml_lib, _nvml_ok
    if _nvml_ok is False:
        return -1.0
    try:
        import ctypes

        class _Util(ctypes.Structure):
            _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

        if _nvml_lib is None:
            for dll_name in ("nvml", r"C:\Windows\System32\nvml.dll"):
                try:
                    lib = ctypes.WinDLL(dll_name)
                    lib.nvmlInit_v2()
                    _nvml_lib = lib
                    break
                except Exception:
                    continue

        if _nvml_lib is None:
            try:
                import pynvml  # type: ignore[import-not-found]
            except Exception:
                pynvml = None
            if pynvml is None:
                _nvml_ok = False
                return -1.0
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            _nvml_ok = True
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)

        dev = ctypes.c_void_p()
        _nvml_lib.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
        util = _Util()
        _nvml_lib.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(util))
        _nvml_ok = True
        return float(util.gpu)
    except Exception:
        _nvml_ok = False
        return -1.0


class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        self._thread = None
        if self._should_start_thread():
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()

    def _should_start_thread(self) -> bool:
        if os.environ.get("PYTEST_CURRENT_TEST") is not None:
            return False
        if os.environ.get("JARVIS_HEADLESS") is not None:
            return False
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return False
        if platform.system() != "Windows":
            if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
                return False
        return True

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(3.0)

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()
        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        try:
            import pynvml  # type: ignore[import-not-found]
            pynvml.nvmlInit()
            h = pynvml.nvmlDeviceGetHandleByIndex(0)
            return float(pynvml.nvmlDeviceGetUtilizationRates(h).gpu)
        except Exception:
            pass

        if _OS == "Windows":
            return _nvml_gpu_windows()

        try:
            import ctypes
            _lib = "libnvidia-ml.so.1" if _OS == "Linux" else "libnvidia-ml.dylib"

            class _Util(ctypes.Structure):
                _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]

            nv = ctypes.CDLL(_lib)
            nv.nvmlInit_v2()
            dev = ctypes.c_void_p()
            nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(dev))
            u = _Util()
            nv.nvmlDeviceGetUtilizationRates(dev, ctypes.byref(u))
            return float(u.gpu)
        except Exception:
            pass

        return -1.0

    def _get_temp(self) -> float:
        try:
            temps = psutil.sensors_temperatures()
            for name in ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                         "cpu-thermal", "zenpower", "it8688"]:
                if name in temps and temps[name]:
                    return temps[name][0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass

        if _OS == "Windows":
            try:
                import wmi  # type: ignore[import-not-found]
                w = wmi.WMI(namespace="root/wmi")
                tz = w.MSAcpi_ThermalZoneTemperature()
                if tz:
                    return (tz[0].CurrentTemperature / 10.0) - 273.15
            except Exception:
                pass

        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, assistant_name: str = "J.A.R.V.I.S", parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self._assistant_name = assistant_name

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._load_face(face_path)

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(33)

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _step(self):
        self._tick += 1
        now = time.time()
        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now

        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75)) % 360

        fw  = min(self.width(), self.height())
        lim = fw * 0.74
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                p.drawPoint(x, y)

        r_face = fw * 0.31

        for i in range(10):
            r   = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a   = max(0, min(255, int(self._halo * 0.085 * frc)))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        for pr in self._pulses:
            a   = max(0, int(230 * (1.0 - pr / (fw * 0.74))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r)); p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self.speaking else 44
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        t_out, t_in = fw * 0.497, fw * 0.474
        p.setPen(QPen(qcol(C.PRI, 140), 1))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 6
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
            )

        ch_r, gap_h = fw * 0.51, fw * 0.16
        p.setPen(QPen(qcol(C.PRI, int(self._halo * 0.5)), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

        bl = 24
        bc = qcol(C.PRI, 210)
        hl, hr = cx - fw // 2, cx + fw // 2
        ht, hb = cy - fw // 2, cy + fw // 2
        p.setPen(QPen(bc, 2))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        if self._face_px:
            fsz    = int(fw * 0.62 * self._scale)
            scaled = self._face_px.scaled(
                fsz, fsz,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), scaled)
        else:
            orb_r = int(fw * 0.27 * self._scale)
            oc    = (200, 0, 50) if self.muted else (0, 60, 110)
            for i in range(8, 0, -1):
                r2  = int(orb_r * i / 8)
                frc = i / 8
                a   = max(0, min(255, int(self._halo * 1.1 * frc)))
                p.setBrush(QBrush(QColor(int(oc[0]*frc), int(oc[1]*frc), int(oc[2]*frc), a)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(C.PRI, min(255, int(self._halo * 2))), 1))
            p.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
            p.drawText(QRectF(cx - 80, cy - 14, 160, 28),
                       Qt.AlignmentFlag.AlignCenter, self._assistant_name)

        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        sy = cy + fw * 0.40
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            elif self.speaking:
                hgt = random.randint(3, 20)
                cl  = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
            else:
                hgt = int(3 + 2 * math.sin(self._tick * 0.09 + i * 0.6))
                cl  = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._ai_name_lc = "jarvis"
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        _ai_pfx = f"{self._ai_name_lc}:"
        if   tl.startswith("you:"):                              self._tag = "you"
        elif tl.startswith(_ai_pfx) or tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):                             self._tag = "file"
        elif "err" in tl:                                        self._tag = "err"
        else:                                                    self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._current_files: list[str] = []
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        paths = []
        for url in urls:
            path = url.toLocalFile()
            if Path(path).is_file():
                paths.append(path)
        if paths:
            self._set_files(paths)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def current_files(self) -> list[str]:
        return list(self._current_files)

    def clear_file(self):
        self._current_file = None
        self._current_files = []
        self._canvas.update()

    def _browse(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if paths:
            self._set_files(paths)

    def _set_files(self, paths: list[str]):
        uniq: list[str] = []
        for raw in paths:
            path = str(Path(raw))
            if Path(path).is_file() and path not in uniq:
                uniq.append(path)
        if not uniq:
            return
        self._current_file = uniq[-1]
        self._current_files = uniq
        self._canvas.update()
        self.file_selected.emit(uniq if len(uniq) > 1 else uniq[0])


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class _CameraPreview(QWidget):
    _W, _H = 244, 188

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            _CameraPreview {{
                background: rgba(0, 6, 10, 242);
                border: 1px solid {C.PRI};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 5, 6, 6)
        lay.setSpacing(4)

        hdr = QHBoxLayout()
        title = QLabel("◈  VISUAL INPUT")
        title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(title)
        hdr.addStretch()
        close_btn = QPushButton("✕")
        close_btn.setFixedSize(16, 16)
        close_btn.setFont(QFont("Courier New", 8))
        close_btn.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: transparent; border: none;"
        )
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.clicked.connect(self.hide)
        hdr.addWidget(close_btn)
        lay.addLayout(hdr)

        self._img_lbl = QLabel()
        self._img_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_lbl.setStyleSheet("background: transparent;")
        lay.addWidget(self._img_lbl)

        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)

        self.hide()

    def show_frame(self, img_bytes: bytes) -> None:
        px = QPixmap()
        px.loadFromData(img_bytes)
        if not px.isNull():
            max_w = self._W - 12
            scaled = px.scaled(
                max_w, 160,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._img_lbl.setPixmap(scaled)
            self._img_lbl.setFixedSize(scaled.width(), scaled.height())
            self.adjustSize()
        self.show()
        self.raise_()
        self._timer.start(6_000)


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


class ManageProfilesOverlay(QWidget):
    """
    Profile management overlay for BiometricLock_Protocol.
    Allows viewing, adding, and removing authorized profiles (voice & visual signatures).
    """
    def __init__(self, parent=None):
        self._qt_ready = False
        app = _ensure_qapplication()
        if app is None:
            return
        self._qt_ready = True
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ManageProfilesOverlay {{
                background: rgba(0, 6, 12, 248);
                border: 1px solid {C.PRI};
                border-radius: 8px;
            }}
        """)
        self.setFixedSize(480, 400)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(20, 18, 20, 18)
        lay.setSpacing(8)

        title = QLabel("👥 AUTHORIZED PROFILES MANAGER")
        title.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        sub = QLabel("Manage biometric access credentials and voice/visual signatures.")
        sub.setFont(QFont("Courier New", 8))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        lay.addWidget(sep)

        self._profile_list_edit = QTextEdit()
        self._profile_list_edit.setReadOnly(True)
        self._profile_list_edit.setFont(QFont("Courier New", 8))
        self._profile_list_edit.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL}; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 4px; padding: 6px;
            }}
        """)
        lay.addWidget(self._profile_list_edit, stretch=1)

        form_row = QHBoxLayout()
        form_row.setSpacing(6)
        self._new_name_input = QLineEdit()
        self._new_name_input.setPlaceholderText("Profile Name (e.g. James Lumsden)")
        self._new_name_input.setFont(QFont("Courier New", 8))
        self._new_name_input.setFixedHeight(30)
        self._new_name_input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.PANEL2}; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        form_row.addWidget(self._new_name_input, stretch=2)

        add_btn = QPushButton("ADD PROFILE")
        add_btn.setFixedHeight(30)
        add_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: #002010; }}
        """)
        add_btn.clicked.connect(self._add_profile)
        form_row.addWidget(add_btn, stretch=1)
        lay.addLayout(form_row)

        self._voice_input = QLineEdit()
        self._voice_input.setPlaceholderText("Voice signature text")
        self._voice_input.setFont(QFont("Courier New", 8))
        self._voice_input.setFixedHeight(30)
        self._voice_input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.PANEL2}; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px;
            }}
        """)
        lay.addWidget(self._voice_input)

        self._visual_input = QLineEdit()
        self._visual_input.setPlaceholderText("Visual signature text")
        self._visual_input.setFont(QFont("Courier New", 8))
        self._visual_input.setFixedHeight(30)
        self._visual_input.setStyleSheet(f"""
            QLineEdit {{
                background: {C.PANEL2}; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 2px 6px;
            }}
        """)
        lay.addWidget(self._visual_input)

        self._setup_status = QLabel("Setup: use the button below to capture your live voice and face baseline.")
        self._setup_status.setFont(QFont("Courier New", 8))
        self._setup_status.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._setup_status.setWordWrap(True)
        lay.addWidget(self._setup_status)

        self._preview_frame = QFrame()
        self._preview_frame.setFixedHeight(140)
        self._preview_frame.setStyleSheet(f"""
            QFrame {{
                background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 6px;
            }}
        """)
        preview_layout = QVBoxLayout(self._preview_frame)
        preview_layout.setContentsMargins(8, 8, 8, 8)
        preview_layout.setSpacing(4)

        preview_title = QLabel("◉ LIVE CAMERA PREVIEW")
        preview_title.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        preview_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        preview_layout.addWidget(preview_title)

        self._preview_placeholder = QLabel("Camera preview will appear here while the baseline is being captured.")
        self._preview_placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._preview_placeholder.setWordWrap(True)
        self._preview_placeholder.setFont(QFont("Courier New", 7))
        self._preview_placeholder.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        preview_layout.addWidget(self._preview_placeholder, stretch=1)

        self._camera_cap = None
        self._preview_timer = None
        self._capture_state_text = "WAITING"
        self._setup_name = ""

        self._speak_indicator = QLabel("● WAITING")
        self._speak_indicator.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._speak_indicator.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._speak_indicator.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        preview_layout.addWidget(self._speak_indicator)
        lay.addWidget(self._preview_frame)

        self._setup_countdown_label = QLabel("")
        self._setup_countdown_label.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._setup_countdown_label.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        self._setup_countdown_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._setup_countdown_label)

        self._confirm_btn = QPushButton("CONFIRM BASELINE")
        self._confirm_btn.setFixedHeight(34)
        self._confirm_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._confirm_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._confirm_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.GREEN};
                border: 1px solid {C.GREEN_D}; border-radius: 4px;
            }}
            QPushButton:hover {{ background: #001a0f; }}
        """)
        self._confirm_btn.clicked.connect(self._confirm_baseline)
        self._confirm_btn.hide()
        lay.addWidget(self._confirm_btn)

        setup_btn = QPushButton("ESTABLISH LIVE BASELINE")
        setup_btn.setFixedHeight(34)
        setup_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        setup_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        setup_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.ACC2};
                border: 1px solid {C.ACC2}; border-radius: 4px;
            }}
            QPushButton:hover {{ background: #241900; }}
        """)
        setup_btn.clicked.connect(self._establish_baseline)
        lay.addWidget(setup_btn)

        override_btn = QPushButton(
            "CHANGE OVERRIDE CODE" if has_override_code_configured() else "SET OVERRIDE CODE"
        )
        override_btn.setFixedHeight(34)
        override_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        override_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        override_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.RED};
                border: 1px solid {C.RED}; border-radius: 4px;
            }}
            QPushButton:hover {{ background: #200010; }}
        """)
        self._override_btn = override_btn
        override_btn.clicked.connect(self._set_override_code)
        lay.addWidget(override_btn)

        close_btn = QPushButton("CLOSE")
        close_btn.setFixedHeight(34)
        close_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self.hide)
        lay.addWidget(close_btn)

        self._load_profiles()

    def _set_override_code(self) -> None:
        code, ok = QInputDialog.getText(
            self,
            "Manual Override Code",
            "Enter a new manual override code (min. 8 characters):",
            QLineEdit.EchoMode.Password,
            "",
        )
        if not ok or not code:
            return

        confirm, ok = QInputDialog.getText(
            self,
            "Manual Override Code",
            "Re-enter the override code to confirm:",
            QLineEdit.EchoMode.Password,
            "",
        )
        if not ok:
            return
        if confirm != code:
            self._setup_status.setText("Override code entries did not match. Not saved.")
            return

        success, message = set_override_code(code)
        self._setup_status.setText(message)
        if success:
            self._override_btn.setText("CHANGE OVERRIDE CODE")

    def _safe_set_widget_text(self, widget: QLabel | None, text: str) -> None:
        if widget is None:
            return
        try:
            widget.setText(text)
        except RuntimeError:
            pass

    def _safe_set_widget_stylesheet(self, widget: QLabel | None, stylesheet: str) -> None:
        if widget is None:
            return
        try:
            widget.setStyleSheet(stylesheet)
        except RuntimeError:
            pass

    def _start_camera_preview(self) -> None:
        if self._camera_cap is not None:
            return
        try:
            if not self.isVisible() or (self.parent() is not None and not self.parent().isVisible()):
                return
        except RuntimeError:
            return
        try:
            import cv2
        except ImportError:
            return
        try:
            cap = cv2.VideoCapture(0)
        except Exception:
            return
        if not cap.isOpened():
            cap.release()
            return
        self._camera_cap = cap
        try:
            if self._preview_timer is None:
                self._preview_timer = QTimer()
                self._preview_timer.timeout.connect(self._refresh_camera_preview)
                self._preview_timer.setInterval(180)
            self._preview_timer.start()
        except RuntimeError:
            self._stop_camera_preview()

    def _refresh_camera_preview(self) -> None:
        if self._camera_cap is None:
            return
        try:
            import cv2
        except ImportError:
            self._stop_camera_preview()
            return
        try:
            ok, frame = self._camera_cap.read()
        except Exception:
            self._stop_camera_preview()
            return
        if not ok or frame is None:
            self._stop_camera_preview()
            return
        try:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            h, w, ch = rgb_frame.shape
            bytes_per_line = ch * w
            image = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format.Format_RGB888)
            pixmap = QPixmap.fromImage(image)
            scaled = pixmap.scaled(
                self._preview_placeholder.width() or 220,
                self._preview_placeholder.height() or 120,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._preview_placeholder.setPixmap(scaled)
            self._preview_placeholder.setText("")
            self._preview_placeholder.setStyleSheet("background: transparent;")
        except RuntimeError:
            self._stop_camera_preview()

    def _stop_camera_preview(self) -> None:
        try:
            if self._preview_timer is not None:
                self._preview_timer.stop()
        except Exception:
            pass
        try:
            if self._camera_cap is not None:
                self._camera_cap.release()
        except Exception:
            pass
        self._camera_cap = None

    def _load_profiles(self):
        profiles_data = get_authorized_profiles()
        primary = profiles_data.get("primary") or {}
        authorized = profiles_data.get("authorized") or {}
        lines = []

        if primary.get("name"):
            voice_samples = ", ".join(primary.get("voice_prints", []) or [])
            visual_samples = ", ".join(primary.get("visual_signatures", []) or [])
            lines.append(
                f"• PRIMARY: {primary.get('name')} | Voice: {voice_samples or 'n/a'} | Visual: {visual_samples or 'n/a'}"
            )

        for profile_id, profile in authorized.items():
            voice_samples = ", ".join(profile.get("voice_prints", []) or [])
            visual_samples = ", ".join(profile.get("visual_signatures", []) or [])
            lines.append(
                f"• {profile.get('name')} ({profile_id}) | Voice: {voice_samples or 'n/a'} | Visual: {visual_samples or 'n/a'}"
            )

        if not lines:
            lines.append("No profiles registered. Primary user profile will be initialized.")
        self._profile_list_edit.setPlainText("\n".join(lines))

    def _get_profiles(self) -> list[dict]:
        profiles_data = get_authorized_profiles()
        primary = profiles_data.get("primary") or {}
        authorized = profiles_data.get("authorized") or {}
        result = []
        if primary.get("name"):
            result.append({
                "name": primary.get("name"),
                "id": primary.get("id", "JAMES-001"),
                "voice_signature": "verified" if primary.get("voice_prints") else "enrolled",
                "visual_signature": "verified" if primary.get("visual_signatures") else "enrolled",
            })
        for profile_id, profile in authorized.items():
            result.append({
                "name": profile.get("name"),
                "id": profile_id,
                "voice_signature": "enrolled",
                "visual_signature": "enrolled",
            })
        return result

    def _add_profile(self):
        name = self._new_name_input.text().strip()
        if not name:
            return

        voice_text = self._voice_input.text().strip() or name
        visual_text = self._visual_input.text().strip() or name
        make_primary = not bool(get_authorized_profiles().get("primary", {}).get("name"))
        profile_id = name.lower().replace(" ", "_")

        enroll_biometric_profile(
            profile_id=profile_id,
            name=name,
            voice_print=voice_text,
            visual_signature=visual_text,
            clearance_level="omega",
            make_primary=make_primary,
        )

        self._new_name_input.clear()
        self._voice_input.clear()
        self._visual_input.clear()
        self._load_profiles()

    def _set_capture_state(self, state: str) -> None:
        try:
            if state == "recording":
                self._capture_state_text = "● SPEAK NOW"
                self._safe_set_widget_text(self._speak_indicator, self._capture_state_text)
                self._safe_set_widget_stylesheet(self._speak_indicator, f"color: {C.ACC2}; background: transparent;")
                self._safe_set_widget_text(self._preview_placeholder, "Camera preview active. Baseline capture in progress — keep your face centered and speak clearly.")
                self._safe_set_widget_stylesheet(self._preview_placeholder, f"color: {C.TEXT}; background: transparent;")
                self._start_camera_preview()
            elif state == "ready":
                self._capture_state_text = "● READY"
                self._safe_set_widget_text(self._speak_indicator, self._capture_state_text)
                self._safe_set_widget_stylesheet(self._speak_indicator, f"color: {C.GREEN}; background: transparent;")
                self._safe_set_widget_text(self._preview_placeholder, "Baseline captured. Review the result and confirm if it looks right.")
                self._safe_set_widget_stylesheet(self._preview_placeholder, f"color: {C.GREEN}; background: transparent;")
                self._stop_camera_preview()
            else:
                self._capture_state_text = "● WAITING"
                self._safe_set_widget_text(self._speak_indicator, self._capture_state_text)
                self._safe_set_widget_stylesheet(self._speak_indicator, f"color: {C.TEXT_MED}; background: transparent;")
                self._safe_set_widget_text(self._preview_placeholder, "Camera preview will appear here while the baseline is being captured.")
                self._safe_set_widget_stylesheet(self._preview_placeholder, f"color: {C.TEXT_DIM}; background: transparent;")
                self._stop_camera_preview()
        except RuntimeError:
            pass

    def _show_capture_confirmation(self, message: str) -> None:
        try:
            identity = self._setup_name or get_authorized_profiles().get("primary", {}).get("name") or "James Lumsden"
            granted, details = evaluate_live_biometric_security(identity)
            summary = message
            if granted:
                summary = f"{message}\nBaseline captured and verified against the stored profile."
            else:
                summary = f"{message}\nBaseline capture completed, but verification is still pending."
                if details.get("voice_detected"):
                    summary += " Voice signal detected."
                if details.get("visual_detected"):
                    summary += " Face signal detected."
                if not details.get("voice_detected") and not details.get("visual_detected"):
                    summary += " No usable live voice or face sample could be confirmed."

            self._safe_set_widget_text(self._setup_status, summary)
            self._safe_set_widget_stylesheet(self._setup_status, f"color: {C.GREEN if granted else C.ACC2}; background: transparent;")
            self._safe_set_widget_text(self._setup_countdown_label, "Baseline captured" if granted else "Verification pending")
            self._set_capture_state("ready")
            try:
                self._confirm_btn.setText("CONFIRM BASELINE")
                self._confirm_btn.show()
            except RuntimeError:
                pass
        except RuntimeError:
            pass

    def _confirm_baseline(self) -> None:
        self._confirm_btn.hide()
        self._set_capture_state("idle")
        self._setup_status.setText("Baseline confirmed. Your profile is now ready.")
        self._setup_status.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
        self._setup_countdown_label.setText("")
        self._load_profiles()

    def _establish_baseline(self):
        name = self._new_name_input.text().strip() or get_authorized_profiles().get("primary", {}).get("name") or "James Lumsden"
        self._setup_status.setText("Preparing live baseline capture. Keep still, face the camera, and speak clearly.")
        self._setup_status.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        self._setup_countdown_label.setText("Starting in 3...")
        self._confirm_btn.hide()
        self._set_capture_state("idle")
        self._setup_timer = 3
        self._setup_name = name
        QTimer.singleShot(1000, self._countdown_baseline_step)

    def _countdown_baseline_step(self):
        if getattr(self, "_setup_timer", 0) <= 1:
            self._set_capture_state("recording")
            self._setup_countdown_label.setText("Capturing baseline...")
            self._setup_status.setText("Capturing your live voice and face baseline now.")
            ok, message = establish_biometric_baseline(name=self._setup_name)
            if ok:
                self._show_capture_confirmation(message)
            else:
                self._setup_status.setText(message)
                self._setup_status.setStyleSheet(f"color: {C.RED}; background: transparent;")
                self._setup_countdown_label.setText("Capture failed")
                self._set_capture_state("idle")
            return
        self._setup_timer -= 1
        self._setup_countdown_label.setText(f"{self._setup_timer}...")
        QTimer.singleShot(1000, self._countdown_baseline_step)


class BiometricLockOverlay(QWidget):
    """
    BiometricLock_Protocol integration widget.
    Implements mandatory profile-backed voice recognition and visual person detection verification
    for high-security Stark protocols and authorization overrides.
    """
    verified = pyqtSignal()
    failed = pyqtSignal()

    def __init__(self, parent=None):
        self._qt_ready = False
        app = _ensure_qapplication()
        if app is None:
            return
        self._qt_ready = True
        super().__init__(parent)
        self._failed_scans = 0
        self._max_failed_scans = 3
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            BiometricLockOverlay {{
                background: rgba(0, 4, 10, 248);
                border: 1px solid {C.ACC};
                border-radius: 8px;
            }}
        """)
        self.setFixedSize(440, 360)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 20)
        lay.setSpacing(10)

        title = QLabel("🔒 BIOMETRIC LOCK PROTOCOL")
        title.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        title.setStyleSheet(f"color: {C.ACC}; background: transparent;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(title)

        sub = QLabel("Security Protocol XLIX requires dual-factor biometric clearance & profile verification.")
        sub.setFont(QFont("Courier New", 8))
        sub.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        sub.setAlignment(Qt.AlignmentFlag.AlignCenter)
        sub.setWordWrap(True)
        lay.addWidget(sub)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 4px 0;")
        lay.addWidget(sep)

        self._profile_lbl = QLabel("PRIMARY PROFILE: James Lumsden (PRIMARY)")
        self._profile_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._profile_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._profile_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._profile_lbl)
        self._refresh_profile_label()

        self._status_lbl = QLabel("STATUS: AWAITING VOICE & VISUAL SCAN")
        self._status_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._status_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        self._status_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._status_lbl)

        self._voice_chk = QLabel("🎙️ Voice Recognition: PENDING")
        self._voice_chk.setFont(QFont("Courier New", 8))
        self._voice_chk.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(self._voice_chk)

        self._visual_chk = QLabel("👁️ Visual Person Detection: PENDING")
        self._visual_chk.setFont(QFont("Courier New", 8))
        self._visual_chk.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(self._visual_chk)

        lay.addStretch()

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._scan_btn = QPushButton("INITIATE BIOMETRIC SCAN")
        self._scan_btn.setFixedHeight(36)
        self._scan_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scan_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL2}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 4px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        self._scan_btn.clicked.connect(self._run_scan)
        btn_row.addWidget(self._scan_btn)
        lay.addLayout(btn_row)

        override_btn = QPushButton("OVERRIDE CODE")
        override_btn.setFixedHeight(30)
        override_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        override_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        override_btn.setStyleSheet(f"""
            QPushButton {{
                background: #140008; color: {C.RED};
                border: 1px solid {C.RED}; border-radius: 4px;
            }}
            QPushButton:hover {{ background: #200010; }}
        """)
        override_btn.clicked.connect(self._prompt_override_code)
        lay.addWidget(override_btn)

    def _prompt_override_code(self):
        if not has_override_code_configured():
            self._status_lbl.setText("STATUS: NO OVERRIDE CODE CONFIGURED")
            self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")
            return

        allowed, message = check_override_rate_limit()
        if not allowed:
            self._status_lbl.setText(f"STATUS: {message}")
            self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")
            return

        code, ok = QInputDialog.getText(
            self,
            "Manual Override",
            "Enter manual override code:",
            QLineEdit.EchoMode.Password,
            "",
        )
        if not ok or not code:
            return

        success = verify_override_code(code)
        record_override_attempt(success)
        _append_override_audit_log(success, "manual_override")

        if success:
            self._status_lbl.setText("STATUS: OVERRIDE CODE ACCEPTED")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
            host = self.window()
            if host is not None and hasattr(host, "_log"):
                host._log.append_log("SYS: BiometricLock_Protocol cleared via manual override code.")
            self.verified.emit()
        else:
            allowed, message = check_override_rate_limit()
            self._status_lbl.setText(
                f"STATUS: {message}" if not allowed else "STATUS: OVERRIDE CODE REJECTED"
            )
            self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")
            host = self.window()
            if host is not None and hasattr(host, "_log"):
                host._log.append_log("SYS: Manual override attempt rejected.")

    def _refresh_profile_label(self):
        primary = get_authorized_profiles().get("primary") or {}
        name = primary.get("name") or "James Lumsden"
        self._profile_lbl.setText(f"PRIMARY PROFILE: {name} (PRIMARY)")

    def _run_scan(self):
        self._scan_btn.setEnabled(False)
        self._scan_btn.setText("SCANNING...")
        self._refresh_profile_label()
        self._status_lbl.setText("STATUS: SCANNING LIVE BIOMETRICS...")
        self._status_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        self._voice_chk.setText("🎙️ Voice Recognition: LISTENING...")
        self._voice_chk.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._visual_chk.setText("👁️ Visual Person Detection: WATCHING...")
        self._visual_chk.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        QTimer.singleShot(1200, self._step_live_scan)

    def _step_voice(self):
        primary = get_authorized_profiles().get("primary") or {}
        voice_text = ", ".join(primary.get("voice_prints", []) or []) or primary.get("name") or "James Lumsden"
        verified_voice = verify_biometric_security(voice_text, "")
        self._voice_chk.setText(
            "🎙️ Voice Recognition: PROFILE VERIFIED ✓" if verified_voice else "🎙️ Voice Recognition: PROFILE NOT FOUND"
        )
        self._voice_chk.setStyleSheet(f"color: {C.GREEN if verified_voice else C.RED}; background: transparent;")
        QTimer.singleShot(900, self._step_visual)

    def _step_visual(self):
        primary = get_authorized_profiles().get("primary") or {}
        visual_text = ", ".join(primary.get("visual_signatures", []) or []) or primary.get("name") or "James Lumsden"
        verified_visual = verify_biometric_security("", visual_text)
        self._visual_chk.setText(
            "👁️ Visual Person Detection: PROFILE VERIFIED ✓" if verified_visual else "👁️ Visual Person Detection: PROFILE NOT FOUND"
        )
        self._visual_chk.setStyleSheet(f"color: {C.GREEN if verified_visual else C.RED}; background: transparent;")
        if self._apply_verification_state(self._voice_chk.text().endswith("✓"), verified_visual):
            QTimer.singleShot(700, self.verified.emit)

    def _apply_verification_state(self, verified_voice: bool, verified_visual: bool) -> bool:
        granted = verified_visual and verified_voice
        if granted:
            self._failed_scans = 0
            self._status_lbl.setText("STATUS: PROFILE CLEARANCE GRANTED")
            self._status_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")
            self._scan_btn.setEnabled(False)
            self._scan_btn.setText("ACCESS GRANTED")
            return granted

        self._failed_scans += 1
        self._status_lbl.setText(
            f"STATUS: PROFILE NOT VERIFIED ({self._failed_scans}/{self._max_failed_scans})"
        )
        self._status_lbl.setStyleSheet(f"color: {C.RED}; background: transparent;")
        self._scan_btn.setEnabled(True)
        self._scan_btn.setText("RETRY BIOMETRIC SCAN")
        if self._failed_scans >= self._max_failed_scans:
            self._status_lbl.setText("STATUS: SECURITY LOCKDOWN")
            self.failed.emit()
        return granted

    def _step_live_scan(self):
        primary = get_authorized_profiles().get("primary") or {}
        identity = primary.get("name") or "James Lumsden"
        granted, details = evaluate_live_biometric_security(identity)
        self._voice_chk.setText(
            "🎙️ Voice Recognition: PROFILE VERIFIED ✓" if details.get("voice_detected") else "🎙️ Voice Recognition: PROFILE NOT FOUND"
        )
        self._voice_chk.setStyleSheet(f"color: {C.GREEN if details.get('voice_detected') else C.RED}; background: transparent;")
        self._visual_chk.setText(
            "👁️ Visual Person Detection: PROFILE VERIFIED ✓" if details.get("visual_detected") else "👁️ Visual Person Detection: PROFILE NOT FOUND"
        )
        self._visual_chk.setStyleSheet(f"color: {C.GREEN if details.get('visual_detected') else C.RED}; background: transparent;")
        if os.environ.get("JARVIS_BIOMETRIC_DEBUG") == "1":
            debug_line = (
                "SYS: Biometric debug "
                f"voice={details.get('voice_detected')} "
                f"visual={details.get('visual_detected')} "
                f"face_detected={details.get('face_detected')} "
                f"voice_energy={float(details.get('voice_energy') or 0.0):.4f} "
                f"ref_match={details.get('reference_face_match')} "
                f"ref_reason={details.get('reference_face_reason', 'n/a')}"
            )
            try:
                host = self.window()
                if host is not None and hasattr(host, "_log"):
                    host._log.append_log(debug_line)
            except Exception:
                pass
        if self._apply_verification_state(details.get("voice_detected", False), details.get("visual_detected", False)):
            QTimer.singleShot(700, self.verified.emit)


class HueWheel(QWidget):
    hue_picked    = pyqtSignal(str)
    hue_committed = pyqtSignal(str)

    _RING = 16

    def __init__(self, initial_hex: str = DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setFixedSize(148, 148)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self._hue  = 0.53
        self._drag = False
        self.set_color(initial_hex)

    def color(self) -> str:
        return QColor.fromHsvF(self._hue, 1.0, 1.0).name()

    def set_color(self, hex_str: str):
        c = QColor((hex_str or "").strip())
        if c.isValid() and c.hsvHueF() >= 0:
            self._hue = c.hsvHueF()
            self.update()

    def _ring_rect(self) -> QRectF:
        m = self._RING / 2 + 3
        return QRectF(self.rect()).adjusted(m, m, -m, -m)

    def _hue_from_pos(self, pos: QPointF) -> float:
        c  = QRectF(self.rect()).center()
        dx = pos.x() - c.x()
        dy = c.y() - pos.y()
        ang = math.atan2(dy, dx)
        return (ang / (2 * math.pi)) % 1.0

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect   = self._ring_rect()
        center = rect.center()

        grad = QConicalGradient(center, 0)
        for i in range(0, 361, 20):
            grad.setColorAt(i / 360.0, QColor.fromHsvF((i % 360) / 360.0, 1.0, 1.0))
        p.setPen(QPen(QBrush(grad), self._RING))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(rect)

        preview = QColor.fromHsvF(self._hue, 1.0, 1.0)
        inner   = rect.adjusted(30, 30, -30, -30)
        p.setPen(QPen(qcol(C.BORDER_B), 1))
        p.setBrush(QBrush(preview))
        p.drawEllipse(inner)

        r   = rect.width() / 2
        ang = self._hue * 2 * math.pi
        hx  = center.x() + r * math.cos(ang)
        hy  = center.y() - r * math.sin(ang)
        p.setPen(QPen(QColor("#00060a"), 2))
        p.setBrush(QBrush(QColor("#ffffff")))
        p.drawEllipse(QPointF(hx, hy), 7.5, 7.5)

    def mousePressEvent(self, e):
        self._drag = True
        self._hue  = self._hue_from_pos(e.position())
        self.update()
        self.hue_picked.emit(self.color())

    def mouseMoveEvent(self, e):
        if self._drag:
            self._hue = self._hue_from_pos(e.position())
            self.update()
            self.hue_picked.emit(self.color())

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = False
            self.hue_committed.emit(self.color())


class CustomizeOverlay(QWidget):
    saved = pyqtSignal(str, str, str)
    _OW, _OH = 400, 500

    def __init__(self, assistant_name="JARVIS", user_name="",
                 ui_color=DEFAULT_UI_COLOR, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            CustomizeOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 18, 24, 18)
        lay.setSpacing(8)

        def _lbl(txt, fs=9, bold=False, color=C.PRI, align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt); w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        _fs = (f"QLineEdit {{ background: #000d12; color: {C.TEXT}; "
               f"border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px; }}"
               f"QLineEdit:focus {{ border: 1px solid {C.PRI}; }}")

        lay.addWidget(_lbl("⚙  CUSTOMISE ASSISTANT", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_lbl("ASSISTANT NAME", 8, color=C.TEXT_DIM,
                            align=Qt.AlignmentFlag.AlignLeft))
        self._name_input = QLineEdit(assistant_name)
        self._name_input.setFont(QFont("Courier New", 10))
        self._name_input.setFixedHeight(32)
        self._name_input.setStyleSheet(_fs)
        lay.addWidget(self._name_input)

        lay.addSpacing(4)
        lay.addWidget(_lbl("YOUR NAME  (leave blank for default sir / efendim)", 8,
                            color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        self._user_input = QLineEdit(user_name)
        self._user_input.setPlaceholderText("e.g.  Tony   (leave blank for auto)")
        self._user_input.setFont(QFont("Courier New", 10))
        self._user_input.setFixedHeight(32)
        self._user_input.setStyleSheet(_fs)
        lay.addWidget(self._user_input)

        lay.addSpacing(4)
        clr_hdr = QHBoxLayout()
        clr_hdr.addWidget(_lbl("UI COLOUR  —  drag the handle", 8,
                               color=C.TEXT_DIM, align=Qt.AlignmentFlag.AlignLeft))
        clr_hdr.addStretch()
        df_btn = QPushButton("DEFAULT")
        df_btn.setFixedSize(64, 20)
        df_btn.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        df_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        df_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        df_btn.clicked.connect(lambda: self._set_color(DEFAULT_UI_COLOR))
        clr_hdr.addWidget(df_btn)
        lay.addLayout(clr_hdr)

        self._initial_color = (ui_color or DEFAULT_UI_COLOR).strip().lower()
        self._sel_color     = self._initial_color
        self.on_preview     = None

        self._wheel = HueWheel(self._sel_color)
        wheel_row = QHBoxLayout()
        wheel_row.addStretch(); wheel_row.addWidget(self._wheel); wheel_row.addStretch()
        lay.addLayout(wheel_row)
        self._wheel.hue_picked.connect(self._on_wheel_pick)
        self._wheel.hue_committed.connect(self._on_wheel_commit)

        self._hex_input = QLineEdit(self._sel_color)
        self._hex_input.setPlaceholderText("#00d4ff   (custom hex colour)")
        self._hex_input.setFont(QFont("Courier New", 10))
        self._hex_input.setFixedHeight(28)
        self._hex_input.setStyleSheet(_fs)
        self._hex_input.textEdited.connect(self._on_hex_edited)
        lay.addWidget(self._hex_input)

        lay.addSpacing(6)
        btn_row = QHBoxLayout(); btn_row.setSpacing(8)

        save_btn = QPushButton("▸  APPLY CHANGES")
        save_btn.setFixedHeight(34)
        save_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        save_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        save_btn.clicked.connect(self._save)
        btn_row.addWidget(save_btn)

        cancel_btn = QPushButton("CANCEL")
        cancel_btn.setFixedHeight(34)
        cancel_btn.setFont(QFont("Courier New", 9))
        cancel_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cancel_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        cancel_btn.clicked.connect(self._cancel)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

    def _set_color(self, hx: str, update_wheel: bool = True, preview: bool = True):
        self._sel_color = hx.strip().lower()
        self._hex_input.blockSignals(True)
        self._hex_input.setText(self._sel_color)
        self._hex_input.blockSignals(False)
        if update_wheel:
            self._wheel.set_color(self._sel_color)
        if preview and self.on_preview:
            self.on_preview(self._sel_color)

    def _on_wheel_pick(self, hx: str):
        self._sel_color = hx
        self._hex_input.blockSignals(True)
        self._hex_input.setText(hx)
        self._hex_input.blockSignals(False)

    def _on_wheel_commit(self, hx: str):
        self._set_color(hx, update_wheel=False)

    def _on_hex_edited(self, text: str):
        t = text.strip().lower()
        if t.startswith("#") and len(t) == 7:
            try:
                int(t[1:], 16)
            except ValueError:
                return
            self._set_color(t, update_wheel=True, preview=True)

    def _cancel(self):
        if self.on_preview and self._sel_color != self._initial_color:
            self.on_preview(self._initial_color)
        self.hide()

    def _save(self):
        name = self._name_input.text().strip() or "JARVIS"
        user = self._user_input.text().strip()
        self.saved.emit(name, user, self._sel_color or DEFAULT_UI_COLOR)
        self.hide()


class ClipboardPanel(QWidget):
    action_requested = pyqtSignal(str)
    _W, _H = 326, 112

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            ClipboardPanel {{
                background: rgba(0, 8, 14, 248);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)
        self.setFixedWidth(self._W)
        self._clip_text = ""

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 7)
        lay.setSpacing(4)

        hdr = QHBoxLayout(); hdr.setSpacing(4)
        icon_lbl = QLabel("◈  CLIPBOARD DETECTED")
        icon_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        icon_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent;")
        hdr.addWidget(icon_lbl); hdr.addStretch()
        x_btn = QPushButton("✕")
        x_btn.setFixedSize(16, 16)
        x_btn.setFont(QFont("Courier New", 8))
        x_btn.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        x_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        x_btn.clicked.connect(self.hide)
        hdr.addWidget(x_btn)
        lay.addLayout(hdr)

        self._preview = QLabel()
        self._preview.setFont(QFont("Courier New", 8))
        self._preview.setStyleSheet(f"""
            color: {C.TEXT}; background: {C.PANEL2};
            border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 6px;
        """)
        self._preview.setWordWrap(False)
        self._preview.setFixedHeight(28)
        lay.addWidget(self._preview)

        btn_row = QHBoxLayout(); btn_row.setSpacing(4)
        _bs = (f"QPushButton {{ background: {C.PANEL2}; color: {C.TEXT_MED}; "
               f"border: 1px solid {C.BORDER}; border-radius: 2px; }}"
               f"QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}")
        for label, cmd_fmt in [
            ("TRANSLATE", "Translate this text to English: {text}"),
            ("SUMMARISE", "Summarise this: {text}"),
            ("EXPLAIN",   "Explain this: {text}"),
            ("FIX",       "Fix grammar and spelling: {text}"),
        ]:
            b = QPushButton(label)
            b.setFixedHeight(22)
            b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(_bs)
            b.clicked.connect(lambda _, c=cmd_fmt: self._trigger(c))
            btn_row.addWidget(b)
        lay.addLayout(btn_row)

        self._dismiss_timer = QTimer(self)
        self._dismiss_timer.setSingleShot(True)
        self._dismiss_timer.timeout.connect(self.hide)
        self.hide()

    def _trigger(self, cmd_fmt: str):
        if self._clip_text:
            self.action_requested.emit(cmd_fmt.format(text=self._clip_text[:800]))
        self.hide()

    def show_clipboard(self, text: str):
        self._clip_text = text
        preview = text[:58].replace('\n', ' ')
        if len(text) > 58:
            preview += "…"
        self._preview.setText(f'"{preview}"')
        self.show(); self.raise_()
        self._dismiss_timer.start(8000)


class RemoteKeyOverlay(QWidget):
    closed = pyqtSignal()
    _OW, _OH = 400, 465

    def __init__(self, url: str, key: str, auto_login_url: str = "",
                 manual_url: str = "", security_status: str = "", expiry_secs: int = 600, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            RemoteKeyOverlay {{
                background: rgba(0, 4, 12, 0.95);
                border: 1px solid {C.BORDER_B};
                border-radius: 14px;
            }}
        """)
        self._expiry          = time.time() + expiry_secs
        self._on_new_key      = None
        self._auto_login_url  = auto_login_url
        self._manual_url      = manual_url or url
        self._security_status = security_status or "SECURITY: STATUS UNAVAILABLE"

        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 16, 24, 16)
        lay.setSpacing(5)

        def _lbl(txt, fs=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", fs,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            w.setWordWrap(True)
            return w

        lay.addWidget(_lbl("◈  REMOTE ACCESS", 12, True))
        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep)

        self._qr_label = QLabel()
        self._qr_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._qr_label.setFixedSize(176, 176)
        self._qr_label.setStyleSheet(
            "background: white; border-radius: 10px; padding: 4px;"
        )
        qr_row = QHBoxLayout()
        qr_row.addStretch()
        qr_row.addWidget(self._qr_label)
        qr_row.addStretch()
        lay.addLayout(qr_row)

        self._update_qr(auto_login_url or url)

        lay.addWidget(_lbl("Scan with phone camera to connect instantly", 8, color=C.TEXT_DIM))

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 1px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_lbl("Or enter manually:", 7, color=C.TEXT_DIM,
                           align=Qt.AlignmentFlag.AlignLeft))

        self._url_lbl = QLabel(self._manual_url)
        self._url_lbl.setFont(QFont("Courier New", 8))
        self._url_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        self._url_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._url_lbl.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        lay.addWidget(self._url_lbl)

        self._key_lbl = QLabel(key)
        self._key_lbl.setFont(QFont("Courier New", 28, QFont.Weight.Bold))
        self._key_lbl.setStyleSheet(f"""
            color: {C.ACC};
            background: {C.PANEL2};
            border: 1px solid {C.BORDER_B};
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 10px;
        """)
        self._key_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._key_lbl)

        self._timer_lbl = QLabel()
        self._timer_lbl.setFont(QFont("Courier New", 8))
        self._timer_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._timer_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        lay.addWidget(self._timer_lbl)

        self._sec_lbl = QLabel(self._security_status)
        self._sec_lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._sec_lbl.setStyleSheet(
            f"color: {C.TEXT_DIM}; background: {C.PANEL2}; "
            f"border: 1px solid {C.BORDER}; border-radius: 4px; padding: 5px 6px;"
        )
        self._sec_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sec_lbl.setWordWrap(True)
        lay.addWidget(self._sec_lbl)

        btn_row = QHBoxLayout(); btn_row.setSpacing(8)
        new_btn = QPushButton("NEW KEY")
        new_btn.setFixedHeight(32)
        new_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        new_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        new_btn.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        new_btn.clicked.connect(self._refresh_key)
        btn_row.addWidget(new_btn)

        close_btn = QPushButton("DISMISS")
        close_btn.setFixedHeight(32)
        close_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
        """)
        close_btn.clicked.connect(self._do_close)
        btn_row.addWidget(close_btn)
        lay.addLayout(btn_row)

        self._ctimer = QTimer(self)
        self._ctimer.timeout.connect(self._tick)
        self._ctimer.start(1000)
        self._tick()

    def set_new_key_callback(self, fn) -> None:
        self._on_new_key = fn

    def _update_qr(self, url: str) -> None:
        if not url:
            self._qr_label.setText("—")
            return
        try:
            import qrcode as _qrmod
            from io import BytesIO
            qr = _qrmod.QRCode(
                box_size=5, border=2,
                error_correction=_qrmod.constants.ERROR_CORRECT_M,
            )
            qr.add_data(url)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            buf = BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap()
            px.loadFromData(buf.getvalue())
            self._qr_label.setPixmap(
                px.scaled(170, 170,
                          Qt.AspectRatioMode.KeepAspectRatio,
                          Qt.TransformationMode.SmoothTransformation)
            )
        except ImportError:
            self._qr_label.setText("pip install\nqrcode[pil]")
            self._qr_label.setFont(QFont("Courier New", 8))
            self._qr_label.setStyleSheet(
                "color: #888; background: white; border-radius: 10px; padding: 4px;"
            )
        except Exception:
            self._qr_label.setText(url[:28])
            self._qr_label.setFont(QFont("Courier New", 7))
            self._qr_label.setStyleSheet(
                f"color: {C.PRI}; background: white; border-radius: 10px; padding: 4px;"
            )

    def _tick(self):
        remaining = max(0, int(self._expiry - time.time()))
        m, s = divmod(remaining, 60)
        self._timer_lbl.setText(f"Key expires in  {m:02d}:{s:02d}")
        if remaining == 0:
            self._do_close()

    def mark_connected(self) -> None:
        self._ctimer.stop()
        self._key_lbl.setText("CONNECTED")
        self._key_lbl.setStyleSheet(f"""
            color: {C.GREEN};
            background: rgba(34,197,94,0.08);
            border: 2px solid rgba(34,197,94,0.4);
            border-radius: 8px;
            padding: 6px 4px;
            letter-spacing: 4px;
        """)
        self._qr_label.setText("✓")
        self._qr_label.setFont(QFont("Courier New", 54, QFont.Weight.Bold))
        self._qr_label.setStyleSheet(
            "color: #00ff88; background: #001a0d; border-radius: 10px;"
        )
        self._timer_lbl.setText("Phone connected — JARVIS ready")
        self._timer_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent;")

    def _refresh_key(self):
        if self._on_new_key:
            result = self._on_new_key()
            if result:
                url    = result[0]
                key    = result[1]
                auto   = result[2] if len(result) >= 3 else ""
                manual = result[3] if len(result) >= 4 else url
                sec    = result[4] if len(result) >= 5 else "SECURITY: STATUS UNAVAILABLE"
                self._manual_url     = manual or url
                self._url_lbl.setText(self._manual_url)
                self._key_lbl.setText(key)
                self._auto_login_url = auto
                self._security_status = sec
                self._sec_lbl.setText(sec)
                self._update_qr(auto or url)
                self._expiry = time.time() + 600
                self._key_lbl.setStyleSheet(f"""
                    color: {C.ACC};
                    background: {C.PANEL2};
                    border: 1px solid {C.BORDER_B};
                    border-radius: 8px;
                    padding: 6px 4px;
                    letter-spacing: 10px;
                """)
                self._timer_lbl.setStyleSheet(
                    f"color: {C.TEXT_MED}; background: transparent;"
                )
                self._ctimer.start(1000)
                self._tick()

    def _do_close(self):
        self._ctimer.stop()
        self.hide()
        self.closed.emit()


class MainWindow(QMainWindow):
    _log_sig        = pyqtSignal(str)
    _state_sig      = pyqtSignal(str)
    _content_sig    = pyqtSignal(str, str)
    _reconfig_sig   = pyqtSignal()
    _camera_sig     = pyqtSignal(bytes)
    _cam_stream_sig = pyqtSignal(bool)
    _cam_frame_sig  = pyqtSignal(bytes)
    _clipboard_sig  = pyqtSignal(str)
    _remote_url_sig = pyqtSignal(object)
    _wake_bridge_sig = pyqtSignal(str, str, object)
    _audio_status_sig = pyqtSignal(str, str, object)
    _visual_watch_sig = pyqtSignal(str, str, object)

    def __init__(self, face_path: str):
        super().__init__()
        self._face_path = face_path

        _cfg = _read_full_config()
        self._assistant_name: str = (_cfg.get("assistant_name") or "JARVIS").strip()
        _display = self._assistant_name.upper()

        _ui_color = (_cfg.get("ui_color") or "").strip()
        if _ui_color and _ui_color.lower() != DEFAULT_UI_COLOR:
            apply_ui_accent(_ui_color)

        self.setWindowTitle(f"{_display} — MARK XLIX")
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)

        screen = QApplication.primaryScreen().availableGeometry()
        self.move(
            (screen.width()  - _DEFAULT_W) // 2,
            (screen.height() - _DEFAULT_H) // 2,
        )

        self.on_text_command   = None
        self.on_remote_clicked = None
        self.on_remote_url_clicked = None
        self.on_interrupt      = None
        self.on_biometric_failure = None
        self._muted            = False
        self._current_file: str | None = None
        self._security_overlay: QWidget | None = None
        self._biometric_overlay: BiometricLockOverlay | None = None
        self._biometric_locked: bool = True
        self._manage_profiles_overlay: ManageProfilesOverlay | None = None
        self._remote_overlay: RemoteKeyOverlay | None = None
        self._customize_overlay: CustomizeOverlay | None = None

        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)

        self._left_panel = self._build_left_panel()
        body.addWidget(self._left_panel, stretch=0)

        self.hud = HudCanvas(face_path, _display)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._content_panel = self._build_content_panel()
        self._remote_url_status_lbl = QLabel("Remote URL: unavailable")
        self._remote_url_status_lbl.setFont(QFont("Courier New", 7))
        self._remote_url_status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._remote_url_status_lbl.setToolTip("Current public or manual remote dashboard URL")
        self._wake_bridge_status_lbl = QLabel("Wake Bridge: checking...")
        self._wake_bridge_status_lbl.setFont(QFont("Courier New", 7))
        self._wake_bridge_status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._wake_bridge_status_lbl.setToolTip("iMessage cold-start bridge health")
        self._audio_status_lbl = QLabel("Audio diagnostics: initializing...")
        self._audio_status_lbl.setFont(QFont("Courier New", 7))
        self._audio_status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._audio_status_lbl.setToolTip("Live audio queue depth and drop counters")
        self._visual_watch_status_lbl = QLabel("Visual watch: idle")
        self._visual_watch_status_lbl.setFont(QFont("Courier New", 7))
        self._visual_watch_status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._visual_watch_status_lbl.setToolTip("Live tab/window/app watch activity")
        self._vps_status_lbl = QLabel("VPS: checking...")
        self._vps_status_lbl.setFont(QFont("Courier New", 7))
        self._vps_status_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._vps_status_lbl.setToolTip("Remote VPS health status")

        _cam_cont = QWidget()
        _cam_cont.setStyleSheet("background: #000308;")
        _cam_v = QVBoxLayout(_cam_cont)
        _cam_v.setContentsMargins(0, 0, 0, 0)
        _cam_v.setSpacing(0)
        _cam_hdr = QHBoxLayout()
        _cam_hdr.setContentsMargins(8, 5, 8, 5)
        _cam_title = QLabel("◈  CAMERA FEED")
        _cam_title.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_title.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        _cam_hdr.addWidget(_cam_title)
        _cam_hdr.addStretch()
        _cam_x = QPushButton("✕  CLOSE")
        _cam_x.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        _cam_x.setCursor(Qt.CursorShape.PointingHandCursor)
        _cam_x.setStyleSheet(f"""
            QPushButton {{
                color: {C.TEXT_DIM}; background: transparent;
                border: none; padding: 2px 6px;
            }}
            QPushButton:hover {{ color: {C.PRI}; }}
        """)
        _cam_x.clicked.connect(self.stop_camera_stream)
        _cam_hdr.addWidget(_cam_x)
        _cam_v.addLayout(_cam_hdr)
        self._cam_live_lbl = QLabel()
        self._cam_live_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cam_live_lbl.setStyleSheet("background: transparent;")
        self._cam_live_lbl.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        _cam_v.addWidget(self._cam_live_lbl, stretch=1)

        self._hud_cam_stack = QStackedWidget()
        self._hud_cam_stack.addWidget(self.hud)
        self._hud_cam_stack.addWidget(_cam_cont)

        self._center_split = QSplitter(Qt.Orientation.Vertical)
        self._center_split.setStyleSheet(f"""
            QSplitter::handle {{
                background: {C.BORDER};
                height: 4px;
            }}
            QSplitter::handle:hover {{
                background: {C.PRI_DIM};
            }}
        """)
        self._center_split.addWidget(self._hud_cam_stack)
        self._center_split.addWidget(self._content_panel)
        self._center_split.setStretchFactor(0, 3)
        self._center_split.setStretchFactor(1, 1)
        self._center_split.setCollapsible(0, False)
        body.addWidget(self._center_split, stretch=5)

        self._right_panel = self._build_right_panel()
        body.addWidget(self._right_panel, stretch=0)

        root.addLayout(body, stretch=1)
        root.addWidget(self._build_footer())

        self._quick_drawer = self._build_quick_drawer()
        self._update_autostart_btn(self._check_autostart())
        from memory.config_manager import get_brief_enabled as _gbe
        self._update_brief_btn(_gbe())
        self._update_audio_profile_btn(self._get_audio_latency_profile())
        self._update_public_remote_btn(self._get_public_remote_enabled())

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000)
        self._tick_clock()

        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(3000)
        self._update_metrics()

        self._log_sig.connect(self._log.append_log)
        self._state_sig.connect(self._apply_state)
        self._content_sig.connect(self._show_content)
        self._reconfig_sig.connect(self._show_setup)
        self._camera_sig.connect(self._show_camera_frame)
        self._cam_stream_sig.connect(self._on_cam_stream)
        self._cam_frame_sig.connect(self._on_cam_frame)
        self._clipboard_sig.connect(self._show_clipboard_panel)
        self._remote_url_sig.connect(self.set_remote_url_status)
        self._wake_bridge_sig.connect(self.set_wake_bridge_status)
        self._audio_status_sig.connect(self.set_audio_status)
        self._visual_watch_sig.connect(self.set_visual_watch_status)
        self._cam_stop = threading.Event()
        self._cam_streaming = False

        self._cam_preview = _CameraPreview(self.centralWidget())
        self._clipboard_panel = ClipboardPanel(self.centralWidget())
        self._clipboard_panel.action_requested.connect(self._on_clipboard_action)
        QApplication.clipboard().dataChanged.connect(self._on_clipboard_changed)

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()
        elif os.getenv("JARVIS_VPS_URL", "").strip():
            # Pure VPS-worker mode runs unattended — there's no one at the Mac
            # to clear a face/voice scan, and the scan's own camera use would
            # fight remote camera-capture requests for the same device. The
            # lock still stays active (gating tool execution and remote
            # commands); it can only be cleared via the manual override code.
            self._biometric_locked = True
            self._log.append_log(
                "SYS: VPS worker mode — visual/voice scan skipped, "
                "BiometricLock_Protocol remains active pending override code."
            )
        else:
            QTimer.singleShot(400, self._show_biometric_lock)
        QTimer.singleShot(500, self._start_vps_status_polling)

        sc_mute = QShortcut(QKeySequence("F4"), self)
        sc_mute.activated.connect(self._toggle_mute)
        sc_full = QShortcut(QKeySequence("F11"), self)
        sc_full.activated.connect(self._toggle_fullscreen)
        sc_intr = QShortcut(QKeySequence("Escape"), self)
        sc_intr.activated.connect(self._do_interrupt)

    def _show_camera_frame(self, img_bytes: bytes):
        self._cam_preview.show_frame(img_bytes)
        cw = self.centralWidget()
        pw = _CameraPreview._W
        ph = self._cam_preview.height()
        self._cam_preview.setGeometry(
            cw.width() - _RIGHT_W - pw - 12,
            cw.height() - ph - 28,
            pw, ph,
        )

    def _on_cam_stream(self, start: bool) -> None:
        if start:
            self._cam_streaming = True
            self._hud_cam_stack.setCurrentIndex(1)
            if hasattr(self, "_cam_toggle_btn"):
                self._cam_toggle_btn.setText("⏹  STOP CAMERA FEED")
                self._cam_toggle_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #140008; color: {C.MUTED_C};
                        border: 1px solid {C.MUTED_C}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ background: #200010; border: 1px solid #ff6688; }}
                """)
        else:
            self._cam_streaming = False
            self._hud_cam_stack.setCurrentIndex(0)
            self._cam_live_lbl.clear()
            if hasattr(self, "_cam_toggle_btn"):
                self._cam_toggle_btn.setText("📷  START CAMERA FEED")
                self._cam_toggle_btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #00140a; color: {C.GREEN};
                        border: 1px solid {C.GREEN}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ background: #001f10; }}
                """)

    def _on_cam_frame(self, data: bytes) -> None:
        px = QPixmap()
        px.loadFromData(data)
        if not px.isNull():
            w, h = self._cam_live_lbl.width(), self._cam_live_lbl.height()
            if w > 1 and h > 1:
                self._cam_live_lbl.setPixmap(
                    px.scaled(w, h,
                              Qt.AspectRatioMode.KeepAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
                )

    def start_camera_stream(self) -> None:
        self._cam_stop.clear()
        self._cam_stream_sig.emit(True)
        t = threading.Thread(target=self._cam_loop, daemon=True, name="cam-stream")
        t.start()

    def _cam_loop(self) -> None:
        try:
            import cv2
            cam_idx = 0
            try:
                import json as _j
                cfg = _j.loads((CONFIG_DIR / "api_keys.json").read_text())
                cam_idx = int(cfg.get("camera_index", 0))
            except Exception:
                pass
            try:
                backend = cv2.CAP_DSHOW if _OS == "Windows" else cv2.CAP_ANY
            except AttributeError:
                backend = 0
            cap = cv2.VideoCapture(cam_idx, backend)
            if not cap.isOpened():
                cap = cv2.VideoCapture(0)
            if not cap.isOpened():
                return
            for _ in range(5):
                cap.read()
            while not self._cam_stop.wait(0.033) and cap.isOpened():
                ret, frame = cap.read()
                if ret and frame is not None:
                    _, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 65])
                    self._cam_frame_sig.emit(buf.tobytes())
            cap.release()
        except Exception as e:
            print(f"[Camera] Stream error: {e}")
        finally:
            self._cam_stream_sig.emit(False)

    def stop_camera_stream(self) -> None:
        self._cam_stop.set()

    @staticmethod
    def _build_jarvis_icon(out_path: Path) -> bool:
        try:
            import math
            import PIL.Image
            import PIL.ImageDraw
            import PIL.ImageFilter
        except ImportError:
            return False

        CYAN   = (0, 212, 255)
        DIM    = (0, 100, 140)
        DARK   = (0, 6, 10)
        GLOW   = (0, 160, 200)
        WHITE  = (220, 240, 255)

        def _render(sz: int) -> PIL.Image.Image:
            S  = sz * 4
            img = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            d   = PIL.ImageDraw.Draw(img)
            cx = cy = S // 2

            R = S // 2 - 2
            d.ellipse([cx-R, cy-R, cx+R, cy+R], fill=(*DARK, 255))
            lw = max(2, S // 40)
            d.ellipse([cx-R, cy-R, cx+R, cy+R],
                      outline=(*CYAN, 220), width=lw)
            R2 = int(R * 0.72)
            d.ellipse([cx-R2, cy-R2, cx+R2, cy+R2],
                      outline=(*DIM, 180), width=max(1, lw // 2))

            R_inner = int(R * 0.30)
            R_outer = int(R * 0.62)
            spoke_w = max(1, S // 80)
            for i in range(6):
                angle = math.radians(i * 60 - 30)
                x1 = cx + int(R_inner * math.cos(angle))
                y1 = cy + int(R_inner * math.sin(angle))
                x2 = cx + int(R_outer * math.cos(angle))
                y2 = cy + int(R_outer * math.sin(angle))
                d.line([x1, y1, x2, y2], fill=(*GLOW, 200), width=spoke_w)

            for i in range(6):
                angle = math.radians(i * 60)
                for dr in range(lw * 2):
                    rx = (R - lw - dr)
                    d.point(
                        [cx + int(rx * math.cos(angle)),
                         cy + int(rx * math.sin(angle))],
                        fill=(*WHITE, 220),
                    )

            Ri = int(R * 0.26)
            d.ellipse([cx-Ri, cy-Ri, cx+Ri, cy+Ri],
                      outline=(*CYAN, 255), width=max(2, lw))

            glow_layer = PIL.Image.new("RGBA", (S, S), (0, 0, 0, 0))
            gd = PIL.ImageDraw.Draw(glow_layer)
            Rc = int(R * 0.13)
            gd.ellipse([cx-Rc*2, cy-Rc*2, cx+Rc*2, cy+Rc*2],
                       fill=(*CYAN, 110))
            glow_layer = glow_layer.filter(PIL.ImageFilter.GaussianBlur(S // 14))
            img = PIL.Image.alpha_composite(img, glow_layer)
            d   = PIL.ImageDraw.Draw(img)

            d.ellipse([cx-Rc, cy-Rc, cx+Rc, cy+Rc], fill=(*WHITE, 255))
            return img.resize((sz, sz), PIL.Image.LANCZOS)

        try:
            sizes  = [256, 128, 64, 48, 32, 16]
            frames = [_render(s) for s in sizes]
            frames[0].save(
                out_path,
                format="ICO",
                append_images=frames[1:],
                sizes=[(s, s) for s in sizes],
            )
            return True
        except Exception as e:
            print(f"[Shortcut] ⚠️  Icon generation failed: {e}")
            return False

    @staticmethod
    def _create_lnk_windows(lnk: str, target: str, args: str,
                             work_dir: str, icon_loc: str) -> None:
        try:
            from win32com.client import Dispatch  # type: ignore[import-not-found]
            sh = Dispatch("WScript.Shell")
            sc = sh.CreateShortCut(lnk)
            sc.TargetPath       = target
            sc.Arguments        = f'"{args}"'
            sc.WorkingDirectory = work_dir
            sc.Description      = "J.A.R.V.I.S AI Assistant"
            sc.IconLocation     = icon_loc
            sc.save()
            return
        except ImportError:
            pass

        vbs = "\n".join([
            'Set ws = CreateObject("WScript.Shell")',
            f'Set sc = ws.CreateShortcut("{lnk}")',
            f'sc.TargetPath = "{target}"',
            f'sc.Arguments = Chr(34) & "{args}" & Chr(34)',
            f'sc.WorkingDirectory = "{work_dir}"',
            'sc.Description = "J.A.R.V.I.S AI Assistant"',
            f'sc.IconLocation = "{icon_loc}"',
            'sc.Save',
        ])
        import tempfile
        fd, tmp = tempfile.mkstemp(suffix=".vbs")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(vbs)
            proc = subprocess.Popen(
                ["wscript.exe", "/nologo", tmp],
                creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            )
            proc.wait(timeout=10)
        finally:
            try:
                os.unlink(tmp)
            except Exception:
                pass

    @staticmethod
    def _get_desktop_dir() -> Path:
        home = Path.home()
        _os = platform.system()

        if _os == "Windows":
            try:
                import ctypes
                from ctypes import wintypes

                class _GUID(ctypes.Structure):
                    _fields_ = [("Data1", wintypes.DWORD),
                                ("Data2", wintypes.WORD),
                                ("Data3", wintypes.WORD),
                                ("Data4", ctypes.c_ubyte * 8)]

                fid = _GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                            (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9,
                                                 0x9A, 0x87, 0xC6, 0x41))
                buf = ctypes.c_wchar_p()
                if ctypes.windll.shell32.SHGetKnownFolderPath(
                        ctypes.byref(fid), 0, None, ctypes.byref(buf)) == 0:
                    p = Path(buf.value)
                    ctypes.windll.ole32.CoTaskMemFree(buf)
                    if p.is_dir():
                        return p
            except Exception:
                pass

            try:
                import winreg
                with winreg.OpenKey(
                        winreg.HKEY_CURRENT_USER,
                        r"Software\Microsoft\Windows\CurrentVersion"
                        r"\Explorer\User Shell Folders") as key:
                    val, _t = winreg.QueryValueEx(key, "Desktop")
                p = Path(os.path.expandvars(val))
                if p.is_dir():
                    return p
            except Exception:
                pass

        elif _os == "Linux":
            try:
                out = subprocess.run(["xdg-user-dir", "DESKTOP"],
                                     capture_output=True, text=True, timeout=5)
                p = Path(out.stdout.strip())
                if out.stdout.strip() and p != home and p.is_dir():
                    return p
            except Exception:
                pass
            try:
                cfg = home / ".config" / "user-dirs.dirs"
                for line in cfg.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("XDG_DESKTOP_DIR"):
                        val = line.split("=", 1)[1].strip().strip('"')
                        p = Path(val.replace("$HOME", str(home)))
                        if p != home and p.is_dir():
                            return p
            except Exception:
                pass

        return home / "Desktop"

    def _create_desktop_shortcut(self):
        import stat as _stat
        script  = Path(__file__).resolve().parent / "main.py"
        python  = Path(sys.executable)
        desktop = self._get_desktop_dir()

        ico_path = Path(__file__).resolve().parent / "config" / "jarvis.ico"
        if not ico_path.exists():
            self._build_jarvis_icon(ico_path)

        try:
            _os = platform.system()
            if _os == "Windows":
                pythonw  = python.parent / "pythonw.exe"
                target   = str(pythonw if pythonw.exists() else python)
                lnk      = str(desktop / "J.A.R.V.I.S.lnk")
                icon_loc = str(ico_path) if ico_path.exists() else f"{target},0"
                self._create_lnk_windows(lnk, target, str(script),
                                         str(script.parent), icon_loc)

            elif _os == "Darwin":
                app     = desktop / "J.A.R.V.I.S.app"
                mac_dir = app / "Contents" / "MacOS"
                res_dir = app / "Contents" / "Resources"
                mac_dir.mkdir(parents=True, exist_ok=True)
                res_dir.mkdir(exist_ok=True)

                launcher = mac_dir / "JARVIS"
                launcher.write_text(
                    "#!/usr/bin/env bash\n"
                    f'cd "{script.parent}"\n'
                    f'exec "{python}" "{script}"\n'
                )
                launcher.chmod(launcher.stat().st_mode
                               | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH)

                (app / "Contents" / "Info.plist").write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                    '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                    '<plist version="1.0"><dict>\n'
                    '  <key>CFBundleExecutable</key><string>JARVIS</string>\n'
                    '  <key>CFBundleIdentifier</key>'
                    '<string>com.jarvis.assistant</string>\n'
                    '  <key>CFBundleName</key><string>J.A.R.V.I.S</string>\n'
                    '  <key>CFBundlePackageType</key><string>APPL</string>\n'
                    '  <key>CFBundleVersion</key><string>1.0</string>\n'
                    '</dict></plist>\n'
                )

                try:
                    import PIL.Image
                    icns = res_dir / "AppIcon.icns"
                    PIL.Image.open(ico_path).save(icns, format="ICNS")
                    plist = app / "Contents" / "Info.plist"
                    txt = plist.read_text()
                    plist.write_text(
                        txt.replace(
                            '</dict></plist>',
                            '  <key>CFBundleIconFile</key>'
                            '<string>AppIcon</string>\n</dict></plist>\n',
                        )
                    )
                except Exception:
                    pass

            else:
                png_path = ico_path.with_suffix(".png")
                if not png_path.exists() and ico_path.exists():
                    try:
                        import PIL.Image
                        PIL.Image.open(ico_path).resize(
                            (256, 256), PIL.Image.LANCZOS
                        ).save(png_path, format="PNG")
                    except Exception:
                        png_path = ico_path

                icon_line = f"Icon={png_path}\n" if png_path.exists() else ""
                desk = desktop / "J.A.R.V.I.S.desktop"
                desk.write_text(
                    "[Desktop Entry]\n"
                    "Name=J.A.R.V.I.S\n"
                    f"Exec={python} {script}\n"
                    f"Path={script.parent}\n"
                    "Type=Application\n"
                    "Terminal=false\n"
                    "Categories=Utility;\n"
                    + icon_line
                )
                desk.chmod(desk.stat().st_mode | 0o755)

            self._log.append_log("SYS: Desktop shortcut created.")
        except Exception as e:
            self._log.append_log(f"ERR: Shortcut failed — {e}")

    def _toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
        else:
            self.showFullScreen()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cw = self.centralWidget()
        if cw is None:
            return

        overlay = getattr(self, "_overlay", None)
        if overlay is not None and overlay.isVisible():
            ow, oh = 460, 390
            overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

        biometric_overlay = getattr(self, "_biometric_overlay", None)
        if biometric_overlay is not None and biometric_overlay.isVisible():
            ow, oh = 440, 360
            biometric_overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

        manage_overlay = getattr(self, "_manage_profiles_overlay", None)
        if manage_overlay is not None and manage_overlay.isVisible():
            ow, oh = 480, 400
            manage_overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

        security_overlay = getattr(self, "_security_overlay", None)
        if security_overlay is not None and security_overlay.isVisible():
            ow, oh = 440, 360
            security_overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

        remote_overlay = getattr(self, "_remote_overlay", None)
        if remote_overlay is not None and remote_overlay.isVisible():
            ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
            remote_overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

        customize_overlay = getattr(self, "_customize_overlay", None)
        if customize_overlay is not None and customize_overlay.isVisible():
            ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
            customize_overlay.setGeometry(
                (cw.width() - ow) // 2,
                (cw.height() - oh) // 2,
                ow, oh,
            )

        cam_preview = getattr(self, "_cam_preview", None)
        if cam_preview is not None:
            pw = _CameraPreview._W
            ph = cam_preview.height() or _CameraPreview._H
            cam_preview.setGeometry(
                cw.width() - _RIGHT_W - pw - 12,
                cw.height() - ph - 28,
                pw, ph,
            )

        clipboard_panel = getattr(self, "_clipboard_panel", None)
        if clipboard_panel is not None and clipboard_panel.isVisible():
            self._position_clipboard_panel()

        quick_drawer = getattr(self, "_quick_drawer", None)
        if quick_drawer is not None and quick_drawer.isVisible():
            self._position_quick_drawer()

    def _update_metrics(self):
        snap = _metrics.snapshot()

        cpu = snap["cpu"]
        self._bar_cpu.set_value(cpu, f"{cpu:.0f}%")

        mem = snap["mem"]
        self._bar_mem.set_value(mem, f"{mem:.0f}%")

        net = snap["net"]
        if net < 1.0:
            net_str = f"{net*1024:.0f}KB/s"
        else:
            net_str = f"{net:.1f}MB/s"
        net_pct = min(100, net * 10)
        self._bar_net.set_value(net_pct, net_str)

        gpu = snap["gpu"]
        if gpu >= 0:
            self._bar_gpu.set_value(gpu, f"{gpu:.0f}%")
        else:
            self._bar_gpu.set_value(0, "N/A")

        tmp = snap["tmp"]
        if tmp >= 0:
            tmp_pct = min(100, (tmp / 100) * 100)
            self._bar_tmp.set_value(tmp_pct, f"{tmp:.0f}°C")
        else:
            self._bar_tmp.set_value(0, "N/A")

        try:
            boot_t  = psutil.boot_time()
            elapsed = time.time() - boot_t
            h = int(elapsed // 3600)
            m = int((elapsed % 3600) // 60)
            self._uptime_lbl.setText(f"UP  {h:02d}:{m:02d}")
        except Exception:
            self._uptime_lbl.setText("UP  --:--")

        try:
            proc_count = len(psutil.pids())
            self._proc_lbl.setText(f"PROC  {proc_count}")
        except Exception:
            self._proc_lbl.setText("PROC  --")


    def _build_header(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(54)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w)
        lay.setContentsMargins(16, 0, 16, 0)

        def _badge(txt, color=C.TEXT_MED):
            l = QLabel(txt)
            l.setFont(QFont("Courier New", 8))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_badge("MARK XLIX", C.PRI_DIM))
        lay.addSpacing(8)
        self._drawer_btn = QPushButton("⚙")
        self._drawer_btn.setFixedSize(26, 26)
        self._drawer_btn.setFont(QFont("Courier New", 11))
        self._drawer_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._drawer_btn.setToolTip("Settings & Controls")
        self._drawer_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 4px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI_DIM}; }}
            QPushButton:checked {{ color: {C.PRI}; border-color: {C.PRI}; background: {C.PRI_GHO}; }}
        """)
        self._drawer_btn.setCheckable(True)
        self._drawer_btn.clicked.connect(self._toggle_drawer)
        lay.addWidget(self._drawer_btn)
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(1)
        _disp = self._assistant_name.upper()
        self._title_lbl = QLabel(_disp)
        self._title_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._title_lbl.setFont(QFont("Courier New", 17, QFont.Weight.Bold))
        self._title_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mid.addWidget(self._title_lbl)
        _sub_text = ("Just A Rather Very Intelligent System"
                     if _disp in ("JARVIS", "J.A.R.V.I.S")
                     else "Personal AI Assistant")
        self._sub_lbl = QLabel(_sub_text)
        self._sub_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._sub_lbl.setFont(QFont("Courier New", 7))
        self._sub_lbl.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent;")
        mid.addWidget(self._sub_lbl)
        lay.addLayout(mid)
        lay.addStretch()

        right_col = QVBoxLayout(); right_col.setSpacing(2)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 14, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._clock_lbl)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 7))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignRight)
        right_col.addWidget(self._date_lbl)
        lay.addLayout(right_col)
        return w

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%a %d %b %Y"))

    def _build_left_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_LEFT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 10, 8, 10)
        lay.setSpacing(6)

        hdr = QLabel("◈ SYS MONITOR")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)
        lay.addSpacing(2)

        self._bar_cpu = MetricBar("CPU", C.PRI)
        self._bar_mem = MetricBar("MEM", C.ACC2)
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_gpu = MetricBar("GPU", C.ACC)
        self._bar_tmp = MetricBar("TMP", "#ff6688")

        for bar in [self._bar_cpu, self._bar_mem, self._bar_net,
                    self._bar_gpu, self._bar_tmp]:
            lay.addWidget(bar)

        lay.addSpacing(4)

        info_panel = QWidget()
        info_panel.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;"
        )
        ip_lay = QVBoxLayout(info_panel)
        ip_lay.setContentsMargins(6, 5, 6, 5)
        ip_lay.setSpacing(3)

        self._uptime_lbl = QLabel("UP  --:--")
        self._uptime_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._uptime_lbl.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        ip_lay.addWidget(self._uptime_lbl)

        self._proc_lbl = QLabel("PROC  --")
        self._proc_lbl.setFont(QFont("Courier New", 8))
        self._proc_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        ip_lay.addWidget(self._proc_lbl)

        os_name = {"Windows": "WIN", "Darwin": "macOS", "Linux": "LINUX"}.get(_OS, _OS.upper())
        os_lbl = QLabel(f"OS  {os_name}")
        os_lbl.setFont(QFont("Courier New", 8))
        os_lbl.setStyleSheet(f"color: {C.ACC2}; background: transparent; border: none;")
        ip_lay.addWidget(os_lbl)

        lay.addWidget(info_panel)
        lay.addSpacing(4)

        lay.addStretch()

        for txt, col in [
            ("AI CORE\nACTIVE",  C.GREEN),
            ("SEC\nCLEARED",     C.PRI),
            ("PROTOCOL\nXLIX",   C.TEXT_DIM),
        ]:
            lbl = QLabel(txt)
            lbl.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setStyleSheet(
                f"color: {col}; background: {C.PANEL2};"
                f"border: 1px solid {C.BORDER_A}; border-radius: 3px; padding: 4px;"
            )
            lay.addWidget(lbl)

        return w

    def _build_right_panel(self) -> QWidget:
        w = QWidget()
        w.setFixedWidth(_RIGHT_W)
        w.setStyleSheet(f"background: {C.DARK}; border-left: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(8, 8, 8, 8)
        lay.setSpacing(6)

        def _sec(txt):
            l = QLabel(f"▸ {txt}")
            l.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            l.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
            return l

        lay.addWidget(_sec("ACTIVITY LOG"))
        self._log = LogWidget()
        lay.addWidget(self._log, stretch=1)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep)

        lay.addWidget(_sec("FILE UPLOAD"))
        self._drop_zone = FileDropZone()
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)

        self._file_hint = QLabel("No file loaded — drop or click above to upload")
        self._file_hint.setFont(QFont("Courier New", 7))
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        self._file_hint.setWordWrap(True)
        lay.addWidget(self._file_hint)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep2)

        lay.addWidget(_sec("CAMERA"))

        self._cam_toggle_btn = QPushButton("📷  START CAMERA FEED")
        self._cam_toggle_btn.setFixedHeight(30)
        self._cam_toggle_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._cam_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cam_toggle_btn.clicked.connect(self._toggle_camera_feed)
        self._cam_toggle_btn.setStyleSheet(f"""
            QPushButton {{
                background: #00140a; color: {C.GREEN};
                border: 1px solid {C.GREEN}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: #001f10; }}
        """)
        lay.addWidget(self._cam_toggle_btn)

        self._cam_analyze_btn = QPushButton("🔍  ANALYZE CAMERA NOW")
        self._cam_analyze_btn.setFixedHeight(30)
        self._cam_analyze_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._cam_analyze_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._cam_analyze_btn.clicked.connect(self._request_camera_analysis)
        self._cam_analyze_btn.setStyleSheet(f"""
            QPushButton {{
                background: #00091a; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            QPushButton:disabled {{
                background: transparent; color: {C.TEXT_DIM}; border: 1px solid {C.BORDER};
            }}
        """)
        lay.addWidget(self._cam_analyze_btn)

        sep3 = QFrame(); sep3.setFrameShape(QFrame.Shape.HLine)
        sep3.setStyleSheet(f"color: {C.BORDER}; margin: 2px 0;")
        lay.addWidget(sep3)

        lay.addWidget(_sec("COMMAND INPUT"))
        lay.addLayout(self._build_input_row())

        self._interrupt_btn = QPushButton("✋  INTERRUPT  [ESC]")
        self._interrupt_btn.setFixedHeight(34)
        self._interrupt_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._interrupt_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._interrupt_btn.setStyleSheet(f"""
            QPushButton {{
                background: #140008; color: {C.MUTED_C};
                border: 1px solid {C.MUTED_C}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: #200010; border: 1px solid #ff6688;
            }}
            QPushButton:pressed {{
                background: #300018;
            }}
        """)
        self._interrupt_btn.clicked.connect(self._do_interrupt)
        lay.addWidget(self._interrupt_btn)

        self._mute_btn = QPushButton("🎙  MICROPHONE ACTIVE")
        self._mute_btn.setFixedHeight(30)
        self._mute_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.clicked.connect(self._toggle_mute)
        self._style_mute_btn()
        lay.addWidget(self._mute_btn)

        self._vps_reboot_btn = QPushButton("♻  REBOOT VPS")
        self._vps_reboot_btn.setFixedHeight(30)
        self._vps_reboot_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._vps_reboot_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._vps_reboot_btn.setStyleSheet(f"""
            QPushButton {{
                background: #120d00; color: {C.ACC2};
                border: 1px solid {C.ACC2}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: #1a1000; }}
        """)
        self._vps_reboot_btn.clicked.connect(self._handle_vps_reboot)
        lay.addWidget(self._vps_reboot_btn)

        return w

    def _build_quick_drawer(self) -> QWidget:
        _BTN_STYLE_PRI = f"""
            QPushButton {{
                background: #00091a; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
        """
        _BTN_STYLE_DIM = f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: 1px solid {C.BORDER}; border-radius: 3px;
                text-align: left; padding: 0 8px;
            }}
            QPushButton:hover {{ color: {C.PRI}; border-color: {C.BORDER_B}; }}
        """

        w = QWidget(self.centralWidget())
        w.setObjectName("QuickDrawer")
        w.setStyleSheet(f"""
            QWidget#QuickDrawer {{
                background: {C.DARK};
                border: 1px solid {C.BORDER_B};
                border-top: none;
                border-radius: 0 0 6px 6px;
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(5)

        hdr = QLabel("◈ CONTROLS")
        hdr.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        hdr.setStyleSheet(f"color: {C.PRI_DIM}; background: transparent; "
                          f"border-bottom: 1px solid {C.BORDER}; padding-bottom: 4px;")
        lay.addWidget(hdr)

        remote_btn = QPushButton("◉  REMOTE CONTROL")
        remote_btn.setFixedHeight(30)
        remote_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        remote_btn.setStyleSheet(_BTN_STYLE_PRI)
        remote_btn.clicked.connect(self._open_remote)
        lay.addWidget(remote_btn)

        live_url_btn = QPushButton("☁  SHOW LIVE REMOTE URL")
        live_url_btn.setFixedHeight(26)
        live_url_btn.setFont(QFont("Courier New", 7))
        live_url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        live_url_btn.setStyleSheet(_BTN_STYLE_DIM)
        live_url_btn.clicked.connect(self._show_live_remote_url)
        lay.addWidget(live_url_btn)

        fs_btn = QPushButton("⛶  FULLSCREEN  [F11]")
        fs_btn.setFixedHeight(26)
        fs_btn.setFont(QFont("Courier New", 7))
        fs_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        fs_btn.setStyleSheet(_BTN_STYLE_DIM)
        fs_btn.clicked.connect(self._toggle_fullscreen)
        lay.addWidget(fs_btn)

        sc_btn = QPushButton("⊞  CREATE DESKTOP SHORTCUT")
        sc_btn.setFixedHeight(26)
        sc_btn.setFont(QFont("Courier New", 7))
        sc_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        sc_btn.setStyleSheet(_BTN_STYLE_DIM)
        sc_btn.clicked.connect(self._create_desktop_shortcut)
        lay.addWidget(sc_btn)

        self._autostart_btn = QPushButton("◉  AUTO-START: OFF")
        self._autostart_btn.setFixedHeight(26)
        self._autostart_btn.setFont(QFont("Courier New", 7))
        self._autostart_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._autostart_btn.clicked.connect(self._toggle_autostart)
        lay.addWidget(self._autostart_btn)

        cust_btn = QPushButton("⚙  CUSTOMISE ASSISTANT")
        cust_btn.setFixedHeight(26)
        cust_btn.setFont(QFont("Courier New", 7))
        cust_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        cust_btn.setStyleSheet(_BTN_STYLE_DIM)
        cust_btn.clicked.connect(self._open_customize)
        lay.addWidget(cust_btn)

        prof_btn = QPushButton("👥  MANAGE PROFILES")
        prof_btn.setFixedHeight(26)
        prof_btn.setFont(QFont("Courier New", 7))
        prof_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        prof_btn.setStyleSheet(_BTN_STYLE_DIM)
        prof_btn.clicked.connect(self._open_manage_profiles)
        lay.addWidget(prof_btn)

        self._brief_btn = QPushButton()
        self._brief_btn.setFixedHeight(26)
        self._brief_btn.setFont(QFont("Courier New", 7))
        self._brief_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._brief_btn.clicked.connect(self._toggle_brief)
        lay.addWidget(self._brief_btn)

        self._audio_profile_btn = QPushButton()
        self._audio_profile_btn.setFixedHeight(26)
        self._audio_profile_btn.setFont(QFont("Courier New", 7))
        self._audio_profile_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._audio_profile_btn.clicked.connect(self._toggle_audio_profile)
        lay.addWidget(self._audio_profile_btn)

        self._public_remote_btn = QPushButton()
        self._public_remote_btn.setFixedHeight(26)
        self._public_remote_btn.setFont(QFont("Courier New", 7))
        self._public_remote_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._public_remote_btn.clicked.connect(self._toggle_public_remote)
        lay.addWidget(self._public_remote_btn)

        self._public_remote_url_btn = QPushButton("☁  SET PUBLIC URL")
        self._public_remote_url_btn.setFixedHeight(26)
        self._public_remote_url_btn.setFont(QFont("Courier New", 7))
        self._public_remote_url_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._public_remote_url_btn.clicked.connect(self._set_public_remote_url)
        self._public_remote_url_btn.setStyleSheet(_BTN_STYLE_DIM)
        lay.addWidget(self._public_remote_url_btn)

        self._remote_pin_btn = QPushButton()
        self._remote_pin_btn.setFixedHeight(26)
        self._remote_pin_btn.setFont(QFont("Courier New", 7))
        self._remote_pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._remote_pin_btn.clicked.connect(self._configure_remote_pin)
        lay.addWidget(self._remote_pin_btn)

        w.adjustSize()
        return w

    def _toggle_drawer(self, checked: bool):
        if checked:
            self._position_quick_drawer()
            self._quick_drawer.show()
            self._quick_drawer.raise_()
        else:
            self._quick_drawer.hide()

    def _position_quick_drawer(self):
        if not hasattr(self, '_quick_drawer'):
            return
        _W = 220
        self._quick_drawer.setFixedWidth(_W)
        self._quick_drawer.adjustSize()
        self._quick_drawer.setGeometry(12, 54, _W, self._quick_drawer.sizeHint().height())

    def _build_input_row(self) -> QHBoxLayout:
        row = QHBoxLayout(); row.setSpacing(5)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or question…")
        self._input.setFont(QFont("Courier New", 9))
        self._input.setFixedHeight(30)
        self._input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d14; color: {C.WHITE};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 3px 7px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input)

        send = QPushButton("▸")
        send.setFixedSize(30, 30)
        send.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{
                background: {C.PANEL}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{ background: {C.PRI_GHO}; border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(self._send)
        row.addWidget(send)
        return row

    def _build_content_panel(self) -> QWidget:
        w = QWidget()
        w.setObjectName("ContentPanel")
        w.setStyleSheet(f"""
            QWidget#ContentPanel {{
                background: {C.PANEL};
                border-top: 1px solid {C.BORDER_B};
            }}
        """)
        w.hide()

        lay = QVBoxLayout(w)
        lay.setContentsMargins(12, 7, 12, 8)
        lay.setSpacing(5)

        hdr = QHBoxLayout(); hdr.setSpacing(6)

        dot = QLabel("◈")
        dot.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        dot.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        hdr.addWidget(dot)

        self._content_title_lbl = QLabel("BRIEFING")
        self._content_title_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._content_title_lbl.setStyleSheet(
            f"color: {C.PRI}; background: transparent; letter-spacing: 1px;"
        )
        hdr.addWidget(self._content_title_lbl)
        hdr.addStretch()

        self._content_ts_lbl = QLabel("")
        self._content_ts_lbl.setFont(QFont("Courier New", 7))
        self._content_ts_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        hdr.addWidget(self._content_ts_lbl)

        dismiss = QPushButton("DISMISS  ✕")
        dismiss.setFont(QFont("Courier New", 7))
        dismiss.setFixedHeight(18)
        dismiss.setCursor(Qt.CursorShape.PointingHandCursor)
        dismiss.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_DIM};
                border: 1px solid {C.BORDER}; border-radius: 2px; padding: 0 5px;
            }}
            QPushButton:hover {{ color: {C.TEXT}; border-color: {C.BORDER_B}; }}
        """)
        dismiss.clicked.connect(w.hide)
        hdr.addWidget(dismiss)
        lay.addLayout(hdr)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); lay.addWidget(sep)

        self._content_display = QTextEdit()
        self._content_display.setReadOnly(True)
        self._content_display.setFont(QFont("Courier New", 8))
        self._content_display.setMinimumHeight(60)
        self._content_display.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self._content_display.setStyleSheet(f"""
            QTextEdit {{
                background: {C.DARK};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 3px;
                padding: 6px 8px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG}; width: 6px; border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B}; border-radius: 3px; min-height: 16px;
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0; border: none;
            }}
        """)
        lay.addWidget(self._content_display)

        return w

    def _show_content(self, title: str, text: str):
        import time as _time
        self._content_title_lbl.setText(title.upper()[:48])
        self._content_ts_lbl.setText(_time.strftime("%H:%M:%S"))
        self._content_display.setPlainText(text)
        self._content_display.moveCursor(
            self._content_display.textCursor().MoveOperation.Start
        )
        first_show = not self._content_panel.isVisible()
        self._content_panel.show()
        if first_show:
            total = self._center_split.height()
            self._center_split.setSizes([max(total - 220, 120), 220])

    def set_remote_url_status(self, url: str | None):
        if url:
            text = f"Remote URL: {url}"
            if len(text) > 120:
                text = text[:117] + "..."
            self._remote_url_status_lbl.setText(text)
            self._remote_url_status_lbl.setToolTip(url)
        else:
            self._remote_url_status_lbl.setText("Remote URL: unavailable")
            self._remote_url_status_lbl.setToolTip("Current public or manual remote dashboard URL")

    def set_vps_status(self, text: str, level: str = "neutral", tooltip: str | None = None):
        colors = {"ok": C.GREEN, "warn": C.ACC2, "bad": C.RED, "off": C.TEXT_DIM, "neutral": C.TEXT_DIM}
        color = colors.get(level, C.TEXT_DIM)
        shown = (text or "VPS: unknown").strip()
        if len(shown) > 80:
            shown = shown[:77] + "..."
        self._vps_status_lbl.setText(shown)
        self._vps_status_lbl.setStyleSheet(f"color: {color}; background: transparent;")
        if tooltip:
            self._vps_status_lbl.setToolTip(tooltip)
        else:
            self._vps_status_lbl.setToolTip(shown)

    def set_wake_bridge_status(self, text: str, level: str = "neutral", tooltip: str | None = None):
        colors = {
            "ok": C.GREEN,
            "warn": C.ACC2,
            "bad": C.RED,
            "off": C.TEXT_DIM,
            "neutral": C.TEXT_DIM,
        }
        color = colors.get(level, C.TEXT_DIM)
        shown = (text or "Wake Bridge: unknown").strip()
        if len(shown) > 64:
            shown = shown[:61] + "..."
        self._wake_bridge_status_lbl.setText(shown)
        self._wake_bridge_status_lbl.setStyleSheet(f"color: {color}; background: transparent;")
        if tooltip:
            self._wake_bridge_status_lbl.setToolTip(tooltip)
        else:
            self._wake_bridge_status_lbl.setToolTip(shown)

    def set_audio_status(self, text: str, level: str = "neutral", tooltip: str | None = None):
        colors = {
            "ok": C.GREEN,
            "warn": C.ACC2,
            "bad": C.RED,
            "off": C.TEXT_DIM,
            "neutral": C.TEXT_DIM,
        }
        color = colors.get(level, C.TEXT_DIM)
        shown = (text or "Audio diagnostics: unknown").strip()
        if len(shown) > 80:
            shown = shown[:77] + "..."
        self._audio_status_lbl.setText(shown)
        self._audio_status_lbl.setStyleSheet(f"color: {color}; background: transparent;")
        if tooltip:
            self._audio_status_lbl.setToolTip(tooltip)
        else:
            self._audio_status_lbl.setToolTip(shown)

    def set_visual_watch_status(self, text: str, level: str = "neutral", tooltip: str | None = None):
        colors = {
            "ok": C.GREEN,
            "warn": C.ACC2,
            "bad": C.RED,
            "off": C.TEXT_DIM,
            "neutral": C.TEXT_DIM,
        }
        color = colors.get(level, C.TEXT_DIM)
        shown = (text or "Visual watch: unknown").strip()
        if len(shown) > 80:
            shown = shown[:77] + "..."
        self._visual_watch_status_lbl.setText(shown)
        self._visual_watch_status_lbl.setStyleSheet(f"color: {color}; background: transparent;")
        if tooltip:
            self._visual_watch_status_lbl.setToolTip(tooltip)
        else:
            self._visual_watch_status_lbl.setToolTip(shown)

    def _build_footer(self) -> QWidget:
        w = QWidget()
        w.setFixedHeight(22)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 0, 14, 0)

        def _fl(txt, color=C.TEXT_MED):
            l = QLabel(txt); l.setFont(QFont("Courier New", 7))
            l.setStyleSheet(f"color: {color}; background: transparent;")
            return l

        lay.addWidget(_fl("[F4] Mute  ·  [F11] Fullscreen"))
        lay.addWidget(self._remote_url_status_lbl)
        lay.addWidget(self._vps_status_lbl)
        lay.addWidget(self._wake_bridge_status_lbl)
        lay.addWidget(self._audio_status_lbl)
        lay.addWidget(self._visual_watch_status_lbl)
        lay.addStretch()
        lay.addWidget(_fl("By FatihMakes", C.PRI_DIM))
        return w

    def _handle_vps_reboot(self):
        url = os.getenv("JARVIS_VPS_URL")
        if not url:
            self.set_vps_status("VPS: no URL configured", "warn", "Set JARVIS_VPS_URL to enable remote reboot.")
            return
        try:
            import json
            from urllib import request, error
            req = request.Request(
                f"{url.rstrip('/')}/api/reboot",
                data=b"{}",
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            self.set_vps_status("VPS: reboot requested", "warn", str(payload))
            self._log.append_log("SYS: VPS reboot requested.")
        except (error.URLError, TimeoutError, ValueError, OSError):
            self.set_vps_status("VPS: reboot failed", "bad", "Could not reach remote server.")
            self._log.append_log("SYS: VPS reboot failed — server unreachable.")

    def _poll_vps_status(self):
        url = os.getenv("JARVIS_VPS_URL")
        if not url:
            self.set_vps_status("VPS: not configured", "neutral", "Set JARVIS_VPS_URL to monitor the remote server.")
            return
        try:
            import json
            from urllib import request, error
            with request.urlopen(f"{url.rstrip('/')}/api/ops", timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            connected = bool(payload.get("connected", payload.get("ok", False)))
            status = str(payload.get("status") or "unknown").lower()
            queue_size = payload.get("queue_size", 0)
            uptime = payload.get("uptime_seconds")

            if connected and status not in {"restarting", "error"}:
                uptime_text = ""
                if isinstance(uptime, (int, float)):
                    uptime_text = f" • {int(uptime)}s"
                self.set_vps_status(f"VPS: connected • q{queue_size}{uptime_text}", "ok", str(payload))
            elif status == "restarting":
                self.set_vps_status("VPS: restarting", "warn", str(payload))
            else:
                self.set_vps_status("VPS: unhealthy", "warn", str(payload))
        except (error.URLError, TimeoutError, ValueError, OSError):
            self.set_vps_status("VPS: offline", "bad", "Remote server is unreachable.")

    def _start_vps_status_polling(self):
        timer = QTimer(self)
        timer.timeout.connect(self._poll_vps_status)
        timer.start(12000)
        self._vps_poll_timer = timer
        self._poll_vps_status()

    def _on_file_selected(self, payload):
        paths = []
        if isinstance(payload, (list, tuple, set)):
            paths = [str(Path(p)) for p in payload if str(p).strip()]
        elif payload:
            paths = [str(Path(str(payload)))]
        paths = [p for p in paths if Path(p).is_file()]
        if not paths:
            return

        self._current_file = paths[-1]
        self._current_files = paths

        primary = Path(paths[0])
        cat = _file_category(primary)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        if len(paths) == 1:
            size = _fmt_size(primary.stat().st_size)
            self._file_hint.setText(f"{icon}  {primary.name}  ·  {size}  ·  Tell {self._assistant_name} what to do with it")
            self._log.append_log(f"FILE: {primary.name} ({size}) loaded")
        else:
            preview_names = ", ".join(Path(p).name for p in paths[:3])
            extra = f" +{len(paths) - 3} more" if len(paths) > 3 else ""
            total_size = sum(Path(p).stat().st_size for p in paths if Path(p).exists())
            total_size_str = _fmt_size(total_size)
            self._file_hint.setText(
                f"{icon}  {len(paths)} files  ·  {total_size_str}  ·  Tell {self._assistant_name} to compare them"
            )
            self._log.append_log(f"FILE: {len(paths)} files loaded ({preview_names}{extra})")

        if self.on_text_command:
            if len(paths) == 1:
                p = primary
                size = _fmt_size(primary.stat().st_size)
                msg = (
                    f"[FILE_UPLOADED] path={paths[0]} | name={p.name} | "
                    f"type={p.suffix.lstrip('.')} | size={size} | "
                    f"Briefly tell the user you can see the file '{p.name}' "
                    f"({size}) has been uploaded and ask what they'd like to do with it."
                )
            else:
                preview_names = ", ".join(Path(p).name for p in paths[:5])
                extra = f" +{len(paths) - 5} more" if len(paths) > 5 else ""
                msg = (
                    f"[FILES_UPLOADED] count={len(paths)} | paths={json.dumps(paths)} | "
                    f"names={preview_names}{extra} | "
                    "Cross-analyze these uploaded files, point out similarities, common trends, contradictions, and notable differences, then present a concise summary."
                )
            threading.Thread(target=self.on_text_command, args=(msg,), daemon=True).start()

    def notify_phone_connected(self) -> None:
        if self._remote_overlay and self._remote_overlay.isVisible():
            self._remote_overlay.mark_connected()

    def _open_remote(self):
        if not self.on_remote_clicked:
            self._log.append_log("SYS: Dashboard not running — remote unavailable.")
            return
        result = self.on_remote_clicked()
        if not result:
            self._log.append_log("SYS: Could not generate remote key.")
            return
        url    = result[0]
        key    = result[1]
        auto   = result[2] if len(result) >= 3 else ""
        manual = result[3] if len(result) >= 4 else url
        sec    = result[4] if len(result) >= 5 else "SECURITY: STATUS UNAVAILABLE"
        if self._remote_overlay:
            self._remote_overlay._do_close()
        cw  = self.centralWidget()
        ow, oh = RemoteKeyOverlay._OW, RemoteKeyOverlay._OH
        ov  = RemoteKeyOverlay(url, key, auto_login_url=auto, manual_url=manual,
                               security_status=sec,
                               expiry_secs=600, parent=cw)
        ov.set_new_key_callback(self.on_remote_clicked)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.closed.connect(lambda: setattr(self, '_remote_overlay', None))
        ov.show()
        self._remote_overlay = ov
        self._log.append_log(f"SYS: Remote key generated — manual: {manual or url}")

    def _show_live_remote_url(self):
        if not self.on_remote_url_clicked:
            self._log.append_log("SYS: Dashboard not running — no live remote URL available.")
            return

        result = self.on_remote_url_clicked()
        if not result:
            self._log.append_log("SYS: Could not resolve live remote URL.")
            return

        url = result[0]
        sec = result[1] if len(result) >= 2 else "SECURITY: STATUS UNAVAILABLE"
        try:
            QApplication.clipboard().setText(url)
            self._log.append_log(f"SYS: Live remote URL copied: {url}")
        except Exception:
            self._log.append_log(f"SYS: Live remote URL: {url}")
        self._show_content("Remote URL", f"{url}\n\n{sec}")

    def _check_autostart(self) -> bool:
        try:
            if _OS == "Windows":
                import winreg
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_READ)
                try:
                    winreg.QueryValueEx(key, "JARVIS_AI")
                    return True
                except FileNotFoundError:
                    return False
                finally:
                    winreg.CloseKey(key)
            elif _OS == "Darwin":
                return (Path.home() / "Library" / "LaunchAgents"
                        / "com.jarvis.assistant.plist").exists()
            else:
                return (Path.home() / ".config" / "autostart" / "jarvis.desktop").exists()
        except Exception:
            return False

    def _toggle_autostart(self):
        currently_on = self._check_autostart()
        try:
            script = str(Path(__file__).resolve().parent / "main.py")
            if _OS == "Windows":
                import winreg
                reg = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Run", 0, winreg.KEY_ALL_ACCESS)
                if currently_on:
                    winreg.DeleteValue(reg, "JARVIS_AI")
                else:
                    pythonw = Path(sys.executable).parent / "pythonw.exe"
                    exe = str(pythonw if pythonw.exists() else sys.executable)
                    winreg.SetValueEx(reg, "JARVIS_AI", 0, winreg.REG_SZ,
                                      f'"{exe}" "{script}"')
                winreg.CloseKey(reg)
            elif _OS == "Darwin":
                plist_dir = Path.home() / "Library" / "LaunchAgents"
                plist_dir.mkdir(parents=True, exist_ok=True)
                plist = plist_dir / "com.jarvis.assistant.plist"
                if currently_on:
                    plist.unlink(missing_ok=True)
                else:
                    plist.write_text(
                        '<?xml version="1.0" encoding="UTF-8"?>\n'
                        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
                        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                        '<plist version="1.0"><dict>\n'
                        '  <key>Label</key><string>com.jarvis.assistant</string>\n'
                        '  <key>ProgramArguments</key><array>\n'
                        f'    <string>{sys.executable}</string>\n'
                        f'    <string>{script}</string>\n'
                        '  </array>\n'
                        '  <key>RunAtLoad</key><true/>\n'
                        '</dict></plist>\n'
                    )
            else:
                desk_dir = Path.home() / ".config" / "autostart"
                desk_dir.mkdir(parents=True, exist_ok=True)
                desk = desk_dir / "jarvis.desktop"
                if currently_on:
                    desk.unlink(missing_ok=True)
                else:
                    desk.write_text(
                        "[Desktop Entry]\n"
                        f"Name={self._assistant_name}\n"
                        f"Exec={sys.executable} {script}\n"
                        "Type=Application\nTerminal=false\n"
                        "X-GNOME-Autostart-enabled=true\n"
                    )
            enabled = not currently_on
            self._update_autostart_btn(enabled)
            self._log.append_log(
                f"SYS: Auto-start {'enabled' if enabled else 'disabled'}.")
        except Exception as e:
            self._log.append_log(f"ERR: Auto-start failed — {e}")

    def _update_autostart_btn(self, enabled: bool):
        if not hasattr(self, '_autostart_btn'):
            return
        if enabled:
            self._autostart_btn.setText("◉  AUTO-START: ON")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._autostart_btn.setText("◉  AUTO-START: OFF")
            self._autostart_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _toggle_brief(self):
        from memory.config_manager import get_brief_enabled, save_brief_enabled
        new_val = not get_brief_enabled()
        save_brief_enabled(new_val)
        self._update_brief_btn(new_val)

    def _get_audio_latency_profile(self) -> str:
        cfg = _read_full_config()
        prof = str(cfg.get("audio_latency_profile", "balanced") or "balanced").strip().lower()
        return prof if prof in {"aggressive", "balanced", "safe"} else "balanced"

    def _toggle_audio_profile(self):
        order = ["aggressive", "balanced", "safe"]
        current = self._get_audio_latency_profile()
        try:
            idx = order.index(current)
        except ValueError:
            idx = 1
        new_profile = order[(idx + 1) % len(order)]

        try:
            cfg = _read_full_config()
            cfg["audio_latency_profile"] = new_profile
            API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            self._update_audio_profile_btn(new_profile)
            self._log.append_log(
                f"SYS: Audio latency profile set to {new_profile.upper()}. Restart JARVIS to apply."
            )
        except Exception as e:
            self._log.append_log(f"ERR: Audio profile update failed — {e}")

    def _update_audio_profile_btn(self, profile: str):
        if not hasattr(self, '_audio_profile_btn'):
            return
        prof = (profile or "balanced").strip().lower()
        if prof == "aggressive":
            self._audio_profile_btn.setText("🎧  AUDIO PROFILE: AGGRESSIVE")
            self._audio_profile_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #1a0a00; color: {C.ACC2};
                    border: 1px solid {C.ACC2}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #241000; }}
            """)
        elif prof == "safe":
            self._audio_profile_btn.setText("🎧  AUDIO PROFILE: SAFE")
            self._audio_profile_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00140a; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #001c0d; }}
            """)
        else:
            self._audio_profile_btn.setText("🎧  AUDIO PROFILE: BALANCED")
            self._audio_profile_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00091a; color: {C.PRI};
                    border: 1px solid {C.PRI_DIM}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: {C.PRI_GHO}; border-color: {C.PRI}; }}
            """)

    def _update_brief_btn(self, enabled: bool):
        if not hasattr(self, '_brief_btn'):
            return
        if enabled:
            self._brief_btn.setText("☀  MORNING BRIEF: ON")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #001a08; color: {C.GREEN};
                    border: 1px solid {C.GREEN_D}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #002010; }}
            """)
        else:
            self._brief_btn.setText("☀  MORNING BRIEF: OFF")
            self._brief_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _get_public_remote_enabled(self) -> bool:
        cfg = _read_full_config()
        return bool(cfg.get("public_remote_enabled", False))

    def _toggle_public_remote(self):
        enabled = not self._get_public_remote_enabled()
        try:
            cfg = _read_full_config()
            cfg["public_remote_enabled"] = enabled
            API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            self._update_public_remote_btn(enabled)
            if enabled:
                self._log.append_log("SYS: Public remote enabled. Restart JARVIS to apply tunnel mode.")
            else:
                self._log.append_log("SYS: Public remote disabled. Restart JARVIS to apply.")
        except Exception as e:
            self._log.append_log(f"ERR: Public remote toggle failed — {e}")

    def _set_public_remote_url(self):
        cfg = _read_full_config()
        current = str(cfg.get("public_remote_url", "") or "")
        text, ok = QInputDialog.getText(
            self,
            "Public Remote URL",
            "Enter fixed public URL (e.g. https://jarvis.example.com).\nLeave blank and press OK to clear.",
            QLineEdit.EchoMode.Normal,
            current,
        )
        if not ok:
            return

        clean = (text or "").strip().rstrip("/")
        if clean and not clean.startswith(("http://", "https://")):
            clean = f"https://{clean}"

        if clean:
            try:
                p = urlparse(clean)
                host = p.hostname or ""
                if p.scheme not in ("http", "https") or not host:
                    self._log.append_log("ERR: Invalid public URL. Use http(s)://domain")
                    return
                if not re.fullmatch(r"[a-zA-Z0-9.-]+", host):
                    self._log.append_log("ERR: Invalid public URL host characters.")
                    return
            except Exception:
                self._log.append_log("ERR: Invalid public URL format.")
                return

        try:
            cfg["public_remote_url"] = clean
            API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            if clean:
                self._log.append_log(f"SYS: Public remote URL set to {clean}. Restart JARVIS to apply.")
            else:
                self._log.append_log("SYS: Public remote URL cleared. Tunnel/env mode will be used.")
        except Exception as e:
            self._log.append_log(f"ERR: Could not save public remote URL — {e}")

    def _configure_remote_pin(self):
        text, ok = QInputDialog.getText(
            self,
            "Remote Privacy PIN",
            "Set an extra remote PIN (4-32 chars). Type CLEAR to disable PIN protection.",
            QLineEdit.EchoMode.Password,
            "",
        )
        if not ok:
            return

        value = (text or "").strip()
        cfg = _read_full_config()

        if value.upper() == "CLEAR":
            cfg["remote_access_pin_hash"] = ""
            try:
                API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
                self._update_remote_pin_btn(False)
                self._log.append_log("SYS: Remote PIN protection disabled.")
            except Exception as e:
                self._log.append_log(f"ERR: Could not clear remote PIN — {e}")
            return

        if len(value) < 4 or len(value) > 32:
            self._log.append_log("ERR: Remote PIN must be 4-32 characters.")
            return

        cfg["remote_access_pin_hash"] = hashlib.sha256(value.encode("utf-8")).hexdigest()
        try:
            API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            self._update_remote_pin_btn(True)
            self._log.append_log("SYS: Remote PIN protection enabled. Auto-login QR is disabled for privacy.")
        except Exception as e:
            self._log.append_log(f"ERR: Could not save remote PIN — {e}")

    def _update_public_remote_btn(self, enabled: bool):
        if not hasattr(self, '_public_remote_btn'):
            return
        if enabled:
            self._public_remote_btn.setText("☁  PUBLIC REMOTE: ON")
            self._public_remote_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00112a; color: {C.PRI};
                    border: 1px solid {C.PRI}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: {C.PRI_GHO}; }}
            """)
        else:
            self._public_remote_btn.setText("☁  PUBLIC REMOTE: OFF")
            self._public_remote_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

        self._update_remote_pin_btn(bool(_read_full_config().get("remote_access_pin_hash", "")))

    def _update_remote_pin_btn(self, enabled: bool):
        if not hasattr(self, '_remote_pin_btn'):
            return
        if enabled:
            self._remote_pin_btn.setText("🔒  REMOTE PIN: ON")
            self._remote_pin_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #1a1200; color: {C.ACC2};
                    border: 1px solid {C.ACC2}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ background: #241900; }}
            """)
        else:
            self._remote_pin_btn.setText("🔒  REMOTE PIN: OFF")
            self._remote_pin_btn.setStyleSheet(f"""
                QPushButton {{
                    background: transparent; color: {C.TEXT_DIM};
                    border: 1px solid {C.BORDER}; border-radius: 3px;
                    text-align: left; padding: 0 8px;
                }}
                QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
            """)

    def _open_customize(self):
        cfg = _read_full_config()
        if self._customize_overlay:
            self._customize_overlay.hide()
        cw = self.centralWidget()
        ov = CustomizeOverlay(
            cfg.get("assistant_name", "JARVIS") or "JARVIS",
            cfg.get("user_name", ""),
            cfg.get("ui_color", "") or DEFAULT_UI_COLOR,
            parent=cw,
        )
        ow, oh = CustomizeOverlay._OW, CustomizeOverlay._OH
        oh = min(oh, cw.height() - 16)
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.on_preview = self._preview_ui_color
        ov.saved.connect(self._apply_name_update)
        ov.show()
        self._customize_overlay = ov

    def _open_manage_profiles(self):
        if self._manage_profiles_overlay:
            self._manage_profiles_overlay.hide()
        cw = self.centralWidget()
        ov = ManageProfilesOverlay(parent=cw)
        ow, oh = 480, 400
        ov.setGeometry(
            (cw.width() - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.show()
        self._manage_profiles_overlay = ov

    def _preview_ui_color(self, hex_color: str):
        old = current_palette()
        if apply_ui_accent(hex_color):
            retheme_all_widgets(old, current_palette())

    def _apply_name_update(self, name: str, user_name: str, ui_color: str = ""):
        self._assistant_name = name.strip() or "JARVIS"
        display = self._assistant_name.upper()
        self.setWindowTitle(f"{display} — MARK XLIX")
        self._title_lbl.setText(display)
        if display in ("JARVIS", "J.A.R.V.I.S"):
            self._sub_lbl.setText("Just A Rather Very Intelligent System")
        else:
            self._sub_lbl.setText("Personal AI Assistant")
        self._log._ai_name_lc = self._assistant_name.lower()
        self.hud._assistant_name = display

        color_changed = False
        if ui_color:
            old = current_palette()
            if apply_ui_accent(ui_color):
                retheme_all_widgets(old, current_palette())
                color_changed = old["PRI"] != C.PRI

        try:
            data = _read_full_config()
            data["assistant_name"] = self._assistant_name
            data["user_name"] = user_name.strip()
            if ui_color:
                data["ui_color"] = ui_color.strip().lower()
            API_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")
            self._log.append_log(f"SYS: Identity updated — {display}")
            if color_changed:
                self._log.append_log(f"SYS: UI colour applied — {ui_color}")
        except Exception as e:
            self._log.append_log(f"ERR: Config save failed — {e}")

    def _on_clipboard_changed(self):
        try:
            text = QApplication.clipboard().text().strip()
            if len(text) >= 10:
                self._clipboard_sig.emit(text)
        except Exception:
            pass

    def _show_clipboard_panel(self, text: str):
        self._clipboard_panel.show_clipboard(text)
        self._position_clipboard_panel()

    def _position_clipboard_panel(self):
        cw = self.centralWidget()
        pw = ClipboardPanel._W
        ph = self._clipboard_panel.sizeHint().height() or ClipboardPanel._H
        x = (cw.width() - pw) // 2
        y = cw.height() - ph - 6
        self._clipboard_panel.setGeometry(x, y, pw, ph)
        self._clipboard_panel.raise_()

    def _on_clipboard_action(self, cmd: str):
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    def _do_interrupt(self):
        if self.on_interrupt:
            self.on_interrupt()

    def _toggle_camera_feed(self) -> None:
        if self._cam_streaming:
            self.stop_camera_stream()
            self._log.append_log("SYS: Camera live feed stopped.")
        else:
            self.start_camera_stream()
            self._log.append_log("SYS: Camera live feed started.")

    def _request_camera_analysis(self) -> None:
        cmd = "Analyze what you can see in the camera right now and summarize it clearly."
        self._log.append_log("SYS: Camera analysis requested.")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(cmd,), daemon=True).start()

    def _toggle_mute(self):
        self._muted = not self._muted
        self.hud.muted = self._muted
        self._style_mute_btn()
        if self._muted:
            self._apply_state("MUTED")
            self._log.append_log("SYS: Microphone muted.")
        else:
            self._apply_state("LISTENING")
            self._log.append_log("SYS: Microphone active.")

    def _style_mute_btn(self):
        if self._muted:
            self._mute_btn.setText("🔇  MICROPHONE MUTED")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #140006; color: {C.MUTED_C};
                    border: 1px solid {C.MUTED_C}; border-radius: 3px;
                }}
            """)
        else:
            self._mute_btn.setText("🎙  MICROPHONE ACTIVE")
            self._mute_btn.setStyleSheet(f"""
                QPushButton {{
                    background: #00140a; color: {C.GREEN};
                    border: 1px solid {C.GREEN}; border-radius: 3px;
                }}
                QPushButton:hover {{ background: #001f10; }}
            """)

    def _send(self):
        txt = self._input.text().strip()
        if not txt: return
        self._input.clear()
        self._log.append_log(f"You: {txt}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(txt,), daemon=True).start()

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")

    def _check_config(self) -> bool:
        if not API_FILE.exists(): return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def _show_biometric_lock(self):
        ov = BiometricLockOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 440, 360
        ov.setGeometry(
            (cw.width()  - ow) // 2,
            (cw.height() - oh) // 2,
            ow, oh,
        )
        ov.verified.connect(lambda: self._on_biometric_done(ov))
        ov.failed.connect(self._handle_biometric_failure)
        ov.manage_requested.connect(self._open_manage_profiles)
        ov.show()
        self._biometric_overlay = ov
        self._biometric_locked = True
        self._log.append_log("SYS: BiometricLock_Protocol initiated. Profile voice and visual verification pending.")

    def is_biometric_lock_active(self) -> bool:
        return self._biometric_locked

    def _handle_biometric_failure(self):
        self._log.append_log("SYS: BiometricLock_Protocol failed. Security shutdown initiated.")
        self._biometric_locked = True
        self._apply_state("LOCKED")
        if self.on_biometric_failure:
            self.on_biometric_failure()

    def _on_biometric_done(self, ov: BiometricLockOverlay):
        ov.hide()
        self._biometric_overlay = None
        self._biometric_locked = False
        self._apply_state("LISTENING")
        self._assistant_name = _read_full_config().get("assistant_name", "JARVIS") or "JARVIS"
        self._log.append_log(f"SYS: BiometricLock_Protocol cleared. {self._assistant_name} online with Stark security profiles.")

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        API_FILE.write_text(
            json.dumps({"gemini_api_key": key, "os_system": os_name}, indent=4),
            encoding="utf-8",
        )
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._show_biometric_lock()

class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = _ensure_qapplication()
        if self._app is None:
            self._window_alive = False
            self._win = None
            return
        self._app.setStyle("Fusion")
        # This process is a background VPS worker as much as it is a visible
        # HUD — closing the window (or the window dying for any other reason)
        # must not take down the asyncio loop / local-worker polling with it.
        self._app.setQuitOnLastWindowClosed(False)
        self._win = MainWindow(face_path)
        self._window_alive = True
        self._win.destroyed.connect(self._on_window_destroyed)
        self._win.show()
        self.root = _RootShim(self._app)

    def _on_window_destroyed(self, *_):
        self._window_alive = False
        self._win = None

    def _safe_window_call(self, fn_name: str, *args, **kwargs):
        if not self._window_alive or self._win is None:
            return None
        try:
            fn = getattr(self._win, fn_name)
            return fn(*args, **kwargs)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return None
            raise

    @property
    def muted(self) -> bool:
        if not self._window_alive or self._win is None:
            return True
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if not self._window_alive or self._win is None:
            return
        if v != self._win._muted:
            self._win._toggle_mute()

    @property
    def current_file(self) -> str | None:
        if not self._window_alive or self._win is None:
            return None
        return self._win._drop_zone.current_file()

    @property
    def current_files(self) -> list[str]:
        if not self._window_alive or self._win is None:
            return []
        return self._win._drop_zone.current_files()

    @property
    def on_text_command(self):
        if not self._window_alive or self._win is None:
            return None
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        if not self._window_alive or self._win is None:
            return
        self._win.on_text_command = cb

    @property
    def on_remote_clicked(self):
        if not self._window_alive or self._win is None:
            return None
        return self._win.on_remote_clicked

    @on_remote_clicked.setter
    def on_remote_clicked(self, cb):
        if not self._window_alive or self._win is None:
            return
        self._win.on_remote_clicked = cb

    @property
    def on_remote_url_clicked(self):
        if not self._window_alive or self._win is None:
            return None
        return self._win.on_remote_url_clicked

    @on_remote_url_clicked.setter
    def on_remote_url_clicked(self, cb):
        if not self._window_alive or self._win is None:
            return
        self._win.on_remote_url_clicked = cb

    @property
    def on_interrupt(self):
        if not self._window_alive or self._win is None:
            return None
        return self._win.on_interrupt

    @on_interrupt.setter
    def on_interrupt(self, cb):
        if not self._window_alive or self._win is None:
            return
        self._win.on_interrupt = cb

    @property
    def on_biometric_failure(self):
        if not self._window_alive or self._win is None:
            return None
        return self._win.on_biometric_failure

    @on_biometric_failure.setter
    def on_biometric_failure(self, cb):
        if not self._window_alive or self._win is None:
            return
        self._win.on_biometric_failure = cb

    def notify_phone_connected(self) -> None:
        self._safe_window_call("notify_phone_connected")

    def set_state(self, state: str):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._state_sig.emit(state)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def write_log(self, text: str):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._log_sig.emit(text)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def set_remote_url_status(self, url: str | None):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._remote_url_sig.emit(url)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def set_wake_bridge_status(self, text: str, level: str = "neutral", tooltip: str | None = None):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._wake_bridge_sig.emit(text, level, tooltip)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def set_audio_status(self, text: str, level: str = "neutral", tooltip: str | None = None):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._audio_status_sig.emit(text, level, tooltip)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def set_visual_watch_status(self, text: str, level: str = "neutral", tooltip: str | None = None):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._visual_watch_sig.emit(text, level, tooltip)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def wait_for_api_key(self):
        while self._window_alive and self._win is not None and (not self._win._ready or self._win._biometric_overlay is not None):
            time.sleep(0.1)

    def show_content(self, title: str, text: str):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._content_sig.emit(title[:48], text[:4000])
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def prompt_reconfig(self):
        if not self._window_alive or self._win is None:
            return
        self._win._ready = False
        self._win._reconfig_sig.emit()

    def show_camera_frame(self, img_bytes: bytes):
        if not self._window_alive or self._win is None:
            return
        try:
            self._win._camera_sig.emit(img_bytes)
        except RuntimeError as e:
            if "has been deleted" in str(e):
                self._window_alive = False
                self._win = None
                return
            raise

    def start_camera_stream(self) -> None:
        self._safe_window_call("start_camera_stream")

    def stop_camera_stream(self) -> None:
        self._safe_window_call("stop_camera_stream")

    @property
    def assistant_name(self) -> str:
        if not self._window_alive or self._win is None:
            return "JARVIS"
        return self._win._assistant_name

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")