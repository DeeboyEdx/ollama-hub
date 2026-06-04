#!/usr/bin/env python3
"""
Ollama Monitor — lightweight always-on-top overlay + system tray icon.

Shows which Ollama models are currently loaded and their VRAM/RAM/GPU usage,
plus service-status indicators for companion services (LiteLLM, MCP: WebSearch).

────────────────────────────────────────────────────────────────────────────────
Architecture overview
────────────────────────────────────────────────────────────────────────────────

Single-module, single-class design.  Everything runs in one process:

  • Main thread  — Tkinter event loop (build_window / root.mainloop).
                   Owns all widget creation and mutation.
  • Tray thread  — pystray.Icon.run() in a daemon thread; calls back onto
                   the Tk loop via root.after() to stay thread-safe.
  • DNS threads  — short-lived daemon threads spawned on demand by _poll()
                   to resolve client IP addresses without blocking the UI.
  • Periodic callbacks (all scheduled with root.after, always on main thread):
      _poll()              — fetches Ollama /api/ps + service TCP checks
      _blink_tick()        — drives per-service dot pulse animation
      _ollama_blink_tick() — drives the header dot based on GPU%

────────────────────────────────────────────────────────────────────────────────
Service activity detection
────────────────────────────────────────────────────────────────────────────────

LiteLLM   — TCP connection count → IDLE / LIGHT / MEDIUM / HEAVY
WebSearch  — McpLogMonitor: reads the newest gateway-*.log file each poll and
             classifies by bytes written since last poll.  Also detects gateway
             restarts when the active log filename changes.

In --remote mode all local-only features are disabled (log monitoring, TCP
client tracking, hostname resolution, GPU/CPU graphs).

────────────────────────────────────────────────────────────────────────────────
CLI flags
────────────────────────────────────────────────────────────────────────────────

  --url URL         Ollama base URL (default: localhost:11434)
  --poll SECS       Poll interval (default: 3 s)
  --no-gpu          Disable GPU/CPU metrics
  --no-litellm      Hide LiteLLM service indicator
  --no-websearch    Hide WebSearch service indicator
  --remote          Master toggle: disables everything that requires local access
                    (implies --no-gpu, no TCP client tracking, no log monitoring)

────────────────────────────────────────────────────────────────────────────────
Usage
────────────────────────────────────────────────────────────────────────────────

  python ollama_monitor.py
  python ollama_monitor.py --url http://my-server:11434 --remote
"""

import re
import sys
import time
import glob as _glob_mod
import socket
import threading
import tkinter as tk
import argparse
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlparse

import requests
from PIL import Image, ImageDraw
import pystray

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_POLL_SECS  = 3
HISTORY_SECS       = 180                # 3-minute window
DEFAULT_OLLAMA_URL = "http://localhost:11434"
WINDOW_W  = 290
GRAPH_H   = 48
GRAPH_W   = WINDOW_W - 32   # inner width after content padx(8) + row padx(8)

# Services to monitor (TCP reachability). Disable individually via CLI flags.
# Optional log fields enable a clickable label that tails the newest log file:
#   log_dir, log_pattern  — glob to find the active log file
#   log_tooltip           — tooltip text shown on hover
#   log_tail              — number of lines to tail (default 20)
ALL_SERVICES = [
    {"key": "litellm",   "label": "LiteLLM",        "host": "localhost", "port": 4000,
     "log_dir":     r"D:\OneDrive\Documents\QuikScripts\Projects\LocalLLM\LiteLLMSetup\logs",
     "log_pattern": "litellm-*.log",
     "log_tooltip": "Monitor logs",
     "log_tail":    30},
    {"key": "websearch", "label": "MCP: WebSearch",  "host": "localhost", "port": 8765,
     "log_dir":     r"C:\Users\aquar\mcp-servers\logs",
     "log_pattern": "gateway-*.log",
     "log_tooltip": "Monitor logs",
     "log_tail":    20},
]

# ── Palette ───────────────────────────────────────────────────────────────────
BG          = "#0d1117"
FG          = "#e6edf3"
DIM         = "#8b949e"
ACCENT      = "#58a6ff"
ROW_BG      = "#161b22"
BORDER_CLR  = "#21262d"
GREEN       = "#3fb950"
GREEN_FILL  = "#1a4228"   # dark green for graph fill
SVC_BLINK_DIM   = "#1a6b28" # mid-dark green for service dot idle-on state
SVC_RESTART_ON  = "#58a6ff" # bright blue for restart pulse on
SVC_RESTART_OFF = "#0d2645" # dim blue for restart pulse off
GPU_LINE    = "#a371f7"   # purple for GPU % line
GPU_FILL    = "#1e1035"   # dark purple for GPU % fill
ORANGE      = "#f0883e"
BLUE        = "#79c0ff"
RED         = "#f85149"
CPU_LINE    = "#39c5cf"   # teal — ollama CPU graph + label
CPU_FILL    = "#0d2d30"   # dark teal fill
HEADER_H    = 34          # fixed header height (px) for canvas underlay


# ── Helpers ───────────────────────────────────────────────────────────────────

def human_bytes(n: int) -> str:
    """Convert a byte count to a human-readable string (e.g. 1536 → '1.5 KB')."""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def time_until(iso_string: str) -> str:
    """Return a short human-readable countdown to an ISO-8601 expiry timestamp.

    Ollama returns nanosecond-precision timestamps; we truncate to microseconds
    so fromisoformat() can parse them on Python 3.10.
    """
    try:
        s = re.sub(r"(\.\d{6})\d+", r"\1", iso_string).replace("Z", "+00:00")
        exp = datetime.fromisoformat(s)
        secs = int((exp - datetime.now(timezone.utc)).total_seconds())
        if secs <= 0:
            return "expiring…"
        if secs < 60:
            return f"{secs}s"
        return f"{secs // 60}m {secs % 60}s"
    except Exception:
        return "?"


def fetch_models(base_url: str):
    """Query Ollama /api/ps for currently loaded models.

    Returns (models_list, error_str).  On success error_str is None.
    On connection failure models_list is None and error_str is "offline".
    """
    try:
        r = requests.get(f"{base_url}/api/ps", timeout=2)
        r.raise_for_status()
        return r.json().get("models", []), None
    except requests.exceptions.ConnectionError:
        return None, "offline"
    except Exception as e:
        return None, str(e)


def check_tcp_port(host: str, port: int, timeout: float = 0.3) -> bool:
    """Return True if a TCP connection can be established to host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _get_tcp_client_ips(port: int) -> list[str]:
    """Return deduplicated list of remote IPs with ESTABLISHED connections to port.

    Requires psutil (and admin rights on some Windows configurations).
    Returns [] gracefully if psutil is unavailable or the call fails.
    """
    if not _PSUTIL_OK:
        return []
    try:
        seen: list[str] = []
        seen_set: set[str] = set()
        for c in _psutil.net_connections(kind="tcp"):
            if c.laddr.port == port and c.status == "ESTABLISHED" and c.raddr:
                ip = c.raddr.ip
                if ip not in seen_set:
                    seen_set.add(ip)
                    seen.append(ip)
        return seen
    except Exception:
        return []


class McpLogMonitor:
    """Activity classifier for a log-writing MCP gateway service.

    Each poll() call inspects the newest log file that matches the glob pattern
    and classifies the gateway's workload by how many bytes were written since
    the previous call.

    State machine
    ─────────────
    UNAVAILABLE   Log directory is missing or no matching files exist.
    RESTARTED     The active log filename changed since the last poll — the
                  gateway process restarted (or crashed and was relaunched).
                  This state fires exactly ONCE per restart event; subsequent
                  polls return the real activity level.
    IDLE          No bytes written for ≥ IDLE_AGE seconds.
    JUST_FINISHED File was written recently but nothing new this poll (delta==0).
                  The gateway finished a request and is now waiting quietly.
    LIGHT         Low write rate (≤ LIGHT_MAX bytes/poll).
    MEDIUM        Moderate write rate (≤ MEDIUM_MAX bytes/poll).
    HEAVY         High write rate (> MEDIUM_MAX bytes/poll).

    Thresholds are scaled from a 5 s reference poll to the actual 3 s poll rate
    (multiply spec values by 3/5).
    """
    IDLE_AGE   = 30.0     # seconds since last write → IDLE
    LIGHT_MAX  = 6_000    # bytes/poll (spec 10 KB × 3/5 for 3s poll)
    MEDIUM_MAX = 45_000   # bytes/poll (spec 75 KB × 3/5)

    def __init__(self, log_dir: str, log_pattern: str):
        self.log_dir     = log_dir
        self.log_pattern = log_pattern
        self._current_log: str | None = None   # path of log file seen last poll
        self._last_size: int = 0               # byte size at last poll

    def poll(self) -> str:
        """Read the newest matching log file and return the current activity state."""
        import os
        try:
            # Sort by mtime so the last element is always the most recent file
            files = sorted(
                _glob_mod.glob(os.path.join(self.log_dir, self.log_pattern)),
                key=os.path.getmtime,
            )
        except Exception:
            return "UNAVAILABLE"
        if not files:
            return "UNAVAILABLE"

        newest = files[-1]
        # A different filename means the gateway restarted and opened a new log
        restarted = newest != self._current_log
        if restarted:
            self._current_log = newest
            self._last_size = 0   # reset byte baseline for the new file

        try:
            import os as _os
            stat = _os.stat(newest)
        except OSError:
            return "UNAVAILABLE"

        age   = time.time() - stat.st_mtime         # seconds since last write
        size  = stat.st_size
        delta = max(0, size - self._last_size)       # bytes written this poll
        self._last_size = size

        # RESTARTED is returned only on the first poll after a filename change
        if restarted:
            return "RESTARTED"
        if age >= self.IDLE_AGE:
            return "IDLE"
        if delta == 0:
            return "JUST_FINISHED"                   # recent activity, but quiet now
        if delta <= self.LIGHT_MAX:
            return "LIGHT"
        if delta <= self.MEDIUM_MAX:
            return "MEDIUM"
        return "HEAVY"


# Blink half-cycle durations (ms) — time dot stays ON or OFF before toggling.
# Faster half-cycles = faster visible pulse = higher perceived activity.
_BLINK_FAST_MS    = 150
_BLINK_MED_MS     = 350
_BLINK_SLOW_MS    = 700
_BLINK_IDLE_MS    = 1000   # minimum re-schedule interval when nothing is pulsing
_RESTART_BLINK_MS = 700    # blue restart pulse: same tempo as LIGHT

# Maps McpLogMonitor state → dot half-cycle duration.
# IDLE and UNAVAILABLE are absent intentionally — they render as a solid dim dot
# with no scheduling, so _blink_tick skips them (half_cycle == 0 fallthrough).
_STATE_BLINK_MS: dict[str, int] = {
    "JUST_FINISHED": 1500,   # slow breathing — activity just wrapped up
    "LIGHT":          700,   # gentle slow pulse
    "MEDIUM":         350,   # moderate pulse
    "HEAVY":          150,   # rapid pulse
    "RESTARTED":      700,   # handled via _svc_restart_until; same tempo as LIGHT
}


class _Tooltip:
    """Simple hover tooltip for any Tkinter widget.

    Creates a borderless Toplevel just below the widget on <Enter> and
    destroys it on <Leave>.
    """
    def __init__(self, widget: tk.Widget, text: str):
        self._widget = widget
        self._text   = text
        self._tw: tk.Toplevel | None = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")

    def _show(self, event=None):
        x = self._widget.winfo_rootx() + 10
        y = self._widget.winfo_rooty() + self._widget.winfo_height() + 4
        self._tw = tw = tk.Toplevel(self._widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        tw.attributes("-topmost", True)
        tk.Label(tw, text=self._text, bg="#1c2128", fg=FG,
                 font=("Segoe UI", 8), padx=6, pady=3).pack()

    def _hide(self, event=None):
        if self._tw:
            self._tw.destroy()
            self._tw = None

# ── GPU utilisation (NVML / nvidia-smi fallback) ──────────────────────────────
# Try pynvml first (fast in-process NVML bindings).  Fall back to shelling out
# to nvidia-smi when pynvml is not installed.  _NVML_OK gates all nvml calls.

try:
    import warnings as _w
    with _w.catch_warnings():
        _w.simplefilter("ignore")
        import pynvml as _nvml
    _nvml.nvmlInit()
    _nvml_handle = _nvml.nvmlDeviceGetHandleByIndex(0)
    _NVML_OK = True
except Exception:
    _NVML_OK = False

# psutil is used for TCP connection inspection and per-process CPU%.
# It may require admin rights on some Windows configurations.
try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False


def fetch_gpu_pct() -> float | None:
    """Return GPU core utilisation % (0–100), or None if unavailable.

    Prefers pynvml (zero-subprocess overhead).  Falls back to nvidia-smi
    with CREATE_NO_WINDOW so no console flashes on Windows.
    """
    if _NVML_OK:
        try:
            util = _nvml.nvmlDeviceGetUtilizationRates(_nvml_handle)
            return float(util.gpu)
        except Exception:
            pass
    # Fallback: nvidia-smi subprocess (slower but works without pynvml)
    try:
        import subprocess
        out = subprocess.check_output(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            timeout=1, stderr=subprocess.DEVNULL,
            creationflags=0x08000000,   # CREATE_NO_WINDOW on Windows
        )
        return float(out.decode().strip().splitlines()[0])
    except Exception:
        return None


def make_tray_icon(color: str) -> Image.Image:
    """Render a solid-colour circle into a 64×64 RGBA image for the system tray."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([6, 6, 58, 58], fill=color)
    return img


# ── Overlay window ────────────────────────────────────────────────────────────

class OllamaOverlay:
    def __init__(self, ollama_url: str, poll_secs: int = DEFAULT_POLL_SECS,
                 gpu_enabled: bool = True, services: list | None = None,
                 remote: bool = False):
        self.ollama_url      = ollama_url
        self.poll_interval_ms = poll_secs * 1000
        # Number of history samples that fit in the rolling 3-minute window
        self.max_history     = max(2, HISTORY_SECS * 1000 // self.poll_interval_ms)
        self.gpu_enabled     = gpu_enabled and not remote  # remote implies no-gpu
        self.remote          = remote
        self.services        = services if services is not None else list(ALL_SERVICES)

        # Derive the service host from the Ollama URL so that --url / OLLAMA_SERVER_URL
        # automatically points service checks at the same machine as the Ollama API.
        _svc_host = urlparse(ollama_url).hostname or "localhost"
        self.services = [{**svc, "host": _svc_host} for svc in self.services]
        self.visible = True
        self.root: tk.Tk | None = None
        self.tray: pystray.Icon | None = None
        self._drag_x = self._drag_y = 0

        # Tkinter widget references — keyed by model name
        self._model_widgets: dict[str, dict] = {}

        # Ghost rows: models unloaded within the last 60 s
        # Value dict holds the tk.Frame and the monotonic time of unload.
        self._ghost_widgets: dict[str, dict] = {}

        self._last_status = ""   # tracks "online" | "idle" | "offline" to avoid redundant updates

        # Rolling deques for graph history
        self._vram_history: dict[str, deque] = {}    # per-model VRAM samples
        self._gpu_history:  deque = deque(maxlen=self.max_history)
        self._cpu_history:  deque = deque()           # (monotonic_ts, pct) tuples; pruned by age

        # CPU freeze logic: once Ollama CPU hits 0% we keep appending for HISTORY_SECS,
        # then freeze (stop appending) so the graph line doesn't scroll to a flat baseline.
        self._zero_since:   float | None = None  # monotonic time CPU first hit 0%
        self._had_ram_model: bool = False         # True once any RAM-using model was seen;
                                                   # gates CPU graph visibility

        # "new" badge: track when each model was first seen this session
        self._model_first_seen: dict[str, float] = {}  # model name → monotonic timestamp
        self._poll_count: int = 0                        # suppresses badge on startup models

        # Per-service state (all keyed by svc["key"])
        self._service_up:        dict[str, bool]        = {s["key"]: False for s in self.services}
        self._service_activity:  dict[str, int]         = {s["key"]: 0     for s in self.services}
        self._service_state:     dict[str, str]         = {s["key"]: "IDLE" for s in self.services}
        self._service_client_ips: dict[str, list[str]] = {s["key"]: []     for s in self.services}

        # Service dot widget references (populated in _build_services_footer)
        self._svc_dot_labels:    dict[str, tk.Label]   = {}
        self._svc_client_labels: dict[str, tk.Label]   = {}

        # Blink animation state per service
        self._svc_blink_state:   dict[str, bool]       = {s["key"]: False  for s in self.services}
        self._svc_next_toggle:   dict[str, float]      = {s["key"]: 0.0    for s in self.services}
        # monotonic deadline until which the restart (blue) animation overrides the normal dot colour
        self._svc_restart_until: dict[str, float]      = {s["key"]: 0.0    for s in self.services}

        # Log-file monitors — only created for services that have a log_dir and when not remote
        self._log_monitors: dict[str, McpLogMonitor] = {}
        if not remote:
            for svc in self.services:
                if svc.get("log_dir"):
                    self._log_monitors[svc["key"]] = McpLogMonitor(
                        svc["log_dir"], svc["log_pattern"]
                    )

        # Hostname resolution cache; pre-populate common loopback addresses
        self._hostname_cache: dict[str, str] = {
            "127.0.0.1": "localhost", "::1": "localhost", "0.0.0.0": "localhost",
        }
        self._resolving: set[str] = set()  # IPs currently being resolved in a background thread

        # Tracks when each client IP was first seen per service (for newest-first sort)
        self._client_first_seen: dict[str, dict[str, float]] = {s["key"]: {} for s in self.services}

        self._ollama_proc         = None   # cached list of psutil Process objects for ollama*
        self._after_id            = None   # ID of the pending _poll root.after callback

    # ── Window construction ───────────────────────────────────────────────────

    def build_window(self):
        """Create the Tkinter root window and start the event loop.

        This call blocks until the window is destroyed (i.e., the user quits).
        The tray icon is started in a daemon thread before this is called.
        """
        self.root = tk.Tk()
        self.root.title("Ollama Monitor")
        self.root.overrideredirect(True)          # borderless / no title bar
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.93)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        # Position in the top-right corner, 16 px from the screen edge
        sw = self.root.winfo_screenwidth()
        x = sw - WINDOW_W - 16
        self.root.geometry(f"+{x}+10")
        self.root.minsize(WINDOW_W, 1)
        self.root.maxsize(WINDOW_W, self.root.winfo_screenheight())

        self.root.bind("<ButtonPress-1>", self._on_drag_start)
        self.root.bind("<B1-Motion>", self._on_drag_move)
        self.root.bind("<ButtonPress-3>", self._show_context_menu)

        self._build_header()
        self._build_content_area()
        self._build_services_footer()
        if self.services:
            self.root.after(200, self._blink_tick)     # start service dot animation
        self.root.after(200, self._ollama_blink_tick)  # start Ollama header dot animation

        self.root.after(100, self._poll)               # first data fetch after brief delay
        self.root.mainloop()

    def _build_header(self):
        """Build the fixed-height title bar.

        Uses a Canvas instead of nested Frames so all text items can sit on top
        of the CPU graph (drawn as canvas polygons) without an opaque widget
        background obscuring them.  The canvas items are stored as IDs so _poll
        can update them in place.
        """
        tk.Frame(self.root, bg=BORDER_CLR, height=1).pack(fill="x")

        self.header_canvas = tk.Canvas(
            self.root, bg=BG, highlightthickness=0, bd=0, height=HEADER_H
        )
        self.header_canvas.pack(fill="x")

        cy = HEADER_H // 2   # vertical centre of header

        # Static and dynamic text items — all drawn on canvas, no opaque widget bg
        self._hdr_dot_id   = self.header_canvas.create_text(
            10, cy, text="●", fill=DIM, font=("Segoe UI", 10), anchor="w", tags="hdr"
        )
        self._hdr_title_id = self.header_canvas.create_text(
            26, cy, text="OLLAMA", fill=FG, font=("Segoe UI", 9, "bold"), anchor="w", tags="hdr"
        )
        self._hdr_peak_id  = self.header_canvas.create_text(
            WINDOW_W - 120, 3, text="", fill=DIM, font=("Segoe UI", 7), anchor="n", tags="hdr"
        )
        self._hdr_cpu_id   = self.header_canvas.create_text(
            WINDOW_W - 78, cy, text="", fill=CPU_LINE, font=("Segoe UI", 8), anchor="e", tags="hdr"
        )
        self._hdr_time_id  = self.header_canvas.create_text(
            WINDOW_W - 10, cy, text="", fill=DIM, font=("Segoe UI", 8), anchor="e", tags="hdr"
        )

        # Bind drag + right-click on the canvas (no separate frame needed)
        self.header_canvas.bind("<ButtonPress-1>", self._on_drag_start)
        self.header_canvas.bind("<B1-Motion>",     self._on_drag_move)
        self.header_canvas.bind("<ButtonPress-3>", self._show_context_menu)

        tk.Frame(self.root, bg=BORDER_CLR, height=1).pack(fill="x")

    def _build_content_area(self):
        """Create the scrollable model-list area.

        Model rows are added/removed dynamically by _update_ui.
        The placeholder label is shown when no models are loaded.
        """
        self.content = tk.Frame(self.root, bg=BG, padx=8, pady=6)
        self.content.pack(fill="both", expand=True)

        self.placeholder = tk.Label(
            self.content, text="Connecting…", fg=DIM, bg=BG,
            font=("Segoe UI", 9), pady=10
        )
        self.placeholder.pack()

    def _build_services_footer(self):
        """Build the service-status footer below the model list.

        Each service gets a column containing:
          • A coloured dot (●) + label on one row (indicator)
          • A Label below it for connected client hostnames (hidden when empty)

        Columns are top-aligned (anchor="n") so that a service with no clients
        doesn't vertically centre itself against one that does have clients.
        """
        if not self.services:
            return
        tk.Frame(self.root, bg=BORDER_CLR, height=1).pack(fill="x")
        footer = tk.Frame(self.root, bg=BG, padx=8, pady=7)
        footer.pack(fill="x")
        for svc in self.services:
            col = tk.Frame(footer, bg=BG)
            col.pack(side="left", expand=True, fill="x", anchor="n")

            indicator = tk.Frame(col, bg=BG)
            indicator.pack(fill="x", anchor="w")
            dot = tk.Label(indicator, text="●", fg=DIM, bg=BG, font=("Segoe UI", 8))
            dot.pack(side="left")
            svc_lbl = tk.Label(indicator, text=f" {svc['label']}", fg=DIM, bg=BG,
                               font=("Segoe UI", 8))
            svc_lbl.pack(side="left")
            # If this service has local logs, make the label clickable
            if svc.get("log_dir") and not self.remote:
                svc_lbl.config(cursor="hand2")
                svc_lbl.bind("<Button-1>", lambda e, s=svc: self._open_log_tail(s))
                _Tooltip(svc_lbl, svc.get("log_tooltip", "Monitor logs"))
            self._svc_dot_labels[svc["key"]] = dot

            client_lbl = tk.Label(col, text="", fg=DIM, bg=BG,
                                  font=("Segoe UI", 7), anchor="w", justify="left")
            client_lbl.pack(fill="x", padx=(10, 0))
            self._svc_client_labels[svc["key"]] = client_lbl

    def _update_services_footer(self):
        # Kept for manual one-shot refresh; ongoing updates handled by _blink_tick.
        for svc in self.services:
            up = self._service_up.get(svc["key"], False)
            dot = self._svc_dot_labels.get(svc["key"])
            if dot:
                dot.config(fg=SVC_BLINK_DIM if up else RED)

    def _resolve_hostname(self, ip: str):
        """Reverse-DNS lookup run in a background daemon thread.

        Writes the result into _hostname_cache and schedules a label refresh
        back on the main thread via root.after(0, ...).
        """
        try:
            hostname = socket.gethostbyaddr(ip)[0]
        except Exception:
            hostname = ip   # fall back to the raw IP if resolution fails
        self._hostname_cache[ip] = hostname
        self._resolving.discard(ip)
        if self.root:
            self.root.after(0, self._refresh_client_labels)

    def _refresh_client_labels(self):
        """Redraw the hostname list under each service dot.

        IPs are already sorted newest-first in _service_client_ips.
        We lowercase every hostname for visual consistency.
        Labels are pack_forget()d (not just blanked) when empty so the footer
        collapses to the same height as the title bar when no clients are present.
        """
        any_clients = False
        for svc in self.services:
            lbl = self._svc_client_labels.get(svc["key"])
            if not lbl:
                continue
            ips = self._service_client_ips.get(svc["key"], [])
            names = [self._hostname_cache.get(ip, ip).lower() for ip in ips]
            if names:
                lbl.config(text="\n".join(names))
                lbl.pack(fill="x", padx=(10, 0))
                any_clients = True
            else:
                lbl.config(text="")
                lbl.pack_forget()
        _ = any_clients  # reserved for future footer height adjustment if needed

    def _open_log_tail(self, svc: dict):
        """Open a new PowerShell window tailing the latest log for this service.

        Finds the newest file matching the service's log_dir/log_pattern and
        streams its last 20 lines with -Wait (like `tail -f`).
        Only called for services that have a log_dir and when not in remote mode.
        """
        import subprocess
        log_dir     = svc.get("log_dir", "")
        log_pattern = svc.get("log_pattern", "*.log")
        tail        = svc.get("log_tail", 20)
        glob_path   = f"{log_dir}\\{log_pattern}"
        ps_cmd = (
            f"Get-ChildItem '{glob_path}' | Sort-Object LastWriteTime -Descending "
            f"| Select-Object -First 1 -ExpandProperty FullName "
            f"| ForEach-Object {{ Get-Content $_ -Wait -Tail {tail} }}"
        )
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NoExit", "-Command", ps_cmd],
            creationflags=0x00000010,  # CREATE_NEW_CONSOLE — opens a visible window
        )

    def _state_for_service(self, key: str) -> str:
        """Return a unified activity state string regardless of detection method.

        Log-monitored services return their McpLogMonitor state directly.
        TCP-only services map connection count to IDLE/LIGHT/MEDIUM/HEAVY.
        """
        if key in self._log_monitors:
            return self._service_state.get(key, "IDLE")
        count = self._service_activity.get(key, 0)
        if count >= 4:   return "HEAVY"
        elif count >= 2: return "MEDIUM"
        elif count >= 1: return "LIGHT"
        return "IDLE"

    def _blink_tick(self):
        """Drive the per-service dot pulse animation.

        This method reschedules itself at the earliest time any service dot
        needs to toggle — so when all services are idle (no pulsing needed) it
        wakes up at most once per second, but during heavy activity it can wake
        every 150 ms.

        Priority order for each dot:
          1. Service is down  → solid red, no scheduling
          2. RESTARTED blue   → blue ON/OFF at _RESTART_BLINK_MS until deadline
          3. Normal activity  → green ON/dim-green OFF at _STATE_BLINK_MS rate
          4. IDLE/UNAVAILABLE → solid dim green, no scheduling
        """
        _now = time.monotonic()
        min_next = _now + 1.0  # fallback: check again in 1 s if nothing is pulsing

        for svc in self.services:
            key = svc["key"]
            dot = self._svc_dot_labels.get(key)
            if not dot:
                continue
            up = self._service_up.get(key, False)

            if not up:
                dot.config(fg=RED)
                self._svc_blink_state[key] = False
                continue

            # Restart animation overrides normal activity colours
            if _now < self._svc_restart_until.get(key, 0.0):
                if _now >= self._svc_next_toggle[key]:
                    blink = not self._svc_blink_state[key]
                    self._svc_blink_state[key] = blink
                    dot.config(fg=SVC_RESTART_ON if blink else SVC_RESTART_OFF)
                    self._svc_next_toggle[key] = _now + _RESTART_BLINK_MS / 1000
                min_next = min(min_next, self._svc_next_toggle[key])
                continue

            state      = self._state_for_service(key)
            half_cycle = _STATE_BLINK_MS.get(state, 0)

            if half_cycle == 0:
                # IDLE / UNAVAILABLE — solid dim green, no scheduling needed
                dot.config(fg=SVC_BLINK_DIM)
                self._svc_blink_state[key] = False
            else:
                if _now >= self._svc_next_toggle[key]:
                    blink = not self._svc_blink_state[key]
                    self._svc_blink_state[key] = blink
                    dot.config(fg=GREEN if blink else SVC_BLINK_DIM)
                    self._svc_next_toggle[key] = _now + half_cycle / 1000
                min_next = min(min_next, self._svc_next_toggle[key])

        # Schedule the next tick only as far away as the soonest pending toggle
        delay_ms = max(50, int((min_next - time.monotonic()) * 1000))
        if self.root:
            self.root.after(delay_ms, self._blink_tick)


    # ── Drag ─────────────────────────────────────────────────────────────────

    def _on_drag_start(self, event):
        self._drag_x, self._drag_y = event.x, event.y

    def _on_drag_move(self, event):
        x = self.root.winfo_x() + event.x - self._drag_x
        y = self.root.winfo_y() + event.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    # ── Context menu ──────────────────────────────────────────────────────────

    def _show_context_menu(self, event):
        menu = tk.Menu(self.root, tearoff=False, bg=ROW_BG, fg=FG,
                       activebackground=BORDER_CLR, activeforeground=FG,
                       bd=0, font=("Segoe UI", 9))
        menu.add_command(label="Hide overlay", command=self._hide)
        menu.add_separator()
        menu.add_command(label="Open Ollama logs", command=self._open_ollama_logs)
        menu.add_separator()
        menu.add_command(label="Quit", command=self._quit)
        menu.tk_popup(event.x_root, event.y_root)

    def _open_ollama_logs(self):
        """Tail the Ollama server log in a new PowerShell window."""
        import subprocess
        ps_cmd = r'Get-Content "$env:LOCALAPPDATA\Ollama\server.log" -Wait -Tail 30'
        subprocess.Popen(
            ["powershell", "-NoProfile", "-NoExit", "-Command", ps_cmd],
            creationflags=0x00000010,
        )

    def _reset_position(self):
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"+{sw - WINDOW_W - 16}+10")

    # ── Poll + UI update ──────────────────────────────────────────────────────

    def _poll(self):
        """Main periodic callback — runs every poll_interval_ms on the Tk event loop.

        Sequence each tick:
          1. Increment poll counter (used to suppress the "new" badge at startup)
          2. Fetch loaded models from Ollama /api/ps
          3. Sample GPU% and Ollama process CPU% (if enabled)
          4. Prune CPU history samples older than HISTORY_SECS
          5. For each service:
             a. TCP port check → mark up/down
             b. Activity level via log monitor (if configured) or TCP client count
             c. Client IP tracking + background hostname resolution (local mode only)
          6. Refresh hostname labels
          7. Rebuild/update model rows (_update_ui)
          8. Re-schedule itself
        """
        self._poll_count += 1
        models, error = fetch_models(self.ollama_url)
        gpu_pct = fetch_gpu_pct() if self.gpu_enabled else None
        self._gpu_history.append(gpu_pct)
        cpu_pct = self._get_ollama_cpu_pct() if self.gpu_enabled else None
        _now = time.monotonic()
        if cpu_pct is not None:
            if cpu_pct >= 0.5:   # treat < 0.5% (displays as "0%") as truly idle
                self._zero_since = None
                self._cpu_history.append((_now, cpu_pct))
            else:
                if self._zero_since is None:
                    self._zero_since = _now
                if _now - self._zero_since < HISTORY_SECS:
                    self._cpu_history.append((_now, 0.0))
                # else: >3 min of 0% — freeze; old entries age out naturally
        # Prune samples that have scrolled off the left edge of the graph
        while self._cpu_history and _now - self._cpu_history[0][0] > HISTORY_SECS:
            self._cpu_history.popleft()
        for svc in self.services:
            key = svc["key"]
            up  = check_tcp_port(svc["host"], svc["port"])
            self._service_up[key] = up

            if not up:
                # Service is down — reset all derived state so it's clean when it comes back
                self._service_activity[key]   = 0
                self._service_state[key]      = "IDLE"
                self._service_client_ips[key] = []
                self._svc_restart_until[key]  = 0.0
                continue

            # ── Activity level ─────────────────────────────────────────────
            if key in self._log_monitors:
                state = self._log_monitors[key].poll()
                self._service_state[key] = state
                if state == "RESTARTED":
                    # Arm the blue restart animation for 4 seconds
                    self._svc_restart_until[key] = time.monotonic() + 4.0
                self._service_activity[key] = 0 if state in ("IDLE", "UNAVAILABLE") else 1
            elif not self.remote:
                conn_count = len(_get_tcp_client_ips(svc["port"]))
                self._service_activity[key] = conn_count

            # ── Client IP tracking (local only) ────────────────────────────
            if not self.remote:
                ips = _get_tcp_client_ips(svc["port"])
                # Prune IPs that are no longer connected
                self._client_first_seen[key] = {
                    ip: ts for ip, ts in self._client_first_seen[key].items()
                    if ip in set(ips)
                }
                # Record first-seen time for newly connected IPs
                for ip in ips:
                    if ip not in self._client_first_seen[key]:
                        self._client_first_seen[key][ip] = _now
                # Sort newest-first so the most recently connected client appears at the top
                sorted_ips = sorted(
                    ips,
                    key=lambda ip: self._client_first_seen[key].get(ip, 0),
                    reverse=True,
                )
                self._service_client_ips[key] = sorted_ips
                # Kick off background DNS resolution for any unseen IP
                for ip in sorted_ips:
                    if ip not in self._hostname_cache and ip not in self._resolving:
                        self._resolving.add(ip)
                        threading.Thread(
                            target=self._resolve_hostname, args=(ip,), daemon=True
                        ).start()
        self._refresh_client_labels()
        self._update_ui(models, error)
        if self.root:
            self._after_id = self.root.after(self.poll_interval_ms, self._poll)

    def _update_ui(self, models, error):
        # Record VRAM history first
        if models:
            for m in models:
                name = m.get("name", "unknown")
                vram = m.get("size_vram", 0)
                if name not in self._vram_history:
                    self._vram_history[name] = deque(maxlen=self.max_history)
                self._vram_history[name].append(vram)

        now_str = datetime.now().strftime("%H:%M:%S")
        self.header_canvas.itemconfig(self._hdr_time_id, text=now_str)

        if error == "offline":
            self._set_status("offline")
            self._clear_all_model_rows()
            self.placeholder.config(text="Ollama offline", fg=RED)
            self.placeholder.pack()
            self._update_cpu_header(any_ram=False)
            return

        if models is None:
            self._set_status("offline")
            self._clear_all_model_rows()
            self.placeholder.config(text="Connection error", fg=ORANGE)
            self.placeholder.pack()
            self._update_cpu_header(any_ram=False)
            return

        # Ollama is reachable — handle active models and ghost rows together
        current_names = {m.get("name", "unknown") for m in models} if models else set()

        # Ghost any model that just dropped off the active list
        for name in list(self._model_widgets.keys()):
            if name not in current_names:
                self._remove_model_row(name)
                self._create_ghost_row(name)

        # Expire ghosts older than 60 s; also clear ghosts for models that reloaded
        mono_now = time.monotonic()
        for name in list(self._ghost_widgets.keys()):
            if name in current_names or mono_now - self._ghost_widgets[name]["unloaded_at"] > 60:
                self._remove_ghost_row(name)

        if models:
            self._set_status("online")
            any_ram = any(
                max(0, m.get("size", 0) - m.get("size_vram", 0)) > 0 for m in models
            )
            self._update_cpu_header(any_ram=any_ram)

            for m in models:
                name = m.get("name", "unknown")
                if name in self._model_widgets:
                    self._update_model_row(name, m)
                else:
                    self._create_model_row(m)
        else:
            self._set_status("idle")
            self._update_cpu_header(any_ram=False)

        # Show placeholder only when nothing is visible (no active rows, no ghosts)
        if not models and not self._ghost_widgets:
            self.placeholder.config(text="No models loaded", fg=DIM)
            self.placeholder.pack()
        else:
            self.placeholder.pack_forget()

    def _clear_all_model_rows(self):
        for name in list(self._model_widgets.keys()):
            self._remove_model_row(name)
        for name in list(self._ghost_widgets.keys()):
            self._remove_ghost_row(name)

    def _remove_model_row(self, name: str):
        if name in self._model_widgets:
            self._model_widgets[name]["frame"].destroy()
            del self._model_widgets[name]
        self._model_first_seen.pop(name, None)

    def _create_ghost_row(self, name: str):
        """Show a collapsed, dimmed row for a recently-unloaded model (expires after 60 s)."""
        if name in self._ghost_widgets:
            return
        tag = ""
        base_name = name
        if ":" in name:
            base_name, tag = name.rsplit(":", 1)
        display_name = base_name if (not tag or tag == "latest") else f"{base_name}:{tag}"

        unloaded_time_str = datetime.now().strftime("%H:%M:%S")

        row = tk.Frame(self.content, bg=ROW_BG, padx=8, pady=4)
        row.pack(fill="x", pady=2)

        r1 = tk.Frame(row, bg=ROW_BG)
        r1.pack(fill="x")
        tk.Label(r1, text=display_name, fg=DIM, bg=ROW_BG,
                 font=("Segoe UI", 9), anchor="w").pack(side="left")
        tk.Label(r1, text=f"last seen {unloaded_time_str}", fg="#555555", bg=ROW_BG,
                 font=("Segoe UI", 7)).pack(side="right")

        self._ghost_widgets[name] = {"frame": row, "unloaded_at": time.monotonic()}

    def _remove_ghost_row(self, name: str):
        if name in self._ghost_widgets:
            self._ghost_widgets[name]["frame"].destroy()
            del self._ghost_widgets[name]

    def _set_status(self, status: str):
        """Update the header dot colour and tray icon to reflect Ollama reachability.

        Only redraws when status actually changes to avoid unnecessary canvas updates.
        The tray map uses a fully-bright green for "online" so the icon is clearly
        visible; the header dot is dim-green (SVC_BLINK_DIM) because _ollama_blink_tick
        overrides it with bright green when GPU is active.
        """
        if status == self._last_status:
            return
        self._last_status = status
        dot_map  = {"online": SVC_BLINK_DIM, "idle": DIM, "offline": RED}
        tray_map = {"online": GREEN,          "idle": DIM, "offline": RED}
        self.header_canvas.itemconfig(self._hdr_dot_id, fill=dot_map.get(status, DIM))
        if self.tray:
            self.tray.icon = make_tray_icon(tray_map.get(status, DIM))

    def _ollama_blink_tick(self):
        """Flash the header dot bright green while the GPU is active (>5%).

        Runs on a fixed 1-second interval — much slower than _blink_tick because
        GPU% doesn't need sub-second responsiveness.  Only applies while Ollama
        is online; offline/idle states are handled by _set_status.
        """
        gpu = self._gpu_history[-1] if self._gpu_history else None
        if self._last_status == "online" and self.gpu_enabled:
            active = gpu is not None and gpu > 5
            color = GREEN if active else SVC_BLINK_DIM
            self.header_canvas.itemconfig(self._hdr_dot_id, fill=color)
        if self.root:
            self.root.after(_BLINK_IDLE_MS, self._ollama_blink_tick)

    def _get_ollama_cpu_pct(self) -> float | None:
        """Return summed CPU % for all ollama* processes, normalised to 0–100.

        psutil.cpu_percent() per process returns a value that can exceed 100 on
        multi-core systems (e.g. 200% means two full cores).  We divide by the
        logical core count to normalise to a 0-100 scale.

        Processes are cached across calls because scanning the full process table
        every 3 seconds is expensive.  Dead processes are pruned each call, and
        any newly started ollama* processes are added.

        Note: the first cpu_percent() call on a new Process object always returns
        0 (it just primes the measurement baseline).  The real value appears on
        the next call, which is the following poll tick.
        """
        if not _PSUTIL_OK:
            return None
        # Remove any dead processes from cache
        self._ollama_proc = [p for p in (self._ollama_proc or [])
                             if self._is_proc_alive(p)]
        # Find newly appeared ollama processes
        try:
            cached_pids = {p.pid for p in self._ollama_proc}
            for p in _psutil.process_iter(["name", "pid"]):
                name = p.info["name"].lower()
                if "ollama" in name and "app" not in name and p.pid not in cached_pids:
                    try:
                        p.cpu_percent()   # prime — first call always returns 0
                        self._ollama_proc.append(p)
                    except Exception:
                        pass
        except Exception:
            pass
        if not self._ollama_proc:
            return None
        # Sum CPU across all ollama processes, normalised to 0-100
        total = 0.0
        n_cores = _psutil.cpu_count(logical=True) or 1
        for p in self._ollama_proc:
            try:
                total += p.cpu_percent(interval=None)
            except Exception:
                pass
        return min(100.0, total / n_cores)

    @staticmethod
    def _is_proc_alive(p) -> bool:
        """Return True if p is a running, non-zombie psutil.Process."""
        try:
            return p.is_running() and p.status() != "zombie"
        except Exception:
            return False

    def _draw_cpu_graph(self, canvas: tk.Canvas, w: int, h: int):
        """Render the Ollama CPU % sparkline into the header canvas.

        The graph is time-based (not sample-index-based) so gaps in polling
        show up as gaps in the line rather than misleadingly compressing history.
        Y-axis auto-scales to the local peak so small CPU spikes remain visible.
        The 'graph' tag on every drawn item lets _update_cpu_header wipe and
        redraw efficiently without destroying the canvas text items.
        """
        canvas.delete("graph")
        now = time.monotonic()
        entries = [(t, v) for t, v in self._cpu_history if v is not None]
        if len(entries) < 2:
            return
        peak_val = max(v for _, v in entries)
        max_val = max(peak_val, 10.0)   # always scale to at least 10% so flat lines look flat
        coords = []
        for t, val in entries:
            age = now - t
            x = w * (1.0 - age / HISTORY_SECS)   # older samples appear further left
            y = h - max(1, int(val / max_val * h))
            coords.append((x, y))
        poly = [(coords[0][0], h)] + coords + [(coords[-1][0], h)]
        canvas.create_polygon([c for pt in poly for c in pt],
                              fill=CPU_FILL, outline="", tags="graph")
        canvas.create_line([c for pt in coords for c in pt],
                           fill=CPU_LINE, width=1, tags="graph")
        canvas.tag_raise("hdr")   # keep text items on top of the graph fill

    def _update_cpu_header(self, any_ram: bool = False):
        """Redraw the CPU graph and update its header labels on every poll.

        The CPU graph is only shown when Ollama has (or had) a RAM-using model
        loaded — otherwise there's nothing interesting to track.

        'Freeze' behaviour: after 6 minutes of 0% CPU we consider the process
        fully idle and hide the graph entirely, resetting _had_ram_model so it
        reappears cleanly the next time a RAM model loads.

        Peak label: shown only when the current reading is less than half the
        session peak, so the user can see how active the process was recently
        without the peak number cluttering the display during bursts.
        """
        _mono = time.monotonic()
        idle_secs = (_mono - self._zero_since) if self._zero_since is not None else 0.0
        cpu_fully_hidden = idle_secs >= 2 * HISTORY_SECS  # 6 min of 0%

        if any_ram:
            self._had_ram_model = True
        if cpu_fully_hidden:
            self._had_ram_model = False

        if self.gpu_enabled and self._had_ram_model and not cpu_fully_hidden:
            cpu_now = self._cpu_history[-1][1] if self._cpu_history else None
            cpu_text = f"CPU {cpu_now:.0f}%" if cpu_now is not None else ""
            valid = [v for _, v in self._cpu_history if v is not None]
            peak = max(valid) if valid else 0
            show_peak = peak > 11.0 and (cpu_now is None or cpu_now < peak * 0.5)
            peak_text = f"{peak:.0f}%" if show_peak else ""
            hw = self.header_canvas.winfo_width()
            hh = self.header_canvas.winfo_height()
            if hw > 1 and hh > 1:
                self._draw_cpu_graph(self.header_canvas, hw, hh)
            self.header_canvas.itemconfig(self._hdr_cpu_id, text=cpu_text)
            self.header_canvas.itemconfig(self._hdr_peak_id, text=peak_text)
        else:
            self.header_canvas.delete("graph")
            self.header_canvas.itemconfig(self._hdr_cpu_id, text="")
            self.header_canvas.itemconfig(self._hdr_peak_id, text="")

    def _create_model_row(self, m: dict):
        name: str = m.get("name", "unknown")
        size: int = m.get("size", 0)
        size_vram: int = m.get("size_vram", 0)
        size_ram: int = max(0, size - size_vram)
        expires: str = m.get("expires_at", "")
        details: dict = m.get("details", {})
        param_size: str = details.get("parameter_size", "")
        quant: str = details.get("quantization_level", "")

        tag = ""
        base_name = name
        if ":" in name:
            base_name, tag = name.rsplit(":", 1)

        row = tk.Frame(self.content, bg=ROW_BG, padx=8, pady=6)
        row.pack(fill="x", pady=2)

        # ── "new" badge — shown for first 60 s after load ─────────────────────
        if name not in self._model_first_seen:
            # Pre-expire for models present at startup (first 2 polls)
            seen_at = time.monotonic() if self._poll_count > 2 else time.monotonic() - 61
            self._model_first_seen[name] = seen_at
        new_lbl = tk.Label(row, text="new", fg=GREEN, bg=ROW_BG,
                           font=("Segoe UI", 7), anchor="w")
        elapsed = time.monotonic() - self._model_first_seen[name]
        if elapsed < 60:
            new_lbl.pack(anchor="w", pady=(0, 1))

        # ── Row 1: name + quant (static for life of row) ─────────────────────
        r1 = tk.Frame(row, bg=ROW_BG)
        r1.pack(fill="x")

        name_text = base_name if (not tag or tag == "latest") else f"{base_name}:{tag}"
        tk.Label(r1, text=name_text, fg=FG, bg=ROW_BG,
                 font=("Segoe UI", 9, "bold"), anchor="w").pack(side="left")

        if param_size:
            tk.Label(r1, text=f"  {param_size}", fg=ACCENT, bg=ROW_BG,
                     font=("Segoe UI", 8)).pack(side="left")

        if quant:
            tk.Label(r1, text=quant, fg=DIM, bg=ROW_BG,
                     font=("Segoe UI", 8)).pack(side="right")

        # ── Combined graph canvas (VRAM + GPU%) ──────────────────────────────
        canvas = tk.Canvas(row, width=GRAPH_W, height=GRAPH_H,
                           bg=ROW_BG, highlightthickness=0, bd=0)
        canvas.pack(fill="x", pady=(4, 2))
        self._draw_combined_graph(canvas, self._vram_history.get(name, deque()),
                                  self._gpu_history, GRAPH_W, GRAPH_H)

        # ── Row 2: RAM label + expiry (dynamic) ───────────────────────────────
        r2 = tk.Frame(row, bg=ROW_BG)
        r2.pack(fill="x")

        ram_lbl = tk.Label(r2, text="", fg=BLUE, bg=ROW_BG, font=("Segoe UI", 8))
        exp_lbl = tk.Label(r2, text="", fg=DIM,  bg=ROW_BG, font=("Segoe UI", 8))
        exp_lbl.pack(side="right")
        self._apply_mem_label(ram_lbl, size_vram, size_ram, size)
        if expires:
            exp_lbl.config(text=f"⏱ {time_until(expires)}")

        self._model_widgets[name] = {
            "frame": row, "canvas": canvas,
            "ram_lbl": ram_lbl, "exp_lbl": exp_lbl,
            "new_lbl": new_lbl,
        }

    def _update_model_row(self, name: str, m: dict):
        w = self._model_widgets[name]
        size: int = m.get("size", 0)
        size_vram: int = m.get("size_vram", 0)
        size_ram: int = max(0, size - size_vram)
        expires: str = m.get("expires_at", "")

        self._draw_combined_graph(w["canvas"], self._vram_history.get(name, deque()),
                                  self._gpu_history, GRAPH_W, GRAPH_H)
        self._apply_mem_label(w["ram_lbl"], size_vram, size_ram, size)
        w["exp_lbl"].config(text=f"⏱ {time_until(expires)}" if expires else "")

        # Hide "new" badge after 60 s
        if "new_lbl" in w:
            elapsed = time.monotonic() - self._model_first_seen.get(name, 0)
            if elapsed >= 60:
                w["new_lbl"].pack_forget()

    @staticmethod
    def _apply_mem_label(lbl: tk.Label, size_vram: int, size_ram: int, size: int):
        if size_ram > 0:
            lbl.config(text=f"RAM {human_bytes(size_ram)}", fg=BLUE)
            lbl.pack(side="left")
        elif not size_vram and size:
            lbl.config(text=f"MEM {human_bytes(size)}", fg=DIM)
            lbl.pack(side="left")
        else:
            lbl.pack_forget()

    # ── Combined VRAM + GPU% history graph ───────────────────────────────────

    def _draw_combined_graph(self, canvas: tk.Canvas,
                             vram_history: deque, gpu_history: deque,
                             w: int, h: int):
        """Render overlaid VRAM (green) and GPU% (purple) sparklines in a model row.

        VRAM is auto-scaled to its own peak so the line uses the full canvas height
        regardless of absolute VRAM size.  GPU% is fixed at 0–100 so it is
        comparable across models.  Both series use the same x-axis: sample index
        with equal spacing (step = canvas_width / max_history).

        The grid lines (6 vertical, 4 horizontal) are drawn first so they sit
        behind the filled polygons.
        """
        canvas.delete("all")

        # Subtle grid for readability
        for i in range(1, 7):
            canvas.create_line(int(w * i / 6), 0, int(w * i / 6), h,
                               fill=BORDER_CLR, width=1)
        for i in range(1, 4):
            canvas.create_line(0, int(h * i / 4), w, int(h * i / 4),
                               fill=BORDER_CLR, width=1)

        step = w / (self.max_history - 1)

        # ── VRAM (green, auto-scaled) ─────────────────────────────────────────
        vpts = list(vram_history)
        if len(vpts) >= 2:
            max_val = max(vpts) or 1
            n = len(vpts)
            vcoords = []
            for i, val in enumerate(vpts):
                x = w - (n - 1 - i) * step   # oldest sample at left, newest at right
                y = h - max(1, int(val / max_val * h))
                vcoords.append((x, y))

            poly = [(vcoords[0][0], h)] + vcoords + [(vcoords[-1][0], h)]
            canvas.create_polygon([c for pt in poly for c in pt],
                                  fill=GREEN_FILL, outline="")
            canvas.create_line([c for pt in vcoords for c in pt],
                               fill=GREEN, width=1)
            # Current VRAM value as a small label in the top-left corner
            canvas.create_text(3, 3, text=human_bytes(vpts[-1]),
                               fill=ORANGE, anchor="nw", font=("Segoe UI", 7))

        # ── GPU% (purple, fixed 0-100) ────────────────────────────────────────
        gpts_raw = list(gpu_history)
        # Filter out None samples but preserve their position for correct x alignment
        gpts = [(i, v) for i, v in enumerate(gpts_raw) if v is not None]
        n_total = len(gpts_raw)

        if len(gpts) < 2:
            if not gpts:
                canvas.create_text(w - 3, h - 3, text="GPU% N/A",
                                   fill=DIM, anchor="se", font=("Segoe UI", 7))
        else:
            gcoords = []
            for idx, val in gpts:
                x = w - (n_total - 1 - idx) * step
                y = h - max(1, int(val / 100.0 * h))
                gcoords.append((x, y))

            poly = [(gcoords[0][0], h)] + gcoords + [(gcoords[-1][0], h)]
            canvas.create_polygon([c for pt in poly for c in pt],
                                  fill=GPU_FILL, outline="")
            canvas.create_line([c for pt in gcoords for c in pt],
                               fill=GPU_LINE, width=1)
            # Current GPU% value as a small label in the top-right corner
            canvas.create_text(w - 3, 3, text=f"{gpts[-1][1]:.0f}%",
                               fill=GPU_LINE, anchor="ne", font=("Segoe UI", 7))

    # ── Visibility ────────────────────────────────────────────────────────────

    def _hide(self):
        self.root.withdraw()
        self.visible = False

    def _show(self):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.visible = True

    def toggle_visibility(self):
        """Toggle window visibility — safe to call from the tray (non-main) thread.

        Schedules the actual hide/show on the Tk event loop via root.after(0).
        """
        if self.root:
            self.root.after(0, self._hide if self.visible else self._show)

    # ── Quit ─────────────────────────────────────────────────────────────────

    def _quit(self):
        if self.tray:
            self.tray.stop()
        if self.root:
            if hasattr(self, '_after_id'):
                self.root.after_cancel(self._after_id)
            self.root.after(0, self.root.destroy)

    # ── Tray icon ─────────────────────────────────────────────────────────────

    def _run_tray(self):
        """Build and run the system tray icon.  Blocks until tray.stop() is called.

        Runs in a daemon thread started by run().  All callbacks that touch Tkinter
        widgets must use root.after() to marshal back to the main thread.
        """
        # Build log-tail entries for every service that has a log_dir
        def _make_log_action(s):
            def action(icon, item):
                self._open_log_tail(s)
            return action

        log_items = []
        for svc in self.services:
            if svc.get("log_dir"):
                log_items.append(pystray.MenuItem(
                    f"Show {svc['label']} logs",
                    _make_log_action(dict(svc)),
                ))
        log_items.append(pystray.MenuItem(
            "Show Ollama logs",
            lambda icon, item: self._open_ollama_logs(),
        ))

        menu = pystray.Menu(
            pystray.MenuItem(
                "Show / Hide",
                lambda icon, item: self.toggle_visibility(),
                default=True,
            ),
            pystray.MenuItem(
                "Reset position",
                lambda icon, item: self.root.after(0, self._reset_position),
            ),
            pystray.Menu.SEPARATOR,
            *log_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Quit",
                lambda icon, item: self._quit(),
            ),
        )
        self.tray = pystray.Icon(
            "ollama_monitor",
            make_tray_icon(DIM),
            "Ollama Monitor",
            menu,
        )
        self.tray.run()

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self):
        """Start the tray icon thread then enter the Tk event loop (blocking)."""
        threading.Thread(target=self._run_tray, daemon=True).start()
        self.build_window()   # blocks until window is closed


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    import os
    env_url = os.environ.get("OLLAMA_SERVER_URL", DEFAULT_OLLAMA_URL)

    parser = argparse.ArgumentParser(description="Ollama Monitor overlay")
    parser.add_argument(
        "--url", default=env_url,
        help=f"Ollama base URL (default: OLLAMA_SERVER_URL env var, or {DEFAULT_OLLAMA_URL})"
    )
    parser.add_argument(
        "--poll", type=int, default=DEFAULT_POLL_SECS, metavar="SECS",
        help=f"Poll interval in seconds (default: {DEFAULT_POLL_SECS})"
    )
    parser.add_argument(
        "--no-gpu", "--no-cpu", "--no-xpu",
        action="store_true", dest="no_gpu",
        help="Disable GPU%%/CPU%% querying (use when monitoring a remote Ollama server)"
    )
    parser.add_argument(
        "--no-litellm", action="store_true",
        help="Disable LiteLLM service status indicator"
    )
    parser.add_argument(
        "--no-websearch", action="store_true",
        help="Disable WebSearch MCP service status indicator"
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="Remote/LAN mode: disables all local-only features "
             "(service activity, client tracking, GPU%%/CPU%%)"
    )
    args = parser.parse_args()

    disabled = set()
    if args.no_litellm:
        disabled.add("litellm")
    if args.no_websearch:
        disabled.add("websearch")
    services = [s for s in ALL_SERVICES if s["key"] not in disabled]

    app = OllamaOverlay(
        ollama_url=args.url,
        poll_secs=max(1, args.poll),
        gpu_enabled=not args.no_gpu,
        services=services,
        remote=args.remote,
    )
    app.run()


if __name__ == "__main__":
    main()
