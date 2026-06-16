"""Embed web_ui.html into ollama_monitor.py for PyInstaller build.

Replaces the _WEB_UI_HTML placeholder line with the actual HTML content,
saves a backup as ollama_monitor.py.bak, then overwrites ollama_monitor.py.
The build.bat calls this before PyInstaller, then restores the backup after.
"""
import os
import sys

MARKER = '_WEB_UI_HTML: str = ""  # @@WEB_UI_HTML@@'
SRC    = "ollama_monitor.py"
BAK    = "ollama_monitor.py.bak"
HTML   = "web_ui.html"

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)

if not os.path.exists(HTML):
    print(f"ERROR: {HTML} not found", file=sys.stderr)
    sys.exit(1)

with open(HTML, encoding="utf-8") as f:
    html_content = f.read()

# Escape any triple-quote sequences inside the HTML (unlikely but safe)
html_safe = html_content.replace('"""', '" "" "')

with open(SRC, encoding="utf-8") as f:
    src = f.read()

if MARKER not in src:
    print(f"ERROR: marker not found in {SRC}", file=sys.stderr)
    sys.exit(1)

# Save backup
with open(BAK, "w", encoding="utf-8") as f:
    f.write(src)

replacement = f'_WEB_UI_HTML: str = """\\\n{html_safe}\n"""  # embedded'
patched = src.replace(MARKER, replacement)

with open(SRC, "w", encoding="utf-8") as f:
    f.write(patched)

print(f"  Embedded {len(html_content):,} bytes from {HTML} into {SRC}")
