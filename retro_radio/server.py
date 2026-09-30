import os
import re
import hashlib
import asyncio
import tempfile
import logging
import threading
import time
from typing import Optional, List, Dict, Any, Literal
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, date

from fastapi import FastAPI, HTTPException, Request, Depends
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send
from gtts import gTTS

from .models.radio import ScriptSegment, PlaylistItem, PlaylistItemType
from .config import Settings, get_settings
from .utils.async_runner import shutdown_executor
from .utils.errors import AppError, ScriptGenerationError, TTSError, ConfigurationError
from .core.script_generator import generate_radio_script, parse_script_segments
from .core.music_search import enrich_songs
from .core.song_selector import SongSelector
from .services.song_store import PreviewCache, SongHistoryStore
from .core import legacy_tts
from .core.fallback import get_fallback_song, get_reminiscence_quiz, HistoricalRadioPrograms, select_program_songs
from .core.songs import song_key
from .utils.logging_config import setup_logging
from .utils.text_cleaner import clean_script_for_tts

# --- 提案④・S5: 非同期ジョブ / 協調的キャンセル ---------------------------------
from . import jobs
from .jobs import (
    EVENT_CANCELLED,
    EVENT_DONE,
    EVENT_ESTIMATE,
    EVENT_FAILED,
    EVENT_MUSIC_DONE,
    EVENT_MUSIC_STARTED,
    EVENT_PLAYLIST_DONE,
    EVENT_SCRIPT_DONE,
    EVENT_SCRIPT_STARTED,
    EVENT_TTS_DONE,
    EVENT_TTS_SEGMENT,
    Job,
    JobCancelled,
    GenerationSlots,
    current_event,
    cancellable_wait,
    raise_if_cancelled,
)

# --- 提案⑧・S4: 認証 / テナント ----------------------------------------------------
from .api import audit_router, me_router
from .api.deps import (
    audio_tenant as audio_tenant_dependency,
    optional_principal,
    require_tenant,
    settings_dependency,
)
from .auth.tokens import Principal
from .services.tenant_cache import tenant_cache_from_settings, TenantTtsCache

logger = logging.getLogger("retro_radio")
setup_logging()

settings = get_settings()

# `time.sleep` をキャンセル対応版へ差し替える。
# `tenacity.nap.sleep`（Gemini の指数バックオフ）も `time.sleep` を経由するため、
# ここ 1 行で Gemini のリトライ境界がキャンセル可能になる。
# 差し替え後の挙動は「ジョブスレッドだけ cancellable、他は通常の sleep」で保存される。
jobs.install_cancellable_sleep()

# TTS 音声キャッシュを置くディレクトリ（OS の一時領域）
CACHE_DIR = Path(tempfile.gettempdir()) / "retro_radio_audio_cache"

# フロントエンドの静的ファイル
STATIC_DIR = Path(__file__).parent.parent / "static"

# --- セキュリティヘッダ -----------------------------------------------------------
# 外部リソースは static/index.html と static/app.js を実際に読んで列挙したものだけ許可する。
#   * Google Fonts   : fonts.googleapis.com（web フォント配信）/ fonts.gstatic.com（実ファイル配信）
#   * data:          : favicon・manifest のアイコン・app.css のノイズテクスチャ
#   * blob:          : app.js の object URL の読み込みと POST 応答の Sister. の絶対URL生成
#   * media-src      : iTunes プレビュー https://*.mzstatic.com / audio-ssl.itunes.apple.com
#   * worker-src     : /static/service-worker.js
#   * cdn.jsdelivr   : FastAPI 標準の /docs・/redoc（Swagger UI / ReDoc の読み込み先）
# 未知のホストを一律に許可しない。追加や変更は環境変数 RETRO_RADIO_CSP で上書きできる。
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
        "script-src 'self' https://cdn.jsdelivr.net",
        # 'unsafe-inline' は index.html の style 属性とチャート・クイズ枠の表示制御のために必須
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com https://cdn.jsdelivr.net",
        "font-src 'self' data: https://fonts.gstatic.com",
        "img-src 'self' data: blob: https://fastapi.tiangolo.com",
        "media-src 'self' blob: data: "
        "https://*.mzstatic.com https://audio-ssl.itunes.apple.com",
        "connect-src 'self'",
        "manifest-src 'self'",
        "worker-src 'self' blob:",
    ]
)

PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), display-capture=(), geolocation=(), "
    "gyroscope=(), magnetometer=(), microphone=(), midi=(), payment=(), usb=()"
)

# このアプリはマイク・カメラ・決済・位置情報を使わないため、無効でよい。
# autoplay はオーディオ再生に干渉するリスクがあるため敢えて列挙しない。


def _env_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        logger.warning(f"{name} が数値ではないため既定値 {default} を使います")
        return default


def build_csp() -> str:
    """Content-Security-Policy ヘッダの値（RETRO_RADIO_CSP で完全上書きできる）"""
    override = os.environ.get("RETRO_RADIO_CSP", "").strip()
    return override or CONTENT_SECURITY_POLICY


def _request_is_https(scope: Scope) -> bool:
    """TLS 接続 or リバースプロキシが HTTPS を-forward しているか（信頼プロキシは限定しない）"""
    if scope.get("scheme") == "https":
        return True
    for key, value in (scope.get("headers") or ()):
        if key == b"x-forwarded-proto" and value.split(b",")[0].strip().lower() == b"https":
            return True
    return False


def build_security_headers(scope: Scope) -> Dict[str, str]:
    """1 レスポンスに付与するセキュリティヘッダ。

    HSTS は HTTPS でのみ有効化する。平文 HTTP に返すと意味が無く、
    ローカル開発環境（http://localhost:8501）を破壊するため。
    """
    headers = {
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "Referrer-Policy": "no-referrer",
        "Content-Security-Policy": build_csp(),
        "Permissions-Policy": PERMISSIONS_POLICY,
    }
    if _env_flag("RETRO_RADIO_HSTS_ENABLED", True):
        max_age = _env_int("RETRO_RADIO_HSTS_MAX_AGE", 31536000)
        if max_age > 0 and _request_is_https(scope):
            headers["Strict-Transport-Security"] = (
                f"max-age={max_age}; includeSubDomains"
            )
    if str(scope.get("path", "")).endswith("/service-worker.js"):
        headers["Service-Worker-Allowed"] = "/"
    return headers


class SecurityHeadersMiddleware:
    """全レスポンスにセキュリティヘッダを足す純 ASGI ミドルウェア。

    BaseHTTPMiddleware は FileResponse / StaticFiles のストリーミング応答や
    BackgroundTask を壊しうるため、`http.response.start` のみを加工する実装にしている。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_security_headers(message: Dict[str, Any]) -> None:
            if message.get("type") == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                for name, value in build_security_headers(scope).items():
                    response_headers[name] = value
            await send(message)

        await self.app(scope, receive, send_with_security_headers)


# --- /api/audio の配信防御 -------------------------------------------------------
# TTS キャッシュには gTTS が書いた mp3 しか無いが、認証のない GET エンドポイントなので
# 「拡張子 → サイズ → マジックバイト」の3層で検証する。
AUDIO_MIN_BYTES = 1024
AUDIO_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}\.mp3$")


def _is_mp3_header(head: bytes) -> bool:
    """先頭バイト列が MP3 として妥当か（ID3 タグ or MPEG フレーム同期 11bit）"""
    if not head:
        return False
    if head.startswith(b"ID3"):
        return True
    # 0xFF + (2バイト目の上位3bit が 111) = MPEG 音声フレーム同期（0xFB / 0xF3 / 0xF2 / 0xE3 等）
    return len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0


# --- 同時実行スロット / テナントキャッシュ / 認証ポリシー ---------------------------
# 同時実行できる番組生成の上限。Gemini / gTTS / iTunes はいずれもブロッキングHTTPのため。
#
# `GenerationSlots` は `threading.BoundedSemaphore` を継承しつつ acquire / release の
# 成功回数を数える。クライアント abort 後に「スロットが確実に 0 に戻った」ことを
# sleep 無しで検証するための観測点（提案④・S5 の核心的な回帰防止）。
_generation_slots = GenerationSlots(settings.max_concurrent_generations)
_cache_lock = threading.Lock()
_sweep_counter = 0

_tenant_cache: Optional[TenantTtsCache] = None


def _cache() -> TenantTtsCache:
    """テナント別キャッシュのハンドル。

    `tests/conftest.py` が `server.CACHE_DIR` を実行ごとに差し替えるため、
    モジュール import 時に確定させず、ルートが変わったら作り直す。
    """
    global _tenant_cache
    if _tenant_cache is None or _tenant_cache.root != Path(CACHE_DIR):
        _tenant_cache = tenant_cache_from_settings(Path(CACHE_DIR))
    return _tenant_cache


def _auth_enforced() -> bool:
    """「認証が有効か」をこのプロセスが実際に適用している値として返す。

    判定の順序:
    1. 環境変数 `RETRO_RADIO_REQUIRE_AUTH` が明示されていればそれを使う。
    2. 無ければ設定値 `settings.require_auth`（既定 True = 認証必須）。

    `get_settings()` は `lru_cache` なので起動時の 1 度しか読まれない。
    ここで環境変数を優先して読み直すことで、後から変わった値も反映する。
    """
    raw = os.environ.get("RETRO_RADIO_REQUIRE_AUTH")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() not in ("0", "false", "no", "off")
    return bool(settings.require_auth)


def tenant_principal(
    request: Request,
    current_settings: Settings = Depends(settings_dependency),
) -> Principal:
    """`/api/generate` と `/api/jobs*` のテナント依存（S4 の `require_tenant` を配線）。

    - 認証が有効なとき: S4 の `require_tenant` に委譲する。資格情報が無ければ 401、
      論理削除済みなら 403、資格情報未設定なら 503 で fail-closed。
    - 認証を意図的に切った個人モード（`RETRO_RADIO_REQUIRE_AUTH=0`）:
      `default` テナントで通す。README の API 契約に従う非認証クライアントのため。
    """
    if not _auth_enforced():
        return Principal(
            tenant_id="default",
            role="member",
            auth_mode="disabled",
            authenticated=False,
        )
    return require_tenant(request, current_settings)


def _tenant_cache_dir(tenant_id: Optional[str] = None, *, create: bool = True) -> Path:
    """TTS キャッシュを置くディレクトリ。

    - 認証が有効なとき: `CACHE_DIR/<tenant_id>/`（S4 の `ensure_tenant_dir`）。
      テナント間で物理的に別ディレクトリになるため交差しない。
    - 認証を切った個人モード: 従来どおり `CACHE_DIR` 直下（README の API 契約）。
    """
    if not _auth_enforced():
        if create:
            _ensure_cache_dir()
        return CACHE_DIR
    cache = _cache()
    return cache.ensure_tenant_dir(tenant_id) if create else cache.tenant_dir(tenant_id)


def _audio_url_for(tenant_id: Optional[str], filename: str) -> str:
    """配信 URL を組み立てる。

    認証が有効なときだけテナント ID を含む URL（`/api/audio/<tenant>/<file>`）を返す。
    これがないと配信時のテナント照合が成立しない。
    個人モードでは URL 互換のフラット形式を維持する。
    """
    if not _auth_enforced():
        return f"/api/audio/{filename}"
    return _cache().relative_url_for(tenant_id, filename)


def _record_generation_audit(
    tenant_id: str,
    user_id: Optional[str],
    phase: str,
    outcome: str,
    meta: Optional[Dict[str, Any]] = None,
) -> bool:
    """生成イベントを監査ログへ 1 行記録する。例外は絶対に投げない。

    phase は `started` / `completed` / `failed` / `cancelled` のいずれか。
    1 回の生成につき「開始」と「終端」で 2 行書くため、
    「生成イベント数に対するログ行数」は 200%（= 漏れ 0）になる。
    DB が落ちていても応答は壊さない（可用性が監査より優先）。
    """
    try:
        from .api.audit import ACTION_GENERATION
        from .db.privacy_repository import AuditRepository
        from .db.session import get_db

        payload = dict(meta or {})
        payload["phase"] = phase
        with get_db() as db:
            AuditRepository(db).record(
                tenant_id=tenant_id or "default",
                action=ACTION_GENERATION,
                user_id=user_id,
                resource_type="radio_program",
                resource_id=payload.get("job_id"),
                outcome=outcome if outcome in ("success", "denied", "failure") else "failure",
                # 原稿本文・氏名・生年は監査ログに入れない（開示請求の範囲を広げるため）。
                meta={k: v for k, v in payload.items() if k != "script"},
            )
        return True
    except Exception:  # noqa: BLE001 - 監査は失敗しても応答を壊さない
        logger.warning("監査ログの記録に失敗しました（応答は続行します）", exc_info=True)
        return False


def _ensure_cache_dir() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _sweep_tts_cache(force: bool = False) -> int:
    """TTLより古いTTSキャッシュを削除（起動時 + 一定間隔ごと）

    間隔の単位は **リクエストではなく `generate_tts_cached` の呼び出し回数**。
    1 回の /api/generate で 1 、全身用の全体版 TTS 込みで典型的に 6〜7 回呼ばれるため、
    `tts_cache_sweep_interval`（既定50）は概ね 8〜9 リクエストに相当する実効間隔になる。
    """

    global _sweep_counter
    if not force:
        with _cache_lock:
            _sweep_counter += 1
            if _sweep_counter % max(1, settings.tts_cache_sweep_interval) != 0:
                return 0
    if not CACHE_DIR.is_dir():
        return 0
    cutoff = time.time() - settings.tts_cache_ttl_days * 86400
    removed = 0
    try:
        for entry in CACHE_DIR.iterdir():
            try:
                if entry.is_file() and entry.suffix in (".mp3", ".tmp") and entry.stat().st_mtime < cutoff:
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
    except OSError as e:
        logger.warning(f"TTSキャッシュの整理に失敗しました: {e}")
    if _auth_enforced():
        # 認証有効時はテナントディレクトリも掃除する（S4 の `sweep`）。
        removed += _cache().sweep(tenant_id=None, force=force)
    if removed:
        logger.info(f"古いTTSキャッシュを削除しました: {removed}件")
    return removed


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_cache_dir()
    _sweep_tts_cache(force=True)
    try:
        yield
    finally:
        shutdown_executor()


app = FastAPI(
    title="Retro Radio Time Machine API",
    description="ノスタルジック・レトロラジオ体験 & 介護レク回想法・記念日タイムカプセル API",
    version=settings.app_version,
    lifespan=lifespan
)

# CORS許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# セキュリティヘッダ（CORSMiddleware と並行して、全レスポンスに付与する）
app.add_middleware(SecurityHeadersMiddleware)

# --- 提案⑧・S4 のルータを include する -------------------------------------------
# 認証を保护的差し込んだので、me / audit を公開する。
# `v1`（Pro プラン API のスタブ）は `tests/test_api_access_control.py` が
# 「未 include であること」を固定しているため意図的に外す。
app.include_router(me_router)
app.include_router(audit_router)

# カスタム例外ハンドラー
@app.exception_handler(AppError)
async def app_error_handler(request: Request, exc: AppError):
    # デフォルトは 400 Bad Request
    status_code = 400
    # 特定の例外タイプに応じてステータスコードを調整
    if isinstance(exc, ScriptGenerationError):
        # スクリプト生成エラーは、APIキー関連なら 503、その他は 400
        if "APIキー" in exc.user_message or "API key" in exc.user_message:
            status_code = 503
    elif isinstance(exc, TTSError):
        status_code = 500
    elif isinstance(exc, ConfigurationError):
        status_code = 500
    # MusicSearchError と ValidationError はデフォルトの 400 のまま

    return JSONResponse(
        status_code=status_code,
        content={"detail": exc.user_message},
    )

RadioMode = Literal["normal", "care_recreation", "anniversary"]

class GenerateRequest(BaseModel):
    year: int = Field(..., ge=settings.min_year, le=settings.max_year, description=f"対象年 ({settings.min_year}〜{settings.max_year})")
    # 既定は 1月1日。「今天」（実行当日）にすると month 省略時に 2月・4月・6月・9月・11月で
    # 「その月の31日は存在しない」→ 422 になる（実測: 今日は30日のため2月は422）
    month: int = Field(default=1, ge=1, le=12)
    day: int = Field(default=1, ge=1, le=31)
    mode: RadioMode = Field(default="normal", description="モード: normal | care_recreation | anniversary")
    target_name: Optional[str] = Field(
        default=None,
        max_length=64,
        description="記念日ギフト用の対象者名（最大64文字）",
    )

    @field_validator("target_name")
    @classmethod
    def validate_target_name(cls, value: Optional[str]) -> Optional[str]:
        """対象名は原稿の f-string と `### 見出し` パーサ（CORE）の入力になるため、
        改行・制御文字・見出しマーカーで構造を乗っ取れないようにする。
        """
        if value is None:
            return value
        if any(ch in value for ch in ("\r", "\n", "\t", "\x00")):
            raise ValueError("target_name に改行・タブ・NULL 文字は使用できません")
        if "###" in value:
            raise ValueError("target_name に見出しマーカー '###' は使用できません")
        return value

    @model_validator(mode="after")
    def validate_calendar_date(self) -> "GenerateRequest":
        # 2月30日などの実在しない日付は曜日計算（core/fallback.py）で例外になるため弾く
        try:
            date(self.year, self.month, self.day)
        except ValueError:
            raise ValueError(f"{self.year}年{self.month}月{self.day}日は存在しない日付です")
        return self

class GenerateResponse(BaseModel):
    year: int
    month: int
    day: int
    mode: str
    script: str
    audio_url: Optional[str]
    songs: List[Dict[str, Any]]
    song: Optional[Dict[str, Any]] = None
    reminiscence_quiz: Optional[List[Dict[str, str]]] = None
    target_name: Optional[str] = None
    generated_at: str
    segments: Optional[List[Dict[str, Any]]] = None
    program_guide: Optional[Dict[str, Any]] = None
    playlist: Optional[List[Dict[str, Any]]] = None
    # 周回数ぶんのパス（1 パス = 曲 → トーク → 曲 → … → 曲）。
    # **パスごとに別の曲**が入っており、1 回の放送の中で同じ曲が
    # 2 回以上流れない。旧クライアントは `playlist`（= passes[0]）だけを使う。
    passes: Optional[List[List[Dict[str, Any]]]] = None
    # 1 パスの playlist をクライアントが何周するか（既定＝推奨周回数）
    # クライアントは UI で 1〜 に変更できる
    loop_count: int = settings.program_loop_count

def _tts_cache_filename(text: str) -> str:
    # 音声設定（lang / tld / slow）をキーに含めないと同じファイル名のまま古い言語の音声が返る
    cache_key = f"{text}_{settings.tts_language}_{settings.tts_tld}_{settings.tts_slow}"
    return f"tts_{hashlib.sha256(cache_key.encode('utf-8')).hexdigest()}.mp3"

def _remove_quietly(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass

def _save_tts_to_temp(tts, cache_dir: Optional[Path] = None) -> str:
    """同一ファイルシステム上の一時ファイルへ書き出す（os.replace の atomic rename 用）"""
    directory = Path(cache_dir) if cache_dir is not None else CACHE_DIR
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(directory), prefix=".tts_", suffix=".tmp")
    os.close(fd)
    try:
        tts.save(tmp_path)
    except Exception:
        _remove_quietly(tmp_path)
        raise
    return tmp_path

def _atomic_install(tmp_path: str, filepath: Path) -> None:
    for attempt in range(3):
        try:
            os.replace(tmp_path, filepath)
            return
        except PermissionError:
            # 配信中のファイルは Windows がロックしているケースの短時間リトライ
            if attempt == 2:
                raise
            time.sleep(0.1 * (attempt + 1))

# --- gTTS の呼び出し間隔調整 ----------------------------------------------------
# gTTS は Google Translate の公開 TTS を使っており、短時間に連続すると
# 429 (Too Many Requests) で弾かれる。1 回の /api/generate は
# 「セグメント数 + 全体版」で最大 6 回が直列に走るので、間隔を空けないと
# どのセグメントも生成できないまま「原稿だけの無声番組」になってしまう。
# 実測: 間隔なしだと 6 回すべて 429 になる。
_tts_gate = threading.Lock()
_tts_last_call_at = 0.0
_tts_rate_limited_until = 0.0


def _tts_is_network_client() -> bool:
    """今 `gTTS` が実物のネットワーククライアントかどうか。

    テストは `gTTS` をネットワーク不要のスタブへ差し替えるため、
    差し替えられている間は間隔待ちをしては困らない（テストが数十倍遅くなる）。
    実 gTTS のクラスは `gtts` パッケージにある。
    """
    module = getattr(gTTS, "__module__", "") or ""
    return module.split(".")[0] == "gtts"


def _tts_throttle() -> None:
    """実 gTTS 呼び出しの間に最低 `tts_min_interval_seconds` を空ける。

    待ち時間は `cancellable_wait` を通すため、ジョブがキャンセルされていれば
    `JobCancelled` が送出され、**待たされずに**次のチェックポイントへ進める。
    """
    global _tts_last_call_at

    if not _tts_is_network_client():
        return
    try:
        interval = float(settings.tts_min_interval_seconds)
    except (TypeError, ValueError):
        return
    if interval <= 0:
        return

    with _tts_gate:
        now = time.monotonic()
        wait = _tts_last_call_at + interval - now
        if wait > 0:
            # キャンセル可能な待ち。セット済みなら `JobCancelled` が飛び、
            # `_tts_gate` は with が解放する。
            cancellable_wait(current_event(), wait)
        _tts_last_call_at = time.monotonic()


def _is_rate_limited(error: BaseException) -> bool:
    """429 (Too Many Requests) 由来かどうか。"""
    text = "%s %s" % (type(error).__name__, error)
    return "429" in text or "Too Many Requests" in text


def _note_rate_limited() -> None:
    """429 を受けた時刻を記録する（ブレーカー用）"""
    global _tts_rate_limited_until
    try:
        hold = float(settings.tts_circuit_breaker_seconds)
    except (TypeError, ValueError):
        hold = 60.0
    if hold <= 0:
        return
    with _tts_gate:
        _tts_rate_limited_until = time.monotonic() + hold


def _tts_circuit_open() -> bool:
    """直近で 429 を受けており、この呼び出しを諦めるべきか。

    レート制限が連打ではなく IP 単位で恒久的に拒まれている場合
    （実測: 3 秒空けても 429）、毎回 6 回叩くと要求が 20 秒以上かかった挙上、
    それでも 1 バイトも取れない。ブレーカーで即座に諦める。
    """
    if not _tts_is_network_client():
        return False
    with _tts_gate:
        if _tts_rate_limited_until <= 0:
            return False
        return time.monotonic() < _tts_rate_limited_until


def _tts_is_blocked_by_gtts() -> bool:
    """gTTS の主要経路が使えず、旧エンドポイントへ切り替えるべき状態か。

    `batchexecute` の CAPTCHA 対策の 429 は `_is_rate_limited` にも掛からない
    形式（例: 500 やタイムアウト）で返ることもあるため、回路状態でも判定する。
    """
    return _tts_circuit_open()


def _generate_tts_via_legacy_endpoint(
    text: str, filepath: Path, cache_dir: Optional[Path] = None, cause: Optional[BaseException] = None
) -> str:
    """旧来の `GET /translate_tts` で音声を合成してキャッシュへ入れる。

    gTTS 2.5.4 の `batchexecute` は Google の CAPTCHA 対策で 429 になるが、
    旧エンドポイントは現在 200 を返す。gTTS が使えないときの救済経路。
    """
    # `settings.tts_tld` は "co.jp" / "com" のような TLD そのもの。
    # ホストは `translate.google.<tld>` なので、そのまま使う（"jp" に切り詰めない）。
    tld = settings.tts_tld or "com"
    logger.info(f"旧 TTS エンドポイントで合成を試みます: tld={tld}, chars={len(text)}")
    try:
        audio = legacy_tts.synthesize(
            text, lang=settings.tts_language, tld=tld
        )
    except Exception as e:
        logger.error(f"旧 TTS エンドポイントでも合成に失敗しました: {e}")
        raise TTSError(
            "音声の合成に失敗しました。時間をおいて再度お試しください。", original=cause or e
        ) from (cause or e)

    directory = Path(cache_dir) if cache_dir is not None else filepath.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(directory), prefix=".tts_", suffix=".tmp")
    os.close(fd)
    try:
        with open(tmp_path, "wb") as handle:
            handle.write(audio)
        _atomic_install(tmp_path, filepath)
        tmp_path = None
    finally:
        if tmp_path:
            _remove_quietly(tmp_path)

    logger.info(
        f"旧 TTS エンドポイントで合成に成功: bytes={len(audio)}, file={filepath.name}"
    )
    return filepath.name


def generate_tts_cached(text: str, tenant_id: Optional[str] = None) -> str:
    """テキストからgTTSで音声を生成し、音声設定込みのハッシュ名でキャッシュ保存。

    Parameters
    ----------
    text:
        読み上げる本文。
    tenant_id:
        認証が有効なときは `CACHE_DIR/<tenant_id>/` 配下へ書く（テナント分離）。
        `None` のときは個人モードのフラット配置（従来どおり `CACHE_DIR` 直下）。
    """
    _sweep_tts_cache()

    filename = _tts_cache_filename(text)
    cache_dir = _tenant_cache_dir(tenant_id)
    filepath = cache_dir / filename

    if filepath.exists():
        logger.info(f"TTSキャッシュヒット: file={filename}")
        jobs.registry.cache_stats.record_hit()
        return filename

    _ensure_cache_dir()
    jobs.registry.cache_stats.record_miss()
    logger.info(f"新規TTS音声生成: chars={len(text)}, file={filename}")

    # チェックポイント（1）。gTTS を叩く直前。ここは cancellable な待ちの直前。
    raise_if_cancelled("tts.begin")

    if _tts_circuit_open():
        # 直近で 429 を受けており、gTTS の batchexecute は使えない。
        # ただし旧来の GET /translate_tts は別の経路なのでそちらへ切り替える。
        logger.info("gTTS はレート制限中のため、旧 TTS エンドポイントへ切り替えます")
        return _generate_tts_via_legacy_endpoint(text, filepath, cache_dir)

    tmp_path: Optional[str] = None
    last_error: Optional[BaseException] = None
    try:
        # 通常ドメイン → 汎用ドメイン の 2 段構え。
        # 1 段目が失敗したら、429 であれば間隔を空けてから 2 段目を試す
        # （空けずに叩くと 429 のまま必ず失敗する）。
        for attempt, tld in enumerate((settings.tts_tld, "com"), start=1):
            _tts_throttle()
            # チェックポイント（2）。`_tts_throttle` の待ち自体が cancellable なので、
            # ここまで来て未キャンセルなら次の gTTS 呼び出しへ進める。
            raise_if_cancelled("tts.throttle")
            try:
                raise_if_cancelled("tts.call")
                tts = gTTS(text=text, lang=settings.tts_language, tld=tld, slow=settings.tts_slow)
                tmp_path = _save_tts_to_temp(tts, cache_dir)
                _atomic_install(tmp_path, filepath)
                tmp_path = None
                last_error = None
                break
            except JobCancelled:
                raise
            except Exception as e:
                last_error = e
                logger.error(f"TTS生成エラー: tld={tld} {e}")
                if tmp_path:
                    _remove_quietly(tmp_path)
                    tmp_path = None
                if attempt == 1 and _is_rate_limited(e):
                    _note_rate_limited()
                    backoff = float(settings.tts_retry_backoff_seconds or 0)
                    if backoff > 0:
                        logger.info(
                            f"TTSレート制限のため {backoff:.1f} 秒待ってから再試行します"
                        )
                        # チェックポイント（3）。backoff は `time.sleep` 経由なので
                        # キャンセルされれば待たずに打ち切る。
                        time.sleep(backoff)
        if last_error is not None:
            raise last_error
    except JobCancelled:
        if tmp_path:
            _remove_quietly(tmp_path)
        raise
    except Exception as e:
        logger.error(f"TTS生成リトライも失敗しました: {e}")
        # gTTS が 429 で使えないときは旧エンドポイントへ切り替える。
        # batchexecute の CAPTCHA 対策で gTTS 全体が死ぬケースを救うため。
        if _is_rate_limited(e) or _tts_is_blocked_by_gtts():
            logger.warning("gTTS が使えないため、旧 TTS エンドポイントへ切り替えます")
            return _generate_tts_via_legacy_endpoint(text, filepath, cache_dir, cause=e)
        raise TTSError(
            "音声の合成に失敗しました。時間をおいて再度お試しください。", original=e
        ) from e
    finally:
        if tmp_path:
            _remove_quietly(tmp_path)

    return filename

def generate_tts_for_segments(
    segments: List[ScriptSegment],
    tenant_id: Optional[str] = None,
    job: Optional[Job] = None,
) -> List[ScriptSegment]:
    """各トークセグメントのTTS音声を生成

    ジョブ指定があれば 1 セグラントごとに `tts.segment` イベントを送る。
    イベントには「キャッシュヒットか」を載せる（`cached`）。
    UI はこの `i / n` を進捗の分子として使う。
    """
    total = len(segments)
    for index, segment in enumerate(segments):
        # チェックポイント（4）。セグメント境界。cancellable な待ちの直前に置く。
        raise_if_cancelled(f"tts.segment[{index}]")
        try:
            # TTS用にクリーニング
            cleaned_content = clean_script_for_tts(segment.content)
            if cleaned_content.strip():
                filename = _tts_cache_filename(cleaned_content)
                cached = (_tenant_cache_dir(tenant_id, create=False) / filename).exists()
                if job is not None:
                    job.emit(EVENT_TTS_SEGMENT, i=index, n=total, cached=cached)
                audio_filename = generate_tts_cached(cleaned_content, tenant_id=tenant_id)
                segment.metadata = segment.metadata or {}
                segment.metadata["audio_url"] = _audio_url_for(tenant_id, audio_filename)
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f"セグメントTTS生成エラー: {e}")
            segment.metadata = segment.metadata or {}
            segment.metadata["audio_url"] = None
    return segments

OPENING_KEYWORDS = ("オープニング", "opening")
ENDING_KEYWORDS = ("エンディング", "ending")

def _song_item(song: Dict[str, Any], order: int) -> Dict[str, Any]:
    return PlaylistItem(
        id=f"song_{order}",
        type=PlaylistItemType.SONG,
        title=song.get("title", "不明"),
        artist=song.get("artist", "不明"),
        preview_url=song.get("preview_url"),
        artwork_url=song.get("artwork_url"),
        is_fallback=song.get("is_fallback", False),
        metadata={"order": order}
    ).to_dict()


def _to_song_dicts(raw_songs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """iTunes の生レスポンス（trackName / previewUrl …）を API の曲形式へ揃える"""
    songs: List[Dict[str, Any]] = []
    for raw in raw_songs:
        preview_url = raw.get("previewUrl")
        songs.append({
            "title": raw.get("trackName", "不明"),
            "artist": raw.get("artistName", "不明"),
            "preview_url": preview_url,
            "artwork_url": raw.get("artworkUrl100"),
            "is_fallback": preview_url is None
        })
    return songs

def _talk_item(segment: ScriptSegment) -> Dict[str, Any]:
    # `segment_index` を載せることで、フロント（app.js）が
    # 「今どの原稿セグメントを朗読中か」を原稿用紙上の該当行へ紐付けられる。
    # 見出し名は重複があるため index を正とする。
    # 既定の dict を直接触らずコピーする（呼び出し元の metadata を汚さないため）。
    metadata = dict(segment.metadata or {})
    metadata["segment_index"] = segment.order
    return PlaylistItem(
        id=segment.id,
        type=PlaylistItemType.TALK,
        title=segment.title,
        content=segment.content,
        estimated_duration=segment.estimated_duration,
        audio_url=metadata.get("audio_url"),
        metadata=metadata
    ).to_dict()

def _classify_segments(segments: List[ScriptSegment]):
    """セグメントを（オープニング / 通常トーク / エンディング）へ分類する。

    同名の見出しが複数来た場合は **先勝ち** にして、余りは黙って消さずに
    通常トークへ寄せる（かつ警告ログを出して追跡できるようにする）。
    旧実装は後勝ちの代入で 1 個を無言で消していた。
    """

    opening_segment = None
    ending_segment = None
    talk_segments: List[ScriptSegment] = []

    for seg in segments:
        title = seg.title or ""
        if any(kw in title for kw in OPENING_KEYWORDS):
            if opening_segment is None:
                opening_segment = seg
                continue
            logger.warning(
                f"オープニング見出しが重複しました（{seg.title!r}）。先勝ちの "
                f"{opening_segment.title!r} をオープニングとして通常トーク側へ回します"
            )
        elif any(kw in title for kw in ENDING_KEYWORDS):
            if ending_segment is None:
                ending_segment = seg
                continue
            logger.warning(
                f"エンディング見出しが重複しました（{seg.title!r}）。先勝ちの "
                f"{ending_segment.title!r} をエンディングとして通常トーク側へ回します"
            )
        talk_segments.append(seg)

    # フォールバック: 見出しが無ければ最初をオープニング、最後をエンディングとみなす
    if not opening_segment and segments:
        opening_segment = segments[0]
        talk_segments = list(segments[1:])
    if not ending_segment and len(segments) > 1:
        ending_segment = segments[-1]
        # 同一オブジェクトだけを取り除く（値が等しい別セグメントを巻き込まないよう `is` で判定）
        talk_segments = [seg for seg in talk_segments if seg is not ending_segment]

    return opening_segment, talk_segments, ending_segment

def _top_up_songs_for_program(
    ordered_songs: List[Dict[str, Any]],
    required: int,
    year: Optional[int],
    reserve: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """曲で始まり曲で終わる構成を成立させるため、スロット数ぶん曲子で埋める。

    短いと、1) トーク同士が連続し 2) 末尾が無音のまま終わる。
    そのため不足分は**正本カタログ**から必ず埋める。
    プレビュー音源の無いスロットはフロント側が「間奏」として扱う。

    Parameters
    ----------
    reserve:
        同じ番組の別パスで既に使った曲。ここにも重複させない。
        指定しないと、1 パス目の「いい日旅立ち」が 2 パス目にも出て
        1 回の放送の中で同じ曲が 2 回流れる（実測）。
    """
    if len(ordered_songs) >= required:
        return ordered_songs

    deficit = required - len(ordered_songs)
    target_year = year if year is not None else settings.default_year

    # 既に使った曲を正規化キーにして弾く（表記ゆれを吸収するため）
    used = set()
    for song in list(ordered_songs) + list(reserve or []):
        used.add(song_key(str(song.get("title", "")), str(song.get("artist", ""))))
    used.discard(song_key("", ""))

    try:
        extra = select_program_songs(target_year, count=deficit + len(used), exclude=used)
    except Exception:
        logger.exception(f"番組用曲の補完に失敗しました: year={target_year}")
        return ordered_songs

    for title, artist in extra:
        if len(ordered_songs) >= required:
            break
        if song_key(title, artist) in used:
            continue
        used.add(song_key(title, artist))
        ordered_songs.append({
            "title": title,
            "artist": artist,
            "preview_url": None,
            "artwork_url": None,
            "is_fallback": True
        })

    if len(ordered_songs) < required:
        logger.warning(
            f"番組の曲スロットを埋められませんでした: 必要{required}曲に対し"
            f"{len(ordered_songs)}曲のみ（{target_year}年の代替楽曲が足りません）"
        )
    return ordered_songs


def build_playlist(
    segments: List[ScriptSegment],
    songs: List[Dict[str, Any]],
    year: Optional[int] = None,
    reserve: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """曲で始まり曲で終わるラジオ番組のプレイリストを構築する。

    構成:
        オープニング曲 → オープニングトーク → 曲1 → トーク1 → 曲2 → … →
        トークN → エンディング曲

    実際のラジオ番組と同じ順序（テーマ曲 → DJトーク → 曲 → DJトーク → … → テーマ曲）に
    そろえる。旧実装は「トーク → 曲」だけだったため、番組の最初の一音が
    司会の声になり、オープニング曲もエンディング曲も構造上ありえなかった。

    **各トークを必ず1曲で挟む**ことで、LLM が何セグメントを返しても
      1. トーク同士が連続しない
      2. 末尾が「トーク2連続」にならない
    ことを構造的に保証する。曲が足りない分は FALLBACK 曲で埋める。

    `songs` は破壊しない（呼び出し元のレスポンス `songs` は `medley_song_count` のまま保つ）。
    """
    ordered_songs = list(songs or [])

    if not segments:
        # セグメントがない場合、曲だけ並べる
        return [_song_item(song, i) for i, song in enumerate(ordered_songs)]

    opening_segment, talk_segments, ending_segment = _classify_segments(segments)

    talk_order: List[ScriptSegment] = []
    if opening_segment is not None:
        talk_order.append(opening_segment)
    talk_order.extend(talk_segments)
    if ending_segment is not None and ending_segment is not opening_segment:
        talk_order.append(ending_segment)

    talk_total = len(talk_order)
    # オープニング曲 + トークN個 + エンディング曲 = トーク数 + 1 曲
    _top_up_songs_for_program(ordered_songs, talk_total + 1, year, reserve=reserve)

    playlist: List[Dict[str, Any]] = []
    song_idx = 0
    if len(ordered_songs) >= talk_total:
        # 曲で始めて曲で終わる（テーマ曲 → トーク → 曲 → … → エンディング曲）
        for segment in talk_order:
            playlist.append(_song_item(ordered_songs[song_idx], song_idx))
            song_idx += 1
            playlist.append(_talk_item(segment))
    else:
        # 曲が満たない場合はトーク優先（無音トークを作らない）
        for segment in talk_order:
            playlist.append(_talk_item(segment))
            if song_idx < len(ordered_songs):
                playlist.append(_song_item(ordered_songs[song_idx], song_idx))
                song_idx += 1
    # 曲が余った場合は末尾に並べる（トークの連続が起きない）
    while song_idx < len(ordered_songs):
        playlist.append(_song_item(ordered_songs[song_idx], song_idx))
        song_idx += 1

    return playlist

def _select_program_songs(year: int, count: int) -> List[Dict[str, Any]]:
    """1 番組ぶんの曲を選ぶ（**原稿とプレイリストの唯一の情報源**）。

    `SongSelector` は再生履歴を見てローテーションで選ぶため、
    同じ年を選んでも直前の放送と被らない。返した一覧を
    原稿（曲名を告げるため）とプレイリスト（実際に流すため）の
    両方へ渡すことで、「司会が A を告げるのに B が流れる」不整合を防ぐ。
    """
    try:
        history = SongHistoryStore(settings.song_store_path or None)
        selector = SongSelector(history=history)
        records = selector.select(year, count)
    except Exception:
        logger.exception(f"選曲に失敗しました（正本カタログへフォールバック）: year={year}")
        records = []
    if not records:
        records = SongSelector(history=None).peek(year, count)
    return records


# ==============================================================================
# ステップ単位の生成パイプライン（提案④・S5）
#
# `_build_generate_response` をステップ単位の関数に分割し、
# 各ステップの境界で `JobCancelled` を確認できるようにした。
# チェックは「cancellable な待ちの直前」にだけ置く（CPU バウンドな処理の
# 合間に置かない）。ステップの順序は UI の 4 ラベルと 1 対 1 で対応する。
# ==============================================================================
class _GenerationContext:
    """1 回の番組生成の作業領域。ステップ間で値を渡す。"""

    def __init__(
        self,
        req: GenerateRequest,
        tenant_id: str = "default",
        user_id: Optional[str] = None,
        job: Optional[Job] = None,
    ) -> None:
        self.req = req
        self.tenant_id = tenant_id or "default"
        self.user_id = user_id
        self.job = job
        self.loop_count = max(1, int(settings.program_loop_count))
        self.slots_per_pass = max(1, settings.program_min_song_count)
        self.selected_records: List[Dict[str, Any]] = []
        self.selected_pairs: List[tuple] = []
        self.script: str = ""
        self.segments: List[ScriptSegment] = []
        self.audio_url: Optional[str] = None
        self.song_list: List[Dict[str, Any]] = []
        self.enriched: List[Dict[str, Any]] = []
        self.passes: List[List[Dict[str, Any]]] = []
        self.playlist: List[Dict[str, Any]] = []
        self.per_pass_song_count: int = self.slots_per_pass
        self.quiz_data: Optional[List[Dict[str, str]]] = None

    # --- ジョブ連携 -------------------------------------------------------------
    def emit(self, name: str, **data: Any) -> None:
        if self.job is not None:
            self.job.emit(name, **data)

    def checkpoint(self, step: str) -> None:
        """ステップ境界でのキャンセル判定。"""
        if self.job is not None:
            self.job.checkpoint(step)
        else:
            raise_if_cancelled(step)

    def record(self, step: str, started_at: float) -> None:
        """ステップの実測時間を統計へ蓄積する（`estimated_ms` の入力）。"""
        jobs.registry.record_step(step, (time.monotonic() - started_at) * 1000.0)


def _step_select_songs(ctx: _GenerationContext) -> None:
    """ステップ 1: 選曲。**1 回だけ**呼ぶ（2 回選曲すると履歴を 2 倍消費する）。"""
    started = time.monotonic()
    planned_slots = ctx.slots_per_pass * ctx.loop_count
    ctx.selected_records = _select_program_songs(ctx.req.year, planned_slots)
    ctx.selected_pairs = [
        (str(r.get("title", "")), str(r.get("artist", ""))) for r in ctx.selected_records
    ]
    ctx.record("music", started)


def _step_generate_script(ctx: _GenerationContext) -> None:
    """ステップ 2: 原稿生成（実際に流れる曲名を渡す）。"""
    started = time.monotonic()
    ctx.emit(EVENT_SCRIPT_STARTED, year=ctx.req.year, month=ctx.req.month, day=ctx.req.day)
    # Gemini の tenacity リトライ境界は `time.sleep` 経由なので、
    # 待ち時間がそのままキャンセル対象になる（`install_cancellable_sleep`）。
    try:
        ctx.script = generate_radio_script(
            ctx.req.year,
            ctx.req.month,
            ctx.req.day,
            mode=ctx.req.mode,
            target_name=ctx.req.target_name,
            songs=ctx.selected_pairs or None,
        )
    except AppError:
        # AppError はユーザー向けメッセージと原因区分を既に持っているので、
        # ここで潰すと app_error_handler の判定が死に、常に 400 になってしまう。
        logger.exception("スクリプト生成エラー(AppError)")
        raise
    except JobCancelled:
        raise
    except Exception as e:
        # 技術的な詳細はログのみ。用户には例外文字列をそのまま返さない
        logger.exception(f"スクリプト生成エラー: {e}")
        raise ScriptGenerationError(
            "原稿の生成に失敗しました。時間をおいて再度お試しください。", original=e
        ) from e
    ctx.record("script", started)
    ctx.emit(EVENT_SCRIPT_DONE, chars=len(ctx.script))


def _step_parse_script(ctx: _GenerationContext) -> None:
    """ステップ 3: 原稿をセグメントにパース（見出しが無ければ空リスト）。"""
    ctx.segments = parse_script_segments(ctx.script)
    if not ctx.segments:
        logger.warning("原稿からトークセグメントを抽出できませんでした")


def _step_tts(ctx: _GenerationContext) -> None:
    """ステップ 4: セグメント TTS → ステップ 5: 全体版 TTS。"""
    started = time.monotonic()
    ctx.segments = generate_tts_for_segments(
        ctx.segments, tenant_id=ctx.tenant_id, job=ctx.job
    )
    # 全体用のTTS音声も生成（後方互換性のため）
    # コストを許容できない環境では RETRO_RADIO_FULL_SCRIPT_TTS=0 で無効化できる。
    ctx.audio_url = None
    if _env_flag("RETRO_RADIO_FULL_SCRIPT_TTS", True):
        try:
            tts_script = clean_script_for_tts(ctx.script)
            audio_filename = generate_tts_cached(tts_script, tenant_id=ctx.tenant_id)
            ctx.audio_url = _audio_url_for(ctx.tenant_id, audio_filename)
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f"全体音声合成処理失敗: {e}")
            ctx.audio_url = None
    else:
        logger.info("RETRO_RADIO_FULL_SCRIPT_TTS が無効のため全体版TTSをスキップします")
    ctx.record("tts", started)
    ctx.emit(EVENT_TTS_DONE, segments=len(ctx.segments))


def _enrich_with_checkpoints(
    records: List[Dict[str, Any]], cache: PreviewCache
) -> List[Dict[str, Any]]:
    """iTunes の予算ループに**曲ごとのキャンセル境界**を作る。

    `enrich_songs` は一括に全曲を解決するため、予算ループ中では
    キャンセルを検知できない。1 曲ずつ呼べば境界が 1 曲ごとにできる。
    1 曲あたりの解決結果（dict の形）は変わらない。
    """
    out: List[Dict[str, Any]] = []
    for record in records:
        raise_if_cancelled("music.resolve")
        out.extend(enrich_songs([record], cache=cache))
    return out


def _step_music(ctx: _GenerationContext) -> None:
    """ステップ 6: 楽曲の音源 URL を解決する。

    互換フィールド `songs` と `playlist` は**同一の `enriched`** から構成する。
    1 パス分の `enriched` 先頭がそのまま `passes[0]` に入るため、
    「司会が告げる曲」と「流れる曲」が必ず一致する。
    """
    started = time.monotonic()
    needed_song_count = max(1, len(ctx.segments) + 1)
    ctx.per_pass_song_count = max(needed_song_count, ctx.slots_per_pass)
    ctx.emit(EVENT_MUSIC_STARTED, planned=ctx.per_pass_song_count * ctx.loop_count)
    try:
        chosen = ctx.selected_records[: ctx.per_pass_song_count * ctx.loop_count]
        cache = PreviewCache(settings.song_store_path or None)
        ctx.enriched = _enrich_with_checkpoints(chosen, cache) if chosen else []
        ctx.song_list = _to_song_dicts(ctx.enriched[: settings.medley_song_count])
    except JobCancelled:
        raise
    except Exception as e:
        logger.error(f"楽曲検索エラー: {e}")
        fallback_title, fallback_artist = get_fallback_song(ctx.req.year)
        ctx.song_list = [{
            "title": fallback_title,
            "artist": fallback_artist,
            "preview_url": None,
            "artwork_url": None,
            "is_fallback": True,
        }]
        ctx.enriched = []
    ctx.record("music", started)
    ctx.emit(EVENT_MUSIC_DONE, count=len(ctx.enriched))


def _step_playlist(ctx: _GenerationContext) -> None:
    """ステップ 7: プレイリスト構築（曲で始まり曲で終わるラジオ番組の構成）。"""
    ctx.passes = []
    per_pass = ctx.per_pass_song_count
    for index in range(ctx.loop_count):
        chunk = _to_song_dicts(
            ctx.enriched[index * per_pass:(index + 1) * per_pass]
        )
        # 既に他のパスで使った曲はこのパスでは使わない
        elsewhere = [
            item
            for other, item in enumerate(ctx.enriched)
            if not (index * per_pass <= other < (index + 1) * per_pass)
        ]
        ctx.passes.append(
            build_playlist(ctx.segments, chunk, year=ctx.req.year, reserve=elsewhere)
        )
    ctx.playlist = ctx.passes[0] if ctx.passes else []
    ctx.emit(EVENT_PLAYLIST_DONE, playlist_len=len(ctx.playlist))


def _step_quiz(ctx: _GenerationContext) -> None:
    """ステップ 8: 回想法モード時のクイズデータ。"""
    ctx.quiz_data = None
    if ctx.req.mode == "care_recreation":
        ctx.quiz_data = get_reminiscence_quiz(ctx.req.year)


#: 実行するステップ（境界ごとにキャンセル判定する）。
GENERATION_STEPS = (
    _step_select_songs,
    _step_generate_script,
    _step_parse_script,
    _step_tts,
    _step_music,
    _step_playlist,
    _step_quiz,
)


def _build_generate_response(
    req: GenerateRequest,
    tenant_id: str = "default",
    user_id: Optional[str] = None,
    job: Optional[Job] = None,
) -> GenerateResponse:
    """1 回の番組生成を組み立てる（**同期 / 旧 API と新 API で共有する本体**）。

    `POST /api/generate`（互換）と `POST /api/jobs`（非同期）は
    **この 1 つの実装を共有する**。重複実装を作らない。
    """
    ctx = _GenerationContext(req, tenant_id=tenant_id, user_id=user_id, job=job)
    for step in GENERATION_STEPS:
        # チェックポイント（5）。ステップ境界。
        # cancellable な待ち（throttle / tenacity / backoff / 曲解決）の直前に
        # 検知されれば、待たずに次の境界へ進める。
        ctx.checkpoint(step.__name__)
        step(ctx)
    return GenerateResponse(
        year=req.year,
        month=req.month,
        day=req.day,
        mode=req.mode,
        script=ctx.script,
        audio_url=ctx.audio_url,
        songs=ctx.song_list,
        song=ctx.song_list[0] if ctx.song_list else None,
        reminiscence_quiz=ctx.quiz_data,
        target_name=req.target_name,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        segments=[s.to_dict() for s in ctx.segments] or None,
        program_guide=HistoricalRadioPrograms.get_program_guide(
            req.year, req.month, req.day
        ).to_dict(),
        playlist=ctx.playlist,
        passes=ctx.passes,
        loop_count=ctx.loop_count,
    )


# ==============================================================================
# ルート
# ==============================================================================
# 同期 def にしたのは意図的。Gemini / gTTS / iTunes はいずれもブロッキングHTTPなので、
# async のまま呼ぶとイベントループが数秒ブロックされる。FastAPI は同期ハンドラをスレッドプールで実行する。
@app.post("/api/generate", response_model=GenerateResponse)
def generate_radio(
    req: GenerateRequest,
    principal: Principal = Depends(tenant_principal),
):
    """ラジオ番組生成API（既存の同期版。互換のために残す）"""
    logger.info(f"番組生成リクエスト受信: year={req.year}, month={req.month}, day={req.day}, mode={req.mode}")

    if not _generation_slots.acquire(timeout=settings.generation_wait_timeout):
        logger.warning("番組生成の同時実行上限に達しました")
        raise HTTPException(status_code=503, detail="混雑しています。しばらく待ってから再度お試しください。")

    try:
        return _build_generate_response(
            req, tenant_id=principal.tenant_id, user_id=principal.user_id
        )
    finally:
        # 必ず解放する。`finally` を外すと失敗時にデッドロックする。
        _generation_slots.release()
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="completed",
            outcome="success",
            meta={"path": "generate", "year": req.year},
        )


# --- 提案④: 非同期ジョブ API ------------------------------------------------------
def _require_job(job_id: str, principal: Principal) -> Job:
    """ジョブをテナント照合付きで取り出す。**他テナントには 404** で見せない。"""
    job = jobs.registry.get(job_id)
    if job is None or job.tenant_id != principal.tenant_id:
        logger.info("ジョブが見つからないかテナント不一致: job_id=%s", job_id)
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _run_job(job: Job, req: GenerateRequest, principal: Principal) -> GenerateResponse:
    """**専用ワーカースレッド**で 1 ジョブを走らせる本体。

    スロットは `try/finally` で確実に解放する。キャンセル例外が飛んでも、
    失敗しても、**必ず** `release()` に到達する。
    """
    if not _generation_slots.acquire(timeout=settings.generation_wait_timeout):
        logger.warning("番組生成の同時実行上限に達しました（ジョブ）: job_id=%s", job.job_id)
        raise HTTPException(status_code=503, detail="混雑しています。しばらく待ってから再度お試しください。")
    try:
        response = _build_generate_response(
            req, tenant_id=principal.tenant_id, user_id=principal.user_id, job=job
        )
        job.emit(EVENT_DONE, playlist_len=len(response.playlist or []))
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="completed",
            outcome="success",
            meta={"job_id": job.job_id, "path": "jobs", "year": req.year},
        )
        return response
    except JobCancelled:
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="cancelled",
            outcome="denied",
            meta={"job_id": job.job_id, "path": "jobs", "year": req.year},
        )
        raise
    except HTTPException as exc:
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="failed",
            outcome="failure",
            meta={"job_id": job.job_id, "path": "jobs", "status": exc.status_code},
        )
        job.emit(
            EVENT_FAILED,
            reason=type(exc).__name__,
            retryable=exc.status_code in (429, 500, 502, 503, 504),
            status=exc.status_code,
        )
        job.fail("busy", exc.status_code in (429, 500, 502, 503, 504), detail=str(exc.detail))
        return GenerateResponse(
            year=req.year, month=req.month, day=req.day, mode=req.mode,
            script="", audio_url=None, songs=[],
            generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
    except AppError as exc:
        retryable = not isinstance(exc, (TTSError, ConfigurationError))
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="failed",
            outcome="failure",
            meta={"job_id": job.job_id, "path": "jobs", "reason": type(exc).__name__},
        )
        job.emit(EVENT_FAILED, reason=type(exc).__name__, retryable=retryable)
        job.fail(type(exc).__name__, retryable, detail=exc.user_message)
        raise
    finally:
        # キャンセル・失敗・成功のどれでも必ずここを通る。
        _generation_slots.release()


@app.post("/api/jobs", status_code=202)
def create_job(
    req: GenerateRequest,
    principal: Principal = Depends(tenant_principal),
):
    """番組生成を**非同期ジョブ**として受け付ける（202 Accepted）。

    リクエストボディは既存 `POST /api/generate` と同一（`GenerateRequest`）。
    応答は `202 {job_id, estimated_ms, poll_after_ms}`。
    `estimated_ms` は**そのキャッシュ状態から**計算する（`jobs.estimate_generation_ms`）。
    """
    job = jobs.new_job(
        jobs.registry, principal.tenant_id, req.model_dump(), settings
    )
    job.emit(EVENT_ESTIMATE, **job.estimate.to_dict() if job.estimate else {})
    _record_generation_audit(
        principal.tenant_id,
        principal.user_id,
        phase="started",
        outcome="success",
        meta={"job_id": job.job_id, "path": "jobs", "year": req.year},
    )
    thread = jobs.start_worker(
        job, lambda j: _run_job(j, req, principal)
    )
    job.worker = thread
    return JSONResponse(
        status_code=202,
        content={
            "job_id": job.job_id,
            "estimated_ms": job.estimated_ms,
            "poll_after_ms": job.poll_after_ms,
            "state": job.state,
            "events_url": f"/api/jobs/{job.job_id}/events",
        },
    )


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, principal: Principal = Depends(tenant_principal)):
    """ジョブの状態を返す（ポーリング用）。SSE が使えない環境の保険。"""
    job = _require_job(job_id, principal)
    snapshot = job.snapshot()
    # 生成結果本身は `result` に丸ごと入っているので、SSE を使えないクライアントも
    # ここ 1 回で全部取れる（重複実装を作らない）。
    return snapshot


@app.delete("/api/jobs/{job_id}")
def cancel_job(job_id: str, principal: Principal = Depends(tenant_principal)):
    """ジョブを**協調的にキャンセル**する。

    サーバは即座に殺さない。次の cancellable な待ち（gTTS の間隔・429 の backoff・
    Gemini の指数バックオフ・iTunes の曲解決）で止まり、`finally` で
    **スロットが確実に解放される**。
    """
    job = _require_job(job_id, principal)
    accepted = job.request_cancel()
    _record_generation_audit(
        principal.tenant_id,
        principal.user_id,
        phase="cancel_requested",
        outcome="denied" if accepted else "failure",
        meta={"job_id": job.job_id, "path": "jobs", "accepted": accepted},
    )
    return {
        "job_id": job.job_id,
        "cancel_requested": accepted,
        "state": job.state,
    }


def _last_event_id(request: Request) -> int:
    """SSE の再開位置（`Last-Event-ID` ヘッダー / クエリ）。"""
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        return int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        return 0


def _is_terminal_event(name: str) -> bool:
    return name in (EVENT_DONE, EVENT_FAILED, EVENT_CANCELLED)


async def _sse_stream(job: Job, request: Request):
    """SSE のイベント列を流す非同期ジェネレータ。

    技術要件:

    * `text/event-stream` / `Cache-Control: no-cache` / `X-Accel-Buffering: no`
      （ヘッダーは `StreamingResponse` 側で付与する）
    * keep-alive コメントを一定間隔で挟む（プロキシの idle タイムアウト対策）
    * `Last-Event-ID` で**途中から再開できる**（`seq` が単調増加するため）
    * ジョブが終端したら**有限に終わる**（無限に開いたままにしない）
    * クライアント切断時は `finally` で確実にクリーンアップする
    """
    after = _last_event_id(request)
    loop = asyncio.get_running_loop()
    deadline = time.monotonic() + jobs.SSE_MAX_SECONDS
    try:
        while True:
            try:
                if await request.is_disconnected():
                    job.client_gone.set()
                    return
            except Exception:  # noqa: BLE001 - 切断判定の失敗でストリームを落とさない
                pass
            pending = await loop.run_in_executor(
                None, job.wait_for_events, after, jobs.SSE_KEEPALIVE_SECONDS
            )
            if not pending:
                yield jobs.sse_comment()
                if time.monotonic() > deadline:
                    yield jobs.sse_comment("stream-timeout")
                    return
                continue
            for event in pending:
                yield event.to_sse()
                after = event.seq
                if _is_terminal_event(event.name):
                    return
            if job.is_finished:
                return
    finally:
        # 購読者が居なくなってもジョブ自体は止めない（ポーリングで拾えるように）。
        job.client_gone.set()


@app.get("/api/jobs/{job_id}/events")
async def stream_job_events(
    job_id: str,
    request: Request,
    principal: Principal = Depends(tenant_principal),
):
    """ジョブの進捗を Server-Sent Events で配信する。

    イベント種別: `script.started` / `script.done` / `tts.segment` / `tts.done` /
    `music.search.done` / `done` / `failed` / `cancelled`。
    UI の既存ラベル（電波を受信中 / 原稿を書く / 読み上げる / ヒット曲を送る）は
    `jobs.UI_LABEL_BY_EVENT` で 1 対 1 に写像できる。
    """
    job = _require_job(job_id, principal)
    return StreamingResponse(
        _sse_stream(job, request),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx などのリバースプロキシのバッファリングを無効化する。
            "X-Accel-Buffering": "no",
        },
    )


def _serve_audio_file(resolved: Path, filename: str) -> FileResponse:
    return FileResponse(
        resolved,
        media_type="audio/mpeg",
        filename=filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )


def _validate_audio_file(resolved: Optional[Path], filename: str) -> Path:
    """3 層防御（拡張子 → サイズ → マジックバイト）をまとめて適用する。"""
    if resolved is None:
        raise HTTPException(status_code=404, detail="Audio file not found")
    try:
        size = resolved.stat().st_size
    except OSError:
        raise HTTPException(status_code=404, detail="Audio file not found")
    if size < AUDIO_MIN_BYTES:
        logger.warning(f"小さすぎる音声ファイルを拒否しました: {filename!r} ({size} bytes)")
        raise HTTPException(status_code=404, detail="Audio file not found")
    try:
        with resolved.open("rb") as handle:
            head = handle.read(3)
    except OSError:
        raise HTTPException(status_code=404, detail="Audio file not found")
    if not _is_mp3_header(head):
        logger.warning(f"MP3として不正な音声ファイルを拒否しました: {filename!r} head={head!r}")
        raise HTTPException(status_code=404, detail="Audio file not found")
    return resolved


@app.get("/api/audio/{filename}")
async def get_audio(filename: str):
    """生成されたTTS音声のストリーミング配信（**従来のフラット URL**）。

    CACHE_DIR 直下に置かれた個人モードのキャッシュを返す。
    3 層防御（拡張子 → サイズ → マジックバイト）は維持する。
    認証有効時は `relative_url_for` がテナント付き URL を返すため、
    新規クライアントはこのルートを叩かない。
    """
    if not AUDIO_FILENAME_PATTERN.match(filename):
        logger.warning(f"拒否された音声ファイル名: {filename!r}")
        raise HTTPException(status_code=404, detail="Audio file not found")

    try:
        resolved = (CACHE_DIR / filename).resolve(strict=True)
        cache_root = CACHE_DIR.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="Audio file not found")

    # basename の直後に 1 枚ガード（シンボリックリンク / .. でのディレクトリ脱出行も排除）
    if not resolved.is_relative_to(cache_root) or not resolved.is_file():
        logger.warning(f"キャッシュ領域外を指す音声パスを受理しませんでした: {filename!r}")
        raise HTTPException(status_code=404, detail="Audio file not found")

    return _serve_audio_file(_validate_audio_file(resolved, filename), filename)


@app.get("/api/audio/{tenant_id}/{filename}")
async def get_tenant_audio(
    tenant_id: str,
    filename: str,
    tenant: str = Depends(audio_tenant_dependency),
):
    """テナント対応の音声配信（S4 の `audio_tenant` で**テナントを照合**）。

    他テナントのファイル名は 404 に見せる（存在を漏らさない）。
    3 層防御はこのルートでも同じものを適用する。
    """
    resolved = _cache().resolve(tenant, filename)
    if resolved is None:
        raise HTTPException(status_code=404, detail="Audio file not found")
    return _serve_audio_file(_validate_audio_file(resolved, filename), filename)


@app.get("/api/decades")
async def get_decades():
    """利用可能な年代一覧"""
    # 1950から2025年まで10年刻みに生成
    decades = list(range(settings.min_year, settings.max_year + 1, 10))
    # 2025を含むように調整
    if settings.max_year not in decades:
        decades.append(settings.max_year)
    return {
        "decades": sorted(list(set(decades))),  # 重複を除去してソート
        "default_year": settings.default_year
    }

@app.get("/health")
async def health(principal: Principal = Depends(optional_principal)):
    # APIキーの状態もヘルスチェックに含める
    has_api_key = bool(settings.gemini_api_key)
    return {
        "status": "healthy" if has_api_key else "degraded",
        "service": "Retro Radio Time Machine",
        "version": settings.app_version,
        "api_key_configured": has_api_key,
        "secret_key_configured": bool(settings.secret_key),
        # --- 提案⑧・S4: 認証の状態（既定は True = 認証必須）---
        "auth_required": bool(settings.require_auth),
        "auth_ready": bool(settings.auth_ready),
        "auth_mode": principal.auth_mode,
        "auth_enforced": _auth_enforced(),
    }

# 静的ファイル配信
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Retro Radio Time Machine API Server</h1><p>Frontend static files loading...</p>")
