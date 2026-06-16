@echo off
cd /d "%~dp0"
echo Building Ollama Monitor...
call .venv\Scripts\activate.bat

echo Embedding web_ui.html...
python embed_html.py
if errorlevel 1 (
    echo.
    echo EMBED FAILED
    pause
    exit /b 1
)

pyinstaller "Ollama Monitor.spec" --noconfirm --distpath "." --workpath "%TEMP%\pyibuild-ollama-monitor"
set BUILD_ERR=%errorlevel%

echo Restoring ollama_monitor.py...
copy /y ollama_monitor.py.bak ollama_monitor.py >nul
del /f /q ollama_monitor.py.bak 2>nul

if %BUILD_ERR% neq 0 (
    echo.
    echo BUILD FAILED - see errors above
    pause
) else (
    echo.
    echo Build complete: Ollama Monitor.exe
)
