@echo off
cd /d "%~dp0"
echo Building Ollama Monitor...
call .venv\Scripts\activate.bat
pyinstaller "Ollama Monitor.spec" --noconfirm --distpath "." --workpath "%TEMP%\pyibuild-ollama-monitor"
if errorlevel 1 (
    echo.
    echo BUILD FAILED - see errors above
    pause
) else (
    echo.
    echo Build complete: Ollama Monitor.exe
)
