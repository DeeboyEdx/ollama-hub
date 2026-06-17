# Ollama Monitor

A lightweight, always-on-top Windows overlay that shows which Ollama models are currently loaded and their resource usage — no browser tab, no terminal, no clutter.

---

## What it shows

**Header bar** (always visible):

| Element | Description |
|---|---|
| **Status dot** | 🟢 model loaded · ⚫ idle · 🔴 offline |
| **CPU graph** (teal) | Ollama process CPU% history, last 3 minutes, auto-scaled (10% floor) |
| **CPU x%** | Current Ollama CPU utilisation |
| **Peak %** | Peak CPU% in the current window (shown only when current has dropped well below the peak) |
| **Clock** | Current time |

The CPU graph and label fade out gracefully when Ollama has been idle: the line scrolls off the left edge over 3 minutes, then the label hides after 6 minutes total. Both reappear instantly when activity resumes.

**Per-model rows** (one per loaded model):

| Element | Description |
|---|---|
| **Model name** | Base name, with tag if not `:latest` |
| **8.0B** (blue) | Parameter count from Ollama API |
| **Q4_K_M** (dim) | Quantization level |
| **Graph — green** | VRAM usage history, auto-scaled to peak, last 3 minutes |
| **Graph — purple** | GPU core utilisation % history (0–100%), overlaid on same canvas |
| **VRAM GB** (orange, top-left of graph) | Current VRAM in use |
| **XX%** (purple, top-right of graph) | Current GPU utilisation |
| **RAM …** (blue, below graph) | CPU RAM used, if model is split across GPU+CPU |
| **⏱ 4m 32s** | Time until Ollama auto-unloads this model |

---

## Requirements

- Windows
- Ollama running locally (or remotely — see `--url` / `OLLAMA_SERVER_URL`)
- An NVIDIA GPU for the GPU% graph (falls back to `nvidia-smi`; shows "GPU% N/A" on non-NVIDIA)

---

## Running

### Standalone executable (recommended)

Double-click **`Ollama Monitor.exe`** — no Python or dependencies needed.

From a terminal with arguments:
```bat
"Ollama Monitor.exe" --url http://192.168.1.50:11434
"Ollama Monitor.exe" --poll 5
"Ollama Monitor.exe" --no-gpu
```

### Auto-start on login

A shortcut to `Ollama Monitor.exe` in the Startup folder will launch it silently on login:
```
%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Ollama Monitor.lnk
```

### From source (dev/debug)

```bat
cd ModelMonitor
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt

:: Console visible — useful for debugging
run.bat

:: Or directly
python ollama_monitor.py --url http://192.168.1.50:11434 --poll 5 --no-gpu
```

---

## Arguments

| Argument | Default | Description |
|---|---|---|
| `--url URL` | `http://localhost:11434` | Ollama server URL |
| `--poll SECS` | `3` local / `5` remote | Polling interval in seconds (min 1) |
| `--no-gpu` | off | Disable GPU% and CPU% monitoring |
| `--no-cpu` | off | Alias for `--no-gpu` |
| `--no-xpu` | off | Alias for `--no-gpu` |
| `--remote` | off | Master toggle for remote servers: disables GPU/CPU graphs, TCP client tracking, log monitoring, and the web server. |
| `--no-webserve` | off | Disable the built-in web dashboard (on by default) |
| `--port PORT` | `11435` | Web dashboard port |
| `--no-litellm` | off | Hide LiteLLM service indicator |
| `--litellm-logs DIR` | *(your install path)* | LiteLLM log directory. If the directory doesn't exist, the indicator is hidden automatically. |
| `--no-websearch` | off | Hide WebSearch MCP service indicator |
| `--websearch-mcp-logs DIR` | *(your install path)* | WebSearch MCP log directory. If the directory doesn't exist, the indicator is hidden automatically. |

`--url` falls back to the `OLLAMA_SERVER_URL` environment variable if set.

> **Note:** GPU% and CPU% are always read from the **local machine** running the overlay, not the Ollama server. If you point `--url` at a remote machine, use `--remote` to suppress local-only features — otherwise the graphs will reflect your local hardware, not the server's.

---

## Web Dashboard

The web dashboard runs on by default on port 11435. Open **`http://<your-pc-ip>:11435/`** in any browser (desktop or phone).

```bat
"Ollama Monitor.exe"               :: web server on by default
"Ollama Monitor.exe" --port 8080   :: custom port
"Ollama Monitor.exe" --no-webserve :: disable it
```

The dashboard shows the same model cards, VRAM/GPU sparklines, CPU histogram, and service status dots as the overlay — auto-refreshing every 1 s while a model is active, 5 s while loaded-idle, or 15 s when Ollama is quiet.

The overlay shows a **●** dot in the services footer:
- **Right-click** → Open web dashboard / Copy web address (uses your LAN IP)
- **Left-click** → toggle server on/off

> **Firewall:** Windows may prompt to allow `Ollama Monitor.exe` through the firewall on first launch. Allow it for private networks to enable LAN access.

---

## Usage

- **Drag** the overlay anywhere by clicking and dragging
- **Right-click** the overlay for Hide / Quit
- **System tray icon** (bottom-right) — double-click to show/hide, right-click for menu

---

## Files

```
├── OllamaMonitor.exe   # Standalone executable — run this
├── OllamaMonitor.spec  # PyInstaller recipe
├── ollama_monitor.py   # Source code
├── web_ui.html         # Web dashboard (served live in dev; embedded in exe at build time)
├── embed_html.py       # Build helper: embeds web_ui.html into ollama_monitor.py
├── requirements.txt    # Python dependencies (for dev/rebuild)
├── build.bat           # Double-click to rebuild the exe
├── run.bat             # Dev launcher (console window visible)
└── README.md           # This file
```

To rebuild the exe after modifying `ollama_monitor.py`, just double-click **`build.bat`**.
Build intermediates go to `%TEMP%\pyibuild-ollama-monitor` to avoid local directory clutter or OneDrive/cloud sync churn.

---

## Dependencies (source / rebuild only)

| Package | Purpose |
|---|---|
| `requests` | Calls Ollama REST API (`/api/ps`) |
| `Pillow` | Draws the system tray icon |
| `pystray` | System tray integration |
| `psutil` | Ollama process-specific CPU% monitoring |
| `nvidia-ml-py` | GPU utilisation via NVML (fast, no subprocess) |

GPU% falls back to `nvidia-smi` subprocess if NVML init fails, and degrades gracefully to "GPU% N/A" if neither is available.
