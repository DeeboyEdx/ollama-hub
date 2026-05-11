@echo off
:: Ollama Monitor — dev/debug launcher (console stays open on error)
cd /d "%~dp0"

set PYTHONEXE=%~dp0.venv\Scripts\python.exe

if not exist "%PYTHONEXE%" (
    echo ERROR: .venv not found. Run: python -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
    pause
    exit /b 1
)

"%PYTHONEXE%" ollama_monitor.py %*
if %errorlevel% neq 0 pause
