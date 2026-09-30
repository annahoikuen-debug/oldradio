@echo off
chcp 65001 > nul

REM Retro Radio Time Machine Launcher (FastAPI + Pure SPA)
REM ダブルクリックでアプリケーションを起動します。
REM 注意: このファイルは UTF-8(BOMなし) で保存してください。
REM       先頭の `chcp 65001` により下の日本語が正しく表示されます。
REM Streamlit は廃止済みです。起動は uvicorn のみです。

setlocal EnableDelayedExpansion

cd /d "%~dp0"

title Retro Radio Time Machine Launcher
color 0A

if not defined PORT set "PORT=8501"
if not defined HOST set "HOST=0.0.0.0"
REM 開発中に自動リロードを使う場合は実行前に RELOAD=1 を設定してください。
if not defined RELOAD set "RELOAD=0"

echo.
echo ==========================================
echo   Retro Radio Time Machine v2.0
echo   FastAPI + Pure SPA
echo ==========================================
echo.

REM `.env` は pydantic-settings（model_config の env_file=".env"）が読み込むため、
REM       ここでは手動で環境変数へ変換しません（旧実装のクォート誤爆の回避）。
REM       キーの有無だけを確認します。
set "HAS_GEMINI_KEY=0"
if defined RETRO_RADIO_GEMINI_API_KEY set "HAS_GEMINI_KEY=1"
if defined GEMINI_API_KEY set "HAS_GEMINI_KEY=1"
if exist ".env" (
    findstr /B /C:"RETRO_RADIO_GEMINI_API_KEY=" .env > nul 2>&1
    if not errorlevel 1 (
        for /f "usebackq tokens=2 delims==" %%a in (".env") do (
            if /i "%%a"=="RETRO_RADIO_GEMINI_API_KEY" (
                if not "!%%a!"=="" set "HAS_GEMINI_KEY=1"
            )
        )
    )
)

if !HAS_GEMINI_KEY!==0 (
    echo [情報] Gemini API キーが見つかりませんでした。
    echo.
    echo [入力] AI原稿生成を有効にするには Gemini API キーを入力してください。
    echo [情報] Enter キーを押すとフォールバックモードで起動します。
    echo.
    set /p GEMINI_API_KEY="API Key (Enter でスキップ): "
    if defined GEMINI_API_KEY (
        echo.
        set /p SAVE_KEY="この API キーを .env に保存しますか? (Y/N): "
        if /i "!SAVE_KEY!"=="Y" call :save_env RETRO_RADIO_GEMINI_API_KEY "!GEMINI_API_KEY!"
    ) else (
        echo [警告] API キー未設定。フォールバックモードで起動します。
        echo [情報] 無料キーの取得: https://aistudio.google.com/app/apikey
    )
) else (
    echo [OK] Gemini API キーが設定されています
)

echo.
echo ------------------------------------------

REM Python の確認（py ランチャーを優先）
set "PYTHON_OK=0"
where py > nul 2>&1
if not errorlevel 1 (
    py -3 -c "import sys" > nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_OK=1"
        set "PYTHON_CMD=py -3"
    )
)

if "!PYTHON_OK!"=="0" (
    where python > nul 2>&1
    if not errorlevel 1 (
        python -c "import sys" > nul 2>&1
        if not errorlevel 1 (
            set "PYTHON_OK=1"
            set "PYTHON_CMD=python"
        )
    )
)

if "!PYTHON_OK!"=="0" goto :no_python

echo [OK] Python 環境を検出しました: !PYTHON_CMD!
echo.
echo [情報] 実行時依存関係を確認中...
!PYTHON_CMD! -c "import fastapi, uvicorn" > nul 2>&1
if errorlevel 1 (
    echo [情報] 必要に応じて依存関係をインストール中...
    !PYTHON_CMD! -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [エラー] 依存関係のインストールに失敗しました。
        goto :failed
    )
)

echo [OK] すべての依存関係の準備が完了しました。
echo.
echo [開始] Retro Radio サーバーを起動中（uvicorn）...
echo [情報] ブラウザで開いてください: http://localhost:8501
echo [情報] ヘルスチェック:           http://localhost:8501/health
echo.
echo [情報] Ctrl+C を押すとサーバーを停止します
echo.

start "" cmd /c "timeout /t 3 > nul && start http://localhost:8501"

if "!RELOAD!"=="1" (
    !PYTHON_CMD! -m uvicorn retro_radio.server:app --host !HOST! --port !PORT! --reload
) else (
    !PYTHON_CMD! -m uvicorn retro_radio.server:app --host !HOST! --port !PORT!
)
goto :eof

:save_env
REM %1 = キー名, %2 = 値。既存の .env を上書き（上書き切り）しないでください。
if not exist ".env" (
    > .env echo %~1=%~2
    goto :eof
)
findstr /B /C:"%~1=" .env > nul 2>&1
if not errorlevel 1 (
    echo [情報] .env に %~1 が既にあります。値の変更は手動で行ってください。
    goto :eof
)
>> .env echo %~1=%~2
echo [OK] %~1 を .env に保存しました
goto :eof

:no_python
echo [エラー] Python 環境が見つかりません。Python 3.11+ をインストールしてください。
echo [情報] ダウンロード: https://www.python.org/downloads/
echo.
pause
exit /b 1

:failed
pause
exit /b 1
