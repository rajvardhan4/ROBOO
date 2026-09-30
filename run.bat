@echo off
cd /d "%~dp0"
python -c "import webview, edge_tts, speech_recognition, anthropic" 2>nul || (
  echo Installing dependencies...
  python -m pip install -r requirements.txt
)
start "" pythonw main.py
