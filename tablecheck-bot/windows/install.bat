@echo off
REM 一次性安裝:建立虛擬環境、裝套件、裝瀏覽器
cd /d "%~dp0.."
echo === 建立虛擬環境 .venv ===
python -m venv .venv
call .venv\Scripts\activate.bat
echo === 安裝 Python 套件 ===
python -m pip install --upgrade pip
pip install -r requirements.txt
echo === 安裝 Chromium 瀏覽器 ===
python -m playwright install chromium
echo.
echo 安裝完成!
echo 接下來: 把 config.example.yaml 複製成 config.yaml 並編輯內容。
pause
