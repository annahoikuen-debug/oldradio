# T2: 非同期処理・テスト基盤・CI/CD実装計画書
## 概要
改善案6〜8を実装し、応答性・品質保証・自動化基盤を構築する

---

## Step 1: 非同期実行基盤実装 (utils/async_runner.py)
**対象ファイル**: `retro_radio/utils/async_runner.py` 新規
**作業内容**: ThreadPoolExecutor + asyncio で同期関数を非同期実行
```python
import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from typing import Callable, TypeVar, Any
import streamlit as st

F = TypeVar('F', bound=Callable[..., Any])
_executor: ThreadPoolExecutor | None = None

def get_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=3)
    return _executor

def shutdown_executor() -> None:
    global _executor
    if _executor:
        _executor.shutdown(wait=True)
        _executor = None

def run_in_executor(func: F, *args, **kwargs) -> asyncio.Future:
    """同期関数をスレッドプールで非同期実行"""
    loop = asyncio.get_event_loop()
    executor = get_executor()
    return loop.run_in_executor(executor, lambda: func(*args, **kwargs))

class AsyncProgress:
    """非同期対応プログレスバー"""
    def __init__(self):
        self.progress_bar = st.progress(0)
        self.status_text = st.empty()
        self._current = 0
    
    def update(self, percent: int, message: str = "") -> None:
        self._current = percent
        self.progress_bar.progress(percent)
        if message:
            self.status_text.text(message)
    
    def complete(self, message: str = "完了") -> None:
        self.update(100, f"✅ {message}")

def async_step(progress: AsyncProgress, percent: int, message: str):
    """デコレータ: 非同期ステップ実行・進捗更新"""
    def decorator(func: F) -> F:
        @wraps(func)
        async def wrapper(*args, **kwargs):
            progress.update(percent, message)
            result = await run_in_executor(func, *args, **kwargs)
            return result
        return wrapper
    return decorator
```
**テスト**: `tests/test_async_runner.py` - 実行・進捗更新・例外伝播・executor shutdown確認
**完了基準**: 同期関数が非同期で実行可能、進捗バー正常更新、リソースリークなし

---

## Step 2: 非同期版生成パイプライン実装 (core/pipeline.py)
**対象ファイル**: `retro_radio/core/pipeline.py` 新規
**作業内容**: 3ステップ（原稿・楽曲・音声）を並列化可能なパイプライン化
```python
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional
from .script_generator import generate_radio_script
from .music_search import search_itunes_songs, select_song
from .tts import text_to_speech, generate_error_audio
from .fallback import get_fallback_song
from ..utils.errors import handle_error
from ..utils.async_runner import AsyncProgress, run_in_executor
from ..config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

@dataclass
class GenerationResult:
    script: str
    song_title: str
    artist_name: str
    preview_url: Optional[str]
    audio_path: Optional[str]
    use_fallback_song: bool
    errors: list[str]

async def generate_all_async(
    year: int, month: int, day: int,
    progress: AsyncProgress | None = None
) -> GenerationResult:
    """全生成ステップを非同期実行（順次だがUIブロックしない）"""
    errors = []
    prog = progress or AsyncProgress()
    
    # Step 1: 原稿生成
    prog.update(0, "📝 ラジオ原稿を作成中...")
    try:
        script = await run_in_executor(generate_radio_script, year, month, day)
    except Exception as e:
        logger.error(f"Script generation failed: {e}")
        handle_error(e, "ScriptGeneration")
        from .fallback import generate_fallback_script
        script = generate_fallback_script(year, month, day)
        errors.append("script_fallback")
    prog.update(33)
    
    # Step 2: 楽曲検索
    prog.update(33, "🎵 その時のヒット曲を探しています...")
    try:
        songs = await run_in_executor(search_itunes_songs, year)
        song_title, artist_name, preview_url, use_fallback = select_song(year, songs)
    except Exception as e:
        logger.error(f"Music search failed: {e}")
        handle_error(e, "MusicSearch")
        song_title, artist_name = get_fallback_song(year)
        preview_url, use_fallback = None, True
        errors.append("music_fallback")
    prog.update(66)
    
    # Step 3: 音声合成
    prog.update(66, "🎙️ 音声を合成しています...")
    try:
        audio_path = await run_in_executor(text_to_speech, script)
    except Exception as e:
        logger.error(f"TTS failed: {e}")
        handle_error(e, "TTS")
        audio_path = None
        errors.append("tts_failed")
    prog.complete("生成完了")
    
    return GenerationResult(
        script=script,
        song_title=song_title,
        artist_name=artist_name,
        preview_url=preview_url,
        audio_path=audio_path,
        use_fallback_song=use_fallback,
        errors=errors
    )

# 将来用: 並列実行版（原稿・楽曲検索を同時実行）
async def generate_all_parallel(
    year: int, month: int, day: int,
    progress: AsyncProgress | None = None
) -> GenerationResult:
    """原稿生成と楽曲検索を並列実行（高速化）"""
    errors = []
    prog = progress or AsyncProgress()
    
    prog.update(0, "📝🎵 原稿作成と楽曲検索を並行実行中...")
    script_task = run_in_executor(generate_radio_script, year, month, day)
    songs_task = run_in_executor(search_itunes_songs, year)
    
    script, songs = await asyncio.gather(script_task, songs_task, return_exceptions=True)
    
    if isinstance(script, Exception):
        handle_error(script, "ScriptGeneration")
        from .fallback import generate_fallback_script
        script = generate_fallback_script(year, month, day)
        errors.append("script_fallback")
    
    if isinstance(songs, Exception):
        handle_error(songs, "MusicSearch")
        songs = []
    
    song_title, artist_name, preview_url, use_fallback = select_song(year, songs)
    prog.update(50)
    
    prog.update(50, "🎙️ 音声を合成しています...")
    audio_path = await run_in_executor(text_to_speech, script)
    prog.complete("生成完了")
    
    return GenerationResult(
        script=script, song_title=song_title, artist_name=artist_name,
        preview_url=preview_url, audio_path=audio_path,
        use_fallback_song=use_fallback, errors=errors
    )
```
**テスト**: `tests/test_pipeline.py` - 順次/並列版両方・エラー時フォールバック・進捗コールバック確認
**完了基準**: 同期版と同等結果、UIブロックなし、進捗バー滑らか更新

---

## Step 3: メインアプリ非同期化統合 (main.py 更新)
**対象ファイル**: `retro_radio/main.py` 更新
**作業内容**: 非同期パイプライン呼び出し・イベントループ管理
```python
# 追加インポート
import asyncio
from retro_radio.core.pipeline import generate_all_async, generate_all_parallel
from retro_radio.utils.async_runner import AsyncProgress, shutdown_executor
from retro_radio.services.history_service import save_generation_result

# ボタンクリックハンドラ内で非同期実行
if st.button("📻 ラジオを再生する", type="primary"):
    # ... 既存の再生フラグ処理 ...
    
    if not settings.gemini_api_key:
        st.error("Gemini APIキーが設定されていません...")
        st.stop()
    
    # 非同期実行ヘルパー
    async def run_generation():
        progress = AsyncProgress()
        # 並列版使用（Step 2で実装）
        return await generate_all_parallel(selected_year, current_month, current_day, progress)
    
    # Streamlitで非同期実行
    try:
        result = asyncio.run(run_generation())
    except RuntimeError as e:
        # ネストしたイベントループ対策
        import nest_asyncio
        nest_asyncio.apply()
        result = asyncio.run(run_generation())
    
    script = result.script
    song_title = result.song_title
    artist_name = result.artist_name
    preview_url = result.preview_url
    audio_path = result.audio_path
    use_fallback_song = result.use_fallback_song
    
    st.success("完成しました！")
    save_generation_result(...)
    # ... 表示処理 ...

# アプリ終了時クリーンアップ
import atexit
atexit.register(shutdown_executor)
```
**依存追加**: `requirements.txt` に `nest-asyncio>=1.5.0` 追加
**テスト**: 統合テストで非同期版動作確認、複数回生成でexhaustionなし
**完了基準**: 同期版と同等機能、生成中もUI操作可能（別タブ切替等）

---

## Step 4: 開発用依存関係整理 (requirements-dev.txt)
**対象ファイル**: `requirements-dev.txt` 新規
**作業内容**: テスト・リンティング・型チェック用依存分離
```text
# Testing
pytest>=7.4.0
pytest-cov>=4.1.0
pytest-asyncio>=0.21.0
pytest-mock>=3.11.0
hypothesis>=6.80.0
playwright>=1.40.0

# Linting & Formatting
ruff>=0.1.0
black>=23.0.0
isort>=5.12.0

# Type Checking
mypy>=1.5.0
types-requests>=2.31.0
types-pytz>=2023.3.0

# Pre-commit
pre-commit>=3.5.0

# Coverage
codecov>=2.1.0
```
**実装手順**:
1. ファイル作成
2. `pip install -r requirements-dev.txt` 実行確認
3. `playwright install` でブラウザインストール
**完了基準**: 全ツールインストール成功、バージョン固定

---

## Step 5: 単体テスト拡充・カバレッジ向上
**対象ファイル**: `tests/` 配下全ファイル拡充
**作業内容**: 各モジュールの網羅的テスト・境界値・モック活用

### 5-1: 設定テスト (`tests/test_config.py`)
```python
import os
from retro_radio.config import Settings, get_settings

def test_default_settings():
    s = Settings()
    assert s.cache_ttl == 3600
    assert s.min_year == 1950
    assert s.max_year == 2025

def test_env_override(monkeypatch):
    monkeypatch.setenv("RETRO_RADIO_CACHE_TTL", "7200")
    s = Settings()
    assert s.cache_ttl == 7200

def test_validation_errors():
    import pytest
    with pytest.raises(Exception):
        Settings(cache_ttl=30)  # ge=60違反
    with pytest.raises(Exception):
        Settings(default_year=1900)  # range違反
```

### 5-2: バリデータテスト (`tests/test_validators.py`)
```python
from hypothesis import given, strategies as st
from retro_radio.utils.validators import GenerationRequest, sanitize_text

@given(year=st.integers(1950, 2025), month=st.integers(1, 12), day=st.integers(1, 31))
def test_valid_dates(year, month, day):
    import datetime
    try:
        datetime.date(year, month, day)
        req = GenerationRequest(year=year, month=month, day=day)
        assert req.year == year
    except ValueError:
        pass  # 無効な日付はスキップ

def test_invalid_date_feb30():
    import pytest
    with pytest.raises(ValueError, match="存在しません"):
        GenerationRequest(year=2020, month=2, day=30)

def test_sanitize_xss():
    assert sanitize_text('<script>alert(1)</script>') == 'scriptalert(1)/script'
    assert sanitize_text('A' * 6000) == 'A' * 5000
```

### 5-3: 非同期テスト (`tests/test_async_runner.py`)
```python
import pytest
import asyncio
from retro_radio.utils.async_runner import run_in_executor, shutdown_executor

@pytest.mark.asyncio
async def test_run_in_executor():
    def sync_add(a, b): return a + b
    result = await run_in_executor(sync_add, 2, 3)
    assert result == 5

@pytest.mark.asyncio
async def test_exception_propagation():
    def sync_raise(): raise ValueError("test")
    with pytest.raises(ValueError, match="test"):
        await run_in_executor(sync_raise)

def test_shutdown():
    shutdown_executor()  # エラーにならないこと
```

**目標カバレッジ**: 各モジュール80%以上、全体75%以上
**実行**: `pytest --cov=retro_radio --cov-report=term-missing --cov-fail-under=75`
**完了基準**: 全テスト通過、カバレッジ基準達成、hypothesisで境界値自動発見

---

## Step 6: E2Eテスト実装 (tests/test_e2e.py)
**対象ファイル**: `tests/test_e2e.py` 新規
**作業内容**: Playwright で実ブラウザ操作テスト
```python
import pytest
from playwright.sync_api import Page, expect

@pytest.mark.e2e
def test_full_generation_flow(page: Page, streamlit_server):
    """完全フロー: 年選択→生成→音声再生→履歴"""
    page.goto(streamlit_server)
    
    # 初期表示確認
    expect(page.locator('text="レトロラジオ・タイムマシン"')).to_be_visible()
    expect(page.locator('text="西暦を選んでください"')).to_be_visible()
    
    # 年選択
    page.locator('input[type="range"]').fill("1980")
    expect(page.locator('text="選択された年: 1980年"')).to_be_visible()
    
    # 生成実行
    page.locator('button:has-text("ラジオを再生する")').click()
    
    # 完了待機（最大30秒）
    expect(page.locator('text="完成しました！"')).to_be_visible(timeout=30000)
    
    # 結果確認
    expect(page.locator('text="ラジオ原稿"')).to_be_visible()
    expect(page.locator('audio')).to_have_attribute('src')
    expect(page.locator('text="今日の一曲"')).to_be_visible()
    
    # 履歴確認
    page.locator('button:has-text("再生")').first.click()
    expect(page.locator('text="完成しました！"')).to_be_visible(timeout=10000)

@pytest.mark.e2e
def test_api_key_warning(page: Page, streamlit_server_no_key):
    """APIキー未設定時の警告"""
    page.goto(streamlit_server_no_key)
    expect(page.locator('text="Gemini APIキーが設定されていません"')).to_be_visible()

@pytest.mark.e2e
def test_pwa_manifest(page: Page, streamlit_server):
    """PWAマニフェスト存在確認"""
    page.goto(streamlit_server)
    manifest_link = page.locator('link[rel="manifest"]')
    expect(manifest_link).to_have_attribute('href', '/static/manifest.json')

# コンフィグ: tests/conftest.py で fixture 定義
```
**fixture例** (`tests/conftest.py`):
```python
import pytest
import subprocess
import time
import requests

@pytest.fixture(scope="session")
def streamlit_server():
    proc = subprocess.Popen([
        "streamlit", "run", "retro_radio/main.py",
        "--server.port=8502", "--server.headless=true"
    ], env={**os.environ, "GEMINI_API_KEY": "test-key"})
    # 起動待機
    for _ in range(30):
        try:
            requests.get("http://localhost:8502", timeout=1)
            break
        except:
            time.sleep(1)
    yield "http://localhost:8502"
    proc.terminate()
```
**完了基準**: 主要フロー自動検証、CIでヘッドレス実行可能

---

## Step 7: リンティング・型チェック設定
**対象ファイル**: `pyproject.toml`, `.pre-commit-config.yaml` 新規
**作業内容**: 統一コードスタイル・静的解析自動化

### pyproject.toml
```toml
[tool.ruff]
line-length = 100
target-version = "py311"
select = ["E", "F", "I", "W", "UP", "B", "C4", "PIE", "T20", "TRY"]
ignore = ["TRY003"]
exclude = [".venv", "dist", "build"]

[tool.ruff.format]
quote-style = "double"
indent-style = "space"

[tool.mypy]
python_version = "3.11"
warn_return_any = true
warn_unused_configs = true
disallow_untyped_defs = true
ignore_missing_imports = true
```

### .pre-commit-config.yaml
```yaml
repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.1.0
    hooks:
      - id: ruff
        args: [--fix, --exit-non-zero-on-fix]
      - id: ruff-format
  - repo: https://github.com/pre-commit/mirrors-mypy
    rev: v1.5.0
    hooks:
      - id: mypy
        args: [--strict]
  - repo: local
    hooks:
      - id: pytest
        name: pytest
        entry: pytest -x
        language: system
        types: [python]
        pass_filenames: false
```
**実装手順**: `pre-commit install` でGitフック登録
**完了基準**: `ruff check . --fix`, `mypy retro_radio/`, `pytest` 全通過

---

## Step 8: GitHub Actions CIパイプライン
**対象ファイル**: `.github/workflows/ci.yml` 新規
**作業内容**: テスト・リンター・型チェック・カバレッジ自動実行
```yaml
name: CI Pipeline
on:
  push:
    branches: [main, develop]
  pull_request:
    branches: [main]

env:
  PYTHON_VERSION: '3.11'

jobs:
  lint-and-typecheck:
    name: Lint & Type Check
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ env.PYTHON_VERSION }} }
      - name: Install dependencies
        run: |
          pip install -r requirements.txt -r requirements-dev.txt
      - name: Ruff check
        run: ruff check .
      - name: Ruff format check
        run: ruff format --check .
      - name: MyPy type check
        run: mypy retro_radio/

  test:
    name: Unit & Integration Tests
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ env.PYTHON_VERSION }} }
      - name: Install dependencies
        run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Run tests with coverage
        run: pytest --cov=retro_radio --cov-report=xml --cov-fail-under=75
      - name: Upload coverage
        uses: codecov/codecov-action@v3
        with: { files: ./coverage.xml }

  e2e-test:
    name: E2E Tests (Playwright)
    runs-on: ubuntu-latest
    if: github.event_name == 'push' || github.event.pull_request.draft == false
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: ${{ env.PYTHON_VERSION }} }
      - uses: microsoft/playwright-github-action@v1
      - name: Install dependencies
        run: pip install -r requirements.txt -r requirements-dev.txt
      - name: Run E2E tests
        run: pytest tests/test_e2e.py -v
        env:
          GEMINI_API_KEY: ${{ secrets.GEMINI_API_KEY }}

  deploy-staging:
    name: Deploy to Staging
    needs: [lint-and-typecheck, test, e2e-test]
    if: github.ref == 'refs/heads/develop'
    runs-on: ubuntu-latest
    steps:
      - uses: streamlit/streamlit-deploy-action@v1
        with:
          app-path: retro_radio/main.py
          branch: develop
        env:
          STREAMLIT_SHARING_TOKEN: ${{ secrets.STREAMLIT_TOKEN }}

  deploy-production:
    name: Deploy to Production
    needs: [lint-and-typecheck, test, e2e-test]
    if: github.ref == 'refs/heads/main'
    runs-on: ubuntu-latest
    steps:
      - uses: streamlit/streamlit-deploy-action@v1
        with:
          app-path: retro_radio/main.py
          branch: main
        env:
          STREAMLIT_SHARING_TOKEN: ${{ secrets.STREAMLIT_TOKEN }}
```
**Secrets設定必要**: `GEMINI_API_KEY`, `STREAMLIT_TOKEN`
**完了基準**: PR作成時全ジョブ実行、mainマージ時本番デプロイ

---

## Step 9: パフォーマンステスト・ベンチマーク
**対象ファイル**: `tests/test_performance.py` 新規
**作業内容**: 生成時間・メモリ・同時接続測定
```python
import pytest
import time
import asyncio
from retro_radio.core.pipeline import generate_all_async
from retro_radio.utils.async_runner import shutdown_executor

@pytest.mark.performance
@pytest.mark.asyncio
async def test_generation_latency():
    """生成レイテンシ測定（モック使用）"""
    from unittest.mock import patch
    with patch('retro_radio.core.script_generator.generate_radio_script') as m1, \
         patch('retro_radio.core.music_search.search_itunes_songs') as m2, \
         patch('retro_radio.core.tts.text_to_speech') as m3:
        m1.return_value = "テスト原稿"
        m2.return_value = [{"trackName": "曲", "artistName": "歌手", "previewUrl": "http://x.mp3"}]
        m3.return_value = "/tmp/test.mp3"
        
        start = time.perf_counter()
        result = await generate_all_async(1980, 5, 15)
        elapsed = time.perf_counter() - start
        
        assert result.script == "テスト原稿"
        assert elapsed < 5.0  # 5秒以内（モックなので高速）

@pytest.mark.performance
def test_memory_leak():
    """メモリリークチェック（簡易）"""
    import tracemalloc
    tracemalloc.start()
    
    for _ in range(100):
        from retro_radio.utils.session import init_session_state
        init_session_state()
    
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 50 * 1024 * 1024  # 50MB以下
```
**実行**: `pytest tests/test_performance.py -v --benchmark-only`
**完了基準**: レイテンシ基準内、メモリ増加なし、ボトルネック特定

---

## Step 10: 負荷テスト・同時接続検証
**対象ファイル**: `tests/test_load.py` 新規
**作業内容**: Locust で同時ユーザーシミュレーション
```python
# tests/test_load.py
from locust import HttpUser, task, between

class RetroRadioUser(HttpUser):
    wait_time = between(1, 3)
    
    def on_start(self):
        self.client.get("/")
    
    @task(3)
    def generate_radio(self):
        # 年選択→生成の流れ（Streamlitはstatefulなので簡易版）
        self.client.post("/_stcore/stream", json={
            "widget_id": "slider", "value": 1980
        })
        self.client.post("/_stcore/stream", json={
            "widget_id": "button_generate", "value": True
        })
    
    @task(1)
    def view_history(self):
        self.client.get("/?history=1")

# 実行: locust -f tests/test_load.py --host=http://localhost:8501
# 目標: 10同時ユーザーでエラー率<1%、p95<10秒
```
**完了基準**: 目標達成、ボトルネック（DB・API・CPU）特定

---

## Step 11: 依存関係脆弱性スキャン自動化
**対象ファイル**: `.github/workflows/security.yml` 新規
**作業内容**: Dependabot・pip-audit・SAST統合
```yaml
name: Security Scan
on:
  schedule: [{ cron: '0 0 * * 0' }]  # 毎週日曜
  push: { branches: [main] }

jobs:
  dependabot:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: dependabot/fetch-metadata@v1
      - name: Auto-merge dependabot PRs
        if: github.actor == 'dependabot[bot]'
        run: gh pr merge --auto --squash ${{ github.event.pull_request.number }}

  pip-audit:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
      - run: pip install pip-audit
      - run: pip-audit -r requirements.txt -r requirements-dev.txt --format=json --output=audit.json
      - uses: github/codeql-action/upload-sarif@v2
        if: always()
        with: { sarif_file: audit.json }

  codeql:
    uses: github/codeql-action/analyze@v2
    with:
      languages: python
```
**完了基準**: 週次スキャン実行、Critical/High脆弱性0維持

---

## Step 12: リリース自動化・バージョニング
**対象ファイル**: `.github/workflows/release.yml`, `pyproject.toml` 更新
**作業内容**: セマンティックバージョニング・変更履歴・タグ自動生成
```yaml
# .github/workflows/release.yml
name: Release
on:
  push:
    tags: ['v*']

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with: { fetch-depth: 0 }
      - uses: actions/setup-python@v5
      - name: Install
        run: pip install build twine
      - name: Build
        run: python -m build
      - name: Publish to PyPI
        if: github.repository_owner == 'your-org'
        run: twine upload dist/*
        env:
          TWINE_USERNAME: __token__
          TWINE_PASSWORD: ${{ secrets.PYPI_TOKEN }}
      - name: Generate Changelog
        run: |
          git cliff --tag ${{ github.ref_name }} --output CHANGELOG.md
      - name: Create GitHub Release
        uses: softprops/action-gh-release@v1
        with:
          generate_release_notes: true
          files: dist/*
```
**pyproject.toml 追加**:
```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "retro-radio"
version = "0.1.0"
description = "レトロラジオ・タイムマシン"
readme = "README.md"
requires-python = ">=3.11"
dependencies = [
    "streamlit==1.35.0",
    "google-generativeai==0.7.2",
    "gTTS==2.5.0",
    "requests==2.31.0",
    "tenacity==8.2.3",
    "pydantic-settings>=2.0",
    "nest-asyncio>=1.5",
]
optional-dependencies = {
    dev = ["pytest>=7.4", "pytest-cov>=4.1", "pytest-asyncio>=0.21", "pytest-mock>=3.11", "hypothesis>=6.80", "playwright>=1.40", "ruff>=0.1", "black>=23.0", "isort>=5.12", "mypy>=1.5", "pre-commit>=3.5", "codecov>=2.1", "locust>=2.15", "pip-audit>=2.6", "git-cliff>=1.0"]
}
```
**完了基準**: `git tag v0.1.0 && git push origin v0.1.0` で自動リリース

---

## T2 リグレッション防止チェックリスト
- [ ] 非同期版生成: 既存同期版と同等結果（全フロー）
- [ ] 進捗バー: 0→33→66→100% 正確更新
- [ ] エラー時: フォールバック動作・エラー記録
- [ ] 並列版: 原稿・楽曲同時実行で高速化確認
- [ ] 単体テスト: 全モジュールカバレッジ75%以上
- [ ] E2Eテスト: 主要3フロー自動検証
- [ ] リンター: `ruff check .` エラー0
- [ ] 型チェック: `mypy retro_radio/` エラー0
- [ ] CI: PR時全ジョブ成功、mainマージ時デプロイ
- [ ] セキュリティ: 脆弱性スキャン0件維持
- [ ] パフォーマンス: 生成レイテンシ目標内、メモリリークなし
- [ ] リリース: タグプッシュで自動PyPI公開・Changelog生成