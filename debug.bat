@echo off
chcp 65001 > nul

REM Retro Radio Time Machine - debug launcher (FastAPI + Pure SPA)
REM 開発者向け: 起動前チェック + リロード有効 + デバッグログ表示。
REM 注意: このファイルは UTF-8(BOMなし) で保存してください。
REM Streamlit は廃止済みです。起動は uvicorn のみです。

setlocal EnableDelayedExpansion

cd /d "%~dp0"

if not defined PORT set "PORT=8501"
if not defined HOST set "HOST=127.0.0.1"

echo ==========================================
echo   Retro Radio Time Machine - debug mode
echo ==========================================
echo.

REM Python の検出
set "PYTHON_CMD="
where py > nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" > nul 2>&1
    if not errorlevel 1 set "PYTHON_CMD=py -3"
)
if "!PYTHON_CMD!"=="" (
    where python > nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" > nul 2>&1
        if not errorlevel 1 set "PYTHON_CMD=python"
    )
)

if "!PYTHON_CMD!"=="" (
    echo [ERROR] Python 3.11+ が見つかりません。
    echo [INFO] https://www.python.org/downloads/
    pause
    exit /b 1
)

echo [情報] Python: !PYTHON_CMD!
!PYTHON_CMD! --version

echo.
echo [情報] 依存関係を確認中...
!PYTHON_CMD! -c "import fastapi, uvicorn" > nul 2>&1
if errorlevel 1 (
    echo [情報] 依存関係をインストール中...
    !PYTHON_CMD! -m pip install -r requirements.txt
)

echo.
echo [情報] 設定ファイルを検証中...
!PYTHON_CMD! -c "from retro_radio.config import get_settings; s=get_settings(); print('[OK] app_version=', s.app_version, '| gemini_model=', s.gemini_model, '| port=', s.port, '| api_key=', bool(s.gemini_api_key))"
if errorlevel 1 echo [WARNING] 設定の読み込みに失敗しました（上記エラー参照）

echo.
echo [開始] uvicorn を起動します（host=!HOST! port=!PORT!, --reload 有効）
echo [情報] ヘルスチェック: http://localhost:!PORT!/health
echo [情報] Ctrl+C で停止
echo.

!PYTHON_CMD! -m uvicorn retro_radio.server:app --host !HOST! --port !PORT! --reload --log-level debug
goto :eof
