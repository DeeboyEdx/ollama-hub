#!/usr/bin/env python3
"""
Ollama Monitor — lightweight always-on-top overlay + system tray icon.
Shows which Ollama models are currently loaded and their VRAM/RAM usage.

Usage: python ollama_monitor.py [--url http://localhost:11434]
"""

import re
import sys
import time
import socket
import threading
import tkinter as tk
import argparse
from collections import deque
from datetime import datetime, timezone

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
ALL_SERVICES = [
    {"key": "litellm",   "label": "LiteLLM",   "host": "localhost", "port": 4000},
    {"key": "websearch", "label": "WebSearch",  "host": "localhost", "port": 8765},
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
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def time_until(iso_string: str) -> str:
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
    """Returns (models_list, error_str). models_list is None on error."""
    try:
        r = requests.get(f"{base_url}/api/ps", timeout=2)
        r.raise_for_status()
        return r.json().get("models", []), None
    except requests.exceptions.ConnectionError:
        return None, "offline"
    except Exception as e:
        return None, str(e)


def check_tcp_port(host: str, port: int, timeout: float = 0.3) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ── GPU utilisation (NVML / nvidia-smi fallback) ──────────────────────────────

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

try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _PSUTIL_OK = False


def fetch_gpu_pct() -> float | None:
    """Return GPU core utilisation % (0-100), or None if unavailable."""
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
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([6, 6, 58, 58], fill=color)
    return img


# ── Overlay window ────────────────────────────────────────────────────────────

class OllamaOverlay:
    def __init__(self, ollama_url: str, poll_secs: int = DEFAULT_POLL_SECS,
                 gpu_enabled: bool = True, services: list | None = None):
        self.ollama_url      = ollama_url
        self.poll_interval_ms = poll_secs * 1000
        self.max_history     = max(2, HISTORY_SECS * 1000 // self.poll_interval_ms)
        self.gpu_enabled     = gpu_enabled
        self.services        = services if services is not None else list(ALL_SERVICES)
        self.visible = True
        self.root: tk.Tk | None = None
        self.tray: pystray.Icon | None = None
        self._drag_x = self._drag_y = 0
        self._model_widgets: dict[str, dict] = {}
        self._last_status = ""   # "online" | "idle" | "offline"
        self._vram_history: dict[str, deque] = {}
        self._gpu_history:  deque = deque(maxlen=self.max_history)
        self._cpu_history:  deque = deque()     # (monotonic_ts, pct) tuples; no fixed maxlen
        self._zero_since:   float | None = None  # monotonic time CPU first hit 0%
        self._had_ram_model: bool = False        # True once a RAM-using model has been seen
        self._model_first_seen: dict[str, float] = {}  # name → monotonic load time
        self._service_up:    dict[str, bool] = {s["key"]: False for s in self.services}
        self._svc_dot_labels: dict[str, tk.Label] = {}
        self._ollama_proc         = None
        self._after_id            = None

    # ── Window construction ───────────────────────────────────────────────────

    def build_window(self):
        self.root = tk.Tk()
        self.root.title("Ollama Monitor")
        self.root.overrideredirect(True)          # no title bar
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.93)
        self.root.configure(bg=BG)
        self.root.resizable(False, False)

        # Position: top-right, small gap from edge; constrain width
        sw = self.root.winfo_screenwidth()
        x = sw - WINDOW_W - 16
        self.root.geometry(f"+{x}+10")
        self.root.minsize(WINDOW_W, 1)
        self.root.maxsize(WINDOW_W, self.root.winfo_screenheight())

        # Drag
        self.root.bind("<ButtonPress-1>", self._on_drag_start)
        self.root.bind("<B1-Motion>", self._on_drag_move)
        # Right-click menu
        self.root.bind("<ButtonPress-3>", self._show_context_menu)

        self._build_header()
        self._build_content_area()
        self._build_services_footer()

        self.root.after(100, self._poll)
        self.root.mainloop()

    def _build_header(self):
        tk.Frame(self.root, bg=BORDER_CLR, height=1).pack(fill="x")

        self.header_canvas = tk.Canvas(
            self.root, bg=BG, highlightthickness=0, bd=0, height=HEADER_H
        )
        self.header_canvas.pack(fill="x")

        cy = HEADER_H // 2   # vertical centre

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
        self.content = tk.Frame(self.root, bg=BG, padx=8, pady=6)
        self.content.pack(fill="both", expand=True)

        self.placeholder = tk.Label(
            self.content, text="Connecting…", fg=DIM, bg=BG,
            font=("Segoe UI", 9), pady=10
        )
        self.placeholder.pack()

    def _build_services_footer(self):
        if not self.services:
            return
        tk.Frame(self.root, bg=BORDER_CLR, height=1).pack(fill="x")
        footer = tk.Frame(self.root, bg=BG, padx=8, pady=4)
        footer.pack(fill="x")
        for svc in self.services:
            col = tk.Frame(footer, bg=BG)
            col.pack(side="left", expand=True, fill="x")
            dot = tk.Label(col, text="●", fg=DIM, bg=BG, font=("Segoe UI", 8))
            dot.pack(side="left")
            tk.Label(col, text=f" {svc['label']}", fg=DIM, bg=BG,
                     font=("Segoe UI", 8)).pack(side="left")
            self._svc_dot_labels[svc["key"]] = dot

    def _update_services_footer(self):
        for svc in self.services:
            up = self._service_up.get(svc["key"], False)
            dot = self._svc_dot_labels.get(svc["key"])
            if dot:
                dot.config(fg=GREEN if up else RED)

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
        menu.add_command(label="Quit", command=self._quit)
        menu.tk_popup(event.x_root, event.y_root)

    def _reset_position(self):
        sw = self.root.winfo_screenwidth()
        self.root.geometry(f"+{sw - WINDOW_W - 16}+10")

    # ── Poll + UI update ──────────────────────────────────────────────────────

    def _poll(self):
        models, error = fetch_models(self.ollama_url)
        gpu_pct = fetch_gpu_pct() if self.gpu_enabled else None
        self._gpu_history.append(gpu_pct)
        cpu_pct = self._get_ollama_cpu_pct() if self.gpu_enabled else None
        _now = time.monotonic()
        if cpu_pct is not None:
            if cpu_pct >= 0.5:   # treat < 0.5% (displays as "0%") as idle
                self._zero_since = None
                self._cpu_history.append((_now, cpu_pct))
            else:
                if self._zero_since is None:
                    self._zero_since = _now
                if _now - self._zero_since < HISTORY_SECS:
                    self._cpu_history.append((_now, 0.0))
                # else: >3 min of 0% — freeze, let old entries age out
        # Prune entries that have scrolled past the window
        while self._cpu_history and _now - self._cpu_history[0][0] > HISTORY_SECS:
            self._cpu_history.popleft()
        for svc in self.services:
            self._service_up[svc["key"]] = check_tcp_port(svc["host"], svc["port"])
        self._update_services_footer()
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

        if not models:
            self._set_status("idle")
            self._clear_all_model_rows()
            self.placeholder.config(text="No models loaded", fg=DIM)
            self.placeholder.pack()
            self._update_cpu_header(any_ram=False)
            return

        self._set_status("online")
        self.placeholder.pack_forget()

        any_ram = any(
            max(0, m.get("size", 0) - m.get("size_vram", 0)) > 0 for m in models
        )
        self._update_cpu_header(any_ram=any_ram)

        current_names = {m.get("name", "unknown") for m in models}

        # Remove rows for models that are no longer loaded
        for name in list(self._model_widgets.keys()):
            if name not in current_names:
                self._remove_model_row(name)

        # Update existing rows / create new ones
        for m in models:
            name = m.get("name", "unknown")
            if name in self._model_widgets:
                self._update_model_row(name, m)
            else:
                self._create_model_row(m)

    def _clear_all_model_rows(self):
        for name in list(self._model_widgets.keys()):
            self._remove_model_row(name)

    def _remove_model_row(self, name: str):
        if name in self._model_widgets:
            self._model_widgets[name]["frame"].destroy()
            del self._model_widgets[name]
        self._model_first_seen.pop(name, None)

    def _set_status(self, status: str):
        if status == self._last_status:
            return
        self._last_status = status
        color_map = {"online": GREEN, "idle": DIM, "offline": RED}
        color = color_map.get(status, DIM)
        self.header_canvas.itemconfig(self._hdr_dot_id, fill=color)
        # Update tray icon colour too
        if self.tray:
            self.tray.icon = make_tray_icon(color)

    def _get_ollama_cpu_pct(self) -> float | None:
        """Return summed CPU % for all ollama* processes, normalised to 0-100."""
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
        try:
            return p.is_running() and p.status() != "zombie"
        except Exception:
            return False

    def _draw_cpu_graph(self, canvas: tk.Canvas, w: int, h: int):
        canvas.delete("graph")
        now = time.monotonic()
        entries = [(t, v) for t, v in self._cpu_history if v is not None]
        if len(entries) < 2:
            return
        peak_val = max(v for _, v in entries)
        max_val = max(peak_val, 10.0)
        coords = []
        for t, val in entries:
            age = now - t
            x = w * (1.0 - age / HISTORY_SECS)
            y = h - max(1, int(val / max_val * h))
            coords.append((x, y))
        poly = [(coords[0][0], h)] + coords + [(coords[-1][0], h)]
        canvas.create_polygon([c for pt in poly for c in pt],
                              fill=CPU_FILL, outline="", tags="graph")
        canvas.create_line([c for pt in coords for c in pt],
                           fill=CPU_LINE, width=1, tags="graph")
        canvas.tag_raise("hdr")

    def _update_cpu_header(self, any_ram: bool = False):
        """Redraw CPU graph and update header labels. Called every poll."""
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
            self._model_first_seen[name] = time.monotonic()
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
        canvas.delete("all")

        # Grid
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
                x = w - (n - 1 - i) * step
                y = h - max(1, int(val / max_val * h))
                vcoords.append((x, y))

            poly = [(vcoords[0][0], h)] + vcoords + [(vcoords[-1][0], h)]
            canvas.create_polygon([c for pt in poly for c in pt],
                                  fill=GREEN_FILL, outline="")
            canvas.create_line([c for pt in vcoords for c in pt],
                               fill=GREEN, width=1)
            canvas.create_text(3, 3, text=human_bytes(vpts[-1]),
                               fill=ORANGE, anchor="nw", font=("Segoe UI", 7))

        # ── GPU% (purple, fixed 0-100) ────────────────────────────────────────
        gpts_raw = list(gpu_history)
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
        """Called from tray (non-main thread) — schedule on Tk event loop."""
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
    )
    app.run()


if __name__ == "__main__":
    main()
