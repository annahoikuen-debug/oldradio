import os
import re
import hmac
import hashlib
import asyncio
import tempfile
import logging
import threading
import time
from typing import Optional, List, Dict, Any, Literal, Tuple
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, date

from fastapi import FastAPI, HTTPException, Request, Response, Depends, status
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
from .core.script_generator import (
    _deterministic_script,
    generate_radio_script,
    parse_script_segments,
)
from .core.music_search import enrich_songs
from .core.preview_resolver import store_link
from .core.song_selector import SongSelector
from .services.song_store import PreviewCache, SongHistoryStore
from .core import legacy_tts
from .core import tts_engines
from .core.fallback import get_fallback_song, get_reminiscence_quiz, HistoricalRadioPrograms, select_program_songs
from .core.songs import song_key, songs_for_year
from .utils.logging_config import setup_logging
from .utils.text_cleaner import clean_script_for_tts

# --- 提案④・S5: 非同期ジョブ / 協調的キャンセル ---------------------------------
from . import jobs
from .jobs import (
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
from .api.me import MAX_TARGET_NAME_LENGTH, TargetNameError, normalize_target_name
from .api.deps import (
    AUTH_UNAVAILABLE_DETAIL,
    audio_tenant as audio_tenant_dependency,
    optional_principal,
    require_tenant,
    settings_dependency,
)
from .auth.tokens import (
    DEFAULT_SESSION_TTL_SECONDS,
    SESSION_COOKIE_NAME,
    Principal,
    TokenError,
    extract_bearer,
    issue_session_token,
    read_bearer_token,
    resolve_mode,
)
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
#   * blob:          : app.js の object URL の読み込みと POST 応答の曲 URL の絶対 URL 生成
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


# R2-04: Stripe webhook のボディ上限（バイト）。署名検証前に検査する。
WEBHOOK_BODY_MAX_BYTES = 1_000_000


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

#: `POST /api/jobs` の**入場制御**。
#:
#: かつては入場制御が無く、202 を返すハンドラが毎回 `threading.Thread` を
#: 作って返していた。**同時実行数（`_generation_slots`）はワーカーの中でしか
#: 取得しない**ため、キュー待ちするジョブも 1 本ずつスレッドを掴む。
#: ハンドラは 202 を返すだけなので耐久はリクエスト処理数で決まるが、
#: 攻撃者が `POST /api/jobs` を連打すると、**`generation_wait_timeout`
#: （既定 30 秒）ブロックされたスレッド**が数百本同時に立ち上がる。
#: それぞれが Job と Event リストとリクエストボディを持つ。
#:
#: そこで**入場を同期的に**許可し、埋まったら `/api/generate` と同じ
#: 503 で断る。枠は「生成中 + 待機中」をまとめて上から数える。
#:
#: **枠はスレッドが生きているあいだ保持する**（`start_worker` の `on_finish`）。
#: 生成枠を待つ前に返すと、20 rps の `POST /api/jobs` で「30 秒何もせず
#: ブロックする daemon スレッド」が数百本同時に立ち上がり、OOM で落ちる。
_JOB_QUEUE_LIMIT = max(2, settings.max_concurrent_generations * 4)
_job_queue_slots = GenerationSlots(_JOB_QUEUE_LIMIT)


class _QueueTicket:
    """入場枠 1 枚を表すチケット（JOB-08 の修正）。

    かつてはプロセスグローバルのカウンタ（``_job_queue_held``）で
    「取得済み枚数」を管理していたが、複数リクエストが絡むと
    誰の枠か分からず二重解放 / 枠リークの温床になっていた。
    チケット方式では **1 枚ごとに個別のオブジェクト**が解放を担うため、
    解放は自然に冪等かつリークしない。
    """

    __slots__ = ("_released", "_slots")

    def __init__(self, slots: "GenerationSlots") -> None:
        self._released = False
        self._slots = slots

    def release(self) -> None:
        """入場枠を返す。**冪等**（2 回呼んでも 1 回だけ戻る）。"""
        if self._released:
            return
        self._released = True
        self._slots.release()


def _acquire_job_queue_slot() -> Optional[_QueueTicket]:
    """`POST /api/jobs` の入場枠を**非同期的に**取る（取れなければ ``None``）。

    非同期（`blocking=False`）で取る。要求を待たせてはいけない。
    空きが無い場合は 503 で即座に返す。
    """
    if not _job_queue_slots.acquire(blocking=False):
        return None
    return _QueueTicket(_job_queue_slots)


_cache_lock = threading.Lock()
_sweep_counter = 0

#: ユーザーごとに保持する生成履歴の件数（`generations` 1 行 = 原稿全文）。
#:
#: `generations` は原稿全文（`anniversary` では `target_name` を含む）を保持し、
#: 書き込むたびに 1 行増える。剪定経路は削除請求（`DELETE /api/me`）しか無いため、
#: ここで上限を決める。無制限だと既定の SQLite ボリュームが膨張する。
GENERATION_HISTORY_KEEP = 20

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

    .. warning::
       **「認証を有効だと思った」と「認証を無効と判定した」が
       食い違う構成では 503 で落とす（fail-closed）。**

       `_auth_enforced()` は環境変数を優先して読み、
       `require_tenant` は `lru_cache` 済みの `Settings` を見る。
       両者が**別々の真実**を指すため、矛盾が起きうる（実測:
       `Settings` の `dependency_overrides` や `get_settings.cache_clear()`）。

       **判定は「両方とも認証不要」を要求するときだけ** 匿名を通す。
       此前は `_auth_enforced()` が False の時点で早期 return していたため、
       `RETRO_RADIO_REQUIRE_AUTH=0`（環境変数）なのに
       `Settings.require_auth=True`（注入値）の**逆向きの矛盾**では
       `resolve_mode` を確認する前に匿名 principal を素通りさせていた（fail-open）。

       この分岐を削除して `Settings` 注入に一本化することは大きいため、
       当面は矛盾を**検出して 503** にする。
    """
    # 先に `_auth_enforced()` を評価し、**次に**注入された Settings と突き合わせる。
    # 順序を逆にすると、逆向きの矛盾（env=0 / Settings=True）で
    # 矛盾検出に到達する前に匿名 principal を素通りしてしまう。
    #
    # 判定の式は 1 本に畳む:
    #     認証が要る（env）      != 認証が要る（Settings）  → 矛盾 → 503
    #     両方とも認証が要らない                          → 個人モード
    #     両方とも認証が要る                              → `require_tenant` に委譲
    env_requires_auth = _auth_enforced()
    settings_mode = resolve_mode(current_settings)
    settings_requires_auth = settings_mode != "disabled"

    if env_requires_auth != settings_requires_auth:
        logger.error(
            "認証設定が矛盾しています: 環境変数は %s を要求しているのに、"
            "注入された Settings は %s です。どちらかが fail-open の原因になるため"
            "503 で拒否します。RETRO_RADIO_REQUIRE_AUTH と Settings.require_auth を"
            "一致させてください。",
            "認証必須" if env_requires_auth else "認証不要",
            settings_mode,
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=AUTH_UNAVAILABLE_DETAIL,
        )

    if not env_requires_auth:
        # 上の矛盾検出を通過しているので、ここに来るのは
        # 「**両方とも**認証不要」を要求したときのみ。
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


def _measured_duration_seconds(
    tenant_id: Optional[str], filename: str, fallback: float
) -> float:
    """生成済み mp3 の**実測** duration（秒）を返す（提案③-4）。

    `estimated_duration` は「3.0 文字/秒」の推定であり、UI が表示する残秒が
    実測とずれる原因だった。mutagen で実測し、読めない（mp3 が無い等）ときは
    ±20% の保守値（推定 × 1.2）にフォールバックする。追加の HTTP 呼び出しはゼロ。
    """
    try:
        from mutagen.mp3 import MP3

        path = _tenant_cache_dir(tenant_id, create=False) / filename
        if path.exists():
            duration = float(MP3(str(path)).info.length)
            if duration > 0:
                return round(duration, 2)
    except Exception as e:  # pragma: no cover - 読み取り失敗は保守値で運用継続
        logger.debug(f"duration 実測に失敗したため保守値へフォールバック: {e}")
    try:
        return round(float(fallback) * 1.2, 2)
    except (TypeError, ValueError):
        return 0.0


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
    1 回の生成につき「開始（`started`）」と「終端（`completed` / `failed` /
    `cancelled`）」で 2 行書くため、
    「生成イベント数に対するログ行数」は 200%（= 漏れ 0）になる。
    **終端行の phase / outcome は実際に起きた結果に従う**
    （失敗した生成を `completed` / `success` として記録しない）。
    `meta` には原稿本文・氏名・対象年（生年相当）を入れない。
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


def _enforce_flat_cache_max_files(
    entries: Optional[List[Tuple[Path, float]]] = None,
) -> int:
    """`CACHE_DIR` 直下のキャッシュファイル数を `tts_cache_max_files` に収める（CACHE-01）。

    **TTL 内のファイルは削除しない。** ここで消されたファイル名は既に
    `playlist[].audio_url` で返し、`generations.audio_path` に保存されている。
    TTL（既定7日）を待たずに消すと、過去に生成済みの番組が放送中に 404 になる。

    そのため「期限切れ」を主条件にし、個数上限は**期限切れファイルの中でのみ**
    効かせる。期限切れがまだ 0 件なら、どれだけファイルが増えても残す。
    （ディスク枯渇が心配な場合は `tts_cache_ttl_days` を短くする運用をする。）

    Parameters
    ----------
    entries:
        呼び出し側が既に集めた `(entry, mtime)` 一覧。渡すと `CACHE_DIR` を
        読み直さない（1 回のスイープで走査を 1 回に抑える）。
    """
    max_files = settings.tts_cache_max_files
    if max_files <= 0 or not CACHE_DIR.is_dir():
        return 0
    if entries is None:
        try:
            entries = _flat_cache_entries()
        except OSError:
            return 0

    expired = sorted(
        (pair for pair in entries if pair[1] < _tts_cache_cutoff()),
        key=lambda pair: pair[1],
    )
    excess = len(expired) - max_files
    if excess <= 0:
        return 0
    removed = 0
    for entry, _mtime in expired[:excess]:
        try:
            entry.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info(
            "キャッシュ個数上限超過のため期限切れのTTSキャッシュを削除しました: %d件"
            "（上限=%d / TTL=%.1f日）",
            removed,
            max_files,
            settings.tts_cache_ttl_days,
        )
    return removed


def _flat_cache_entries() -> List[Tuple[Path, float]]:
    """`CACHE_DIR` 直下のキャッシュファイルと更新時刻を返す（走査 1 回分）。"""
    entries: List[Tuple[Path, float]] = []
    for entry in CACHE_DIR.iterdir():
        try:
            if entry.is_file() and entry.suffix in (".mp3", ".tmp"):
                entries.append((entry, entry.stat().st_mtime))
        except OSError:
            continue
    return entries


def _tts_cache_cutoff() -> float:
    """TTS キャッシュの期限切れ基準時刻（この時刻より古いものを消す）。"""
    return time.time() - settings.tts_cache_ttl_days * 86400


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
    cutoff = _tts_cache_cutoff()
    removed = 0
    # ディレクトリは 1 回だけ走査し、TTL 削除と個数上限の両方で使い回す。
    try:
        entries = _flat_cache_entries()
    except OSError as e:
        logger.warning(f"TTSキャッシュの整理に失敗しました: {e}")
        return 0
    try:
        for entry, mtime in entries:
            if mtime < cutoff:
                entry.unlink()
                removed += 1
    except OSError as e:
        logger.warning(f"TTSキャッシュの整理に失敗しました: {e}")
    # 個数上限（CACHE-01）: TTL 内にファイルが無制限に蓄積するのを防ぐ。
    # 既に集めた一覧を渡すので、ここで `CACHE_DIR` を読まなくなる。
    removed += _enforce_flat_cache_max_files(entries)
    if _auth_enforced():
        # 認証有効時はテナントディレクトリも掃除する（S4 の `sweep`）。
        removed += _cache().sweep(tenant_id=None, force=force)
    if removed:
        logger.info(f"古いTTSキャッシュを削除しました: {removed}件")
    return removed


def _require_database_schema() -> None:
    """起動時にDBのスキーマが既にあることを**確認だけ**して、無ければ起動を落とす。

    以前は `Authenticator.__init__` が毎リクエストで `init_db()` を
    （`create_all()` による DDL 付きで）呼んでいた。これが 2 つの欠陥を生んでいた:

    1. **Alembic を恒久的に壊す。** `create_all()` は `alembic_version` を
       書き換えないので、先に走らせるとその後の
       ``alembic upgrade head`` が ``table tenants already exists`` で
       失敗し続け、**解決しない**（実測）。
    2. **認証前の攻撃者が DDL を起こせる。** `POST /api/auth/session` は
       認証前なので、資格情報を連打するだけで無認証で DDL が走り、
       SQLite のスキーマロックを in-flight の監査ログ書き込みと奪い合う。

    ここでは**何も作らない**。スキーマの所有者は Alembic 一本であり、
    適用はデプロイの start command（`Dockerfile` の `CMD`、
    `fly.toml` / `render.yaml` / `railway.json` の `startCommand`）が
    `alembic upgrade head` で行う。ローカル開発も
    `alembic upgrade head`（または `python scripts/init_db.py`）を先に
    実行する。

    .. note::
       ここで**プロセス内で Alembic を実行しない**のは意図的です。
       `db/migrations/env.py` の ``fileConfig()`` は root logger の
       handler を**置き換えてしまう**ため、アプリ起動時に migration を
       回すとアプリの JSON ログ出力が黙ります（実測）。
       migration を自動化したいなら start command 側の責務です。
    """
    from .db.session import schema_is_ready

    try:
        ready = schema_is_ready()
    except Exception as e:  # noqa: BLE001 - 接続できない場合も起動させない
        logger.exception(f"DB のスキーマを確認できませんでした: {e}")
        raise

    if ready:
        return

    raise RuntimeError(
        "データベースのスキーマが未準備です。`alembic upgrade head`（または "
        "`python scripts/init_db.py`）を先に実行してください。"
        "スキーマ不足のまま起動させると、認証・監査の経路が `no such table` "
        "で 500 になります。"
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    _ensure_cache_dir()
    _sweep_tts_cache(force=True)
    _require_database_schema()
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
# 認証を保護的に差し込んだので、me / audit を公開する。
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
    # 既定は 1月1日。「今日」（実行当日）にすると month 省略時に 2月・4月・6月・9月・11月で
    # 「その月の31日は存在しない」→ 422 になる（実測: 今日は30日のため2月は422）
    month: int = Field(default=1, ge=1, le=12)
    day: int = Field(default=1, ge=1, le=31)
    mode: RadioMode = Field(default="normal", description="モード: normal | care_recreation | anniversary")
    # R2-07: 上限は `api.me.MAX_TARGET_NAME_LENGTH`（= 16）に**一本化**する。
    # 以前は 64 文字だったため、UI 契約（64）とプライバシー目標（16）が
    # 食い違い、64 文字まで本名が記録され得た。
    # 16 文字は「ニックネーム（呼称）」として現実的な長さであり、
    # `api.me.normalize_target_name` をそのまま呼んで同じ制約にする。
    target_name: Optional[str] = Field(
        default=None,
        max_length=MAX_TARGET_NAME_LENGTH,
        description=(
            f"記念日ギフト用の対象者名"
            f"（最大{MAX_TARGET_NAME_LENGTH}文字・本名ではなくニックネーム）"
        ),
    )

    @field_validator("target_name")
    @classmethod
    def validate_target_name(cls, value: Optional[str]) -> Optional[str]:
        """対象名を**1 か所**の規則（`api.me.normalize_target_name`）で検証する。

        この値は原稿の f-string と `### 見出し` パーサ（CORE）の入力になるため、
        改行・制御文字・見出しマーカーで構造を乗っ取れないようにする。
        """
        if value is None:
            return value
        try:
            # 空白除去・制御文字/`###`/長さの制約をすべてここに集約する。
            return normalize_target_name(value)
        except TargetNameError as exc:
            raise ValueError(str(exc))

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

def _tts_edge_options() -> tts_engines.EdgeTTSOptions:
    return tts_engines.EdgeTTSOptions(
        voice=settings.tts_edge_voice,
        rate=settings.tts_edge_rate,
        pitch=settings.tts_edge_pitch,
        volume=settings.tts_edge_volume,
    )


def _active_tts_engine() -> str:
    """実際に使うエンジン名（`auto` を解決した結果）。"""
    return tts_engines.resolve_engine(settings.tts_engine)


def _tts_cache_filename(text: str, engine: Optional[str] = None) -> str:
    """キャッシュファイル名。**音声設定（エンジンと声の属性）をキーに含める**。

    ``engine`` を明示しない場合は実際のエンジン（`auto` を解決したもの）を使う。
    gTTS へフォールバックするときは ``ENGINE_GTTS`` を明示して呼び出す。
    そうしないと、**フォールバックの成果物が edge のキーで保存され**、
    edge が回復しても「キャッシュヒット」になって再合成されない。
    """
    engine = engine or _active_tts_engine()
    if engine == tts_engines.ENGINE_EDGE:
        voice_key = _tts_edge_options().cache_token()
    else:
        voice_key = f"{settings.tts_language}_{settings.tts_tld}_{settings.tts_slow}"
    cache_key = f"{text}_{engine}_{voice_key}"
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
    """今 TTS が実物のネットワーククライアントかどうか。

    テストは `gTTS` / `edge_tts` をネットワーク不要のスタブへ差し替えるため、
    差し替えられている間は間隔待ちをしては困らない（テストが数十倍遅くなる）。
    実 gTTS のクラスは `gtts` パッケージ、実 edge-tts は `edge_tts` にある。
    """
    module = getattr(gTTS, "__module__", "") or ""
    if module.split(".")[0] == "gtts":
        return True
    if _active_tts_engine() != tts_engines.ENGINE_EDGE:
        return False
    try:
        import edge_tts as _edge_tts

        return (getattr(_edge_tts.Communicate, "__module__", "") or "").split(".")[0] == "edge_tts"
    except Exception:
        return False


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
            # キャンセル可能な待ち。**戻り値を見る**: cancellable_wait は
            # キャンセルで False を返すだけで例外は飛ばないため、ここで
            # JobCancelled に変換する。戻り値を無視するとキャンセル済みでも
            # TTS 呼び出しが続く（スロットの占拠につながる）。
            # `_tts_gate` は with が解放する。
            if not cancellable_wait(current_event(), wait):
                raise JobCancelled("tts_throttle")
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
    （実測: 3 秒空けても 429）、毎回 6 回叩くと 1 回の要求が
    20 秒以上かかっても、それでも 1 バイトも取れない。ブレーカーで即座に諦める。

    edge-tts エンジンは gTTS と別のホストなので、このブレーカーでは止めない。
    """
    if _active_tts_engine() != tts_engines.ENGINE_GTTS:
        return False
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


def _generate_tts_via_edge(
    text: str, filepath: Path, cache_dir: Optional[Path] = None
) -> str:
    """edge-tts (Microsoft Edge neural voice) で合成してキャッシュへ入れる。

    無料エンジンで、gTTS の 429 / CAPTCHA ほど制限は厳しくないが、
    ネットワーク障害は起こりうるため失敗時は呼び出し側が gTTS へ落とす。
    """
    options = _tts_edge_options()
    logger.info(
        f"edge-tts で合成を試みます: voice={options.voice}, "
        f"rate={options.rate}, pitch={options.pitch}, chars={len(text)}"
    )

    directory = Path(cache_dir) if cache_dir is not None else filepath.parent
    directory.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(directory), prefix=".tts_", suffix=".tmp")
    os.close(fd)
    try:
        _tts_throttle()
        raise_if_cancelled("tts.edge.call")
        tts_engines.synthesize_to_file(text, tmp_path, options)
        _atomic_install(tmp_path, filepath)
        tmp_path = None
    except JobCancelled:
        raise
    except Exception as e:
        if _is_rate_limited(e):
            _note_rate_limited()
        raise
    finally:
        if tmp_path:
            _remove_quietly(tmp_path)

    logger.info(f"edge-tts で合成に成功: file={filepath.name}")
    return filepath.name


def _generate_tts_via_gtts(
    text: str, filepath: Path, cache_dir: Optional[Path] = None
) -> str:
    """gTTS の従来経路（通常ドメイン → 汎用ドメイン → 旧エンドポイント）。"""
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

    return filepath.name


def generate_tts_cached(text: str, tenant_id: Optional[str] = None) -> str:
    """テキストから音声を生成し、音声設定込みのハッシュ名でキャッシュ保存。

    既定の `auto` は edge-tts（ニューラル音声）を試し、失敗したときだけ
    gTTS へ落とす。gTTS だけが動く環境でも従来どおり動作する。

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
    logger.info(
        f"新規TTS音声生成: engine={_active_tts_engine()}, chars={len(text)}, file={filename}"
    )

    # チェックポイント（1）。ネットワークを叩く直前。ここは cancellable な待ちの直前。
    raise_if_cancelled("tts.begin")

    if _active_tts_engine() == tts_engines.ENGINE_EDGE:
        try:
            return _generate_tts_via_edge(text, filepath, cache_dir)
        except JobCancelled:
            raise
        except Exception as e:
            # edge-tts の失敗（ネットワーク障害・応答不良）を gTTS で救う。
            # 無声番組にするより、機械的な gTTS でも読ませたほうがよい。
            logger.warning(f"edge-tts で合成できなかったため gTTS へ切り替えます: {e}")
            # **gTTS のキャッシュキーへ切り替える。**
            # edge のキーのまま gTTS の音声を保存すると、
            # 次回 edge が回復していても冒頭で「キャッシュヒット」になり
            # edge を再試行しなくなる。その結果、ロボット音声が
            # TTL（既定 7 日）ぶん固定され、
            # ニューラル音声化の目的が一度の通信エラーで失われる。
            fallback_name = _tts_cache_filename(text, tts_engines.ENGINE_GTTS)
            fallback_path = cache_dir / fallback_name
            if fallback_path.exists():
                logger.info(
                    f"gTTS のキャッシュが既にあります（edge は再試行します）: {fallback_name}"
                )
                return fallback_name
            filename, filepath = fallback_name, fallback_path

    if _tts_circuit_open():
        # 直近で 429 を受けており、gTTS の batchexecute は使えない。
        # ただし旧来の GET /translate_tts は別の経路なのでそちらへ切り替える。
        logger.info("gTTS はレート制限中のため、旧 TTS エンドポイントへ切り替えます")
        return _generate_tts_via_legacy_endpoint(text, filepath, cache_dir)

    return _generate_tts_via_gtts(text, filepath, cache_dir)

def generate_tts_for_segments(
    segments: List[ScriptSegment],
    tenant_id: Optional[str] = None,
    job: Optional[Job] = None,
) -> List[ScriptSegment]:
    """各トークセグメントのTTS音声を生成

    ジョブ指定があれば 1 セグメントごとに `tts.segment` イベントを送る。
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
                # 実測 duration（提案③-4）。推定（3.0 文字/秒）を実測 mp3 の
                # 長さで置き換える。UI の残秒表示が実測と一致する。
                segment.estimated_duration = _measured_duration_seconds(
                    tenant_id, audio_filename, segment.estimated_duration
                )
        except JobCancelled:
            raise
        except Exception as e:
            logger.error(f"セグメントTTS生成エラー: {e}")
            segment.metadata = segment.metadata or {}
            segment.metadata["audio_url"] = None
    return segments

OPENING_KEYWORDS = ("オープニング", "opening")
ENDING_KEYWORDS = ("エンディング", "ending")

#: 音源が無いのに塞ぐ必要があるスロットに入れる題。
#:
#: **曲名ではない。**ここに入れるのは「鳴らない曲名を番組表に出さない」
#: ためで、実曲名は :func:`_top_up_songs_for_program` が
#: ``borrowed_song`` に内側だけへ残す。
#:
#: 曲名のままにすると、司会が一度も紹介していない曲が番組の
#: プレイリストに「♪ 曲」として現れ、利用者から見て嘘になる
#: （実測: 原稿は3曲だけなのにプレイリストには6曲出ていた）。
INTERMISSION_TITLE = "間奏"


def _song_item(song: Dict[str, Any], order: int) -> Dict[str, Any]:
    """プレイリストの 1 スロットを作る。

    **音源が無いスロットには曲名を載せない。**
    ここが全経路の出口（選曲で埋めたスロットも、`_top_up_songs_for_program`
    がカタログから借って埋めたスロットも）なので、ここで 1 箇所だけ
    守ればよい。

    曲名を載せてしまうと、司会が一度も紹介していない曲が番組の
    プレイリストに「曲」として現れ、利用者から見て嘘になる
    （実測: 原稿は 3 曲だけなのにプレイリストには 6 曲出ていた）。
    実曲名は ``metadata.borrowed_song`` に内側だけ残す。
    """
    metadata: Dict[str, Any] = {"order": order}
    preview_url = song.get("preview_url")
    title = song.get("title", "不明")
    artist = song.get("artist", "不明")

    # Apple への送客導線。音源が無い間奏には出さない（曲名が無い slot で
    # Apple のページを開く導線は嘘になる）。`PlaylistItem` には新フィールドを
    # 足さず metadata に載せる（キー集合を変えると既存契約が壊れるため）。
    store_url = song.get("store_url") if preview_url else None
    if store_url and _show_store_links():
        metadata["store_url"] = store_url

    if not preview_url:
        borrowed = song.get("borrowed_song")
        detail = borrowed if isinstance(borrowed, dict) else {
            "title": title,
            "artist": artist,
            "origin": "selection",
        }
        metadata["borrowed_song"] = detail
        title = INTERMISSION_TITLE
        artist = ""

    return PlaylistItem(
        id=f"song_{order}",
        type=PlaylistItemType.SONG,
        title=title,
        artist=artist,
        preview_url=preview_url,
        artwork_url=song.get("artwork_url"),
        is_fallback=song.get("is_fallback", False),
        metadata=metadata
    ).to_dict()


def _playable_first(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """音源の有る曲を先頭へ移す（安定順序を保つ）。

    選曲順（司会が紹介する順番）を崩さないため、安定した分割にする。
    音源の有無だけを理由に並びを替えないこと。
    """
    playable = [r for r in records if r.get("previewUrl")]
    silent = [r for r in records if not r.get("previewUrl")]
    return playable + silent


def _show_store_links() -> bool:
    """送客導線（`store_url`）を API に出してよいか。

    Apple の規約上、プレビューを自社アプリで利用する場合は Apple への
    リンクを併せて提供することが望まれる。既定は有効で、環境変数
    （`RETRO_RADIO_ITUNES_SHOW_STORE_LINKS=false`）で無効化できる。
    """
    return bool(settings.itunes_show_store_links)


def _to_song_dicts(raw_songs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """iTunes の生レスポンス（trackName / previewUrl …）を API の曲形式へ揃える

    **音源が無いスロットは間奏として表し、曲名を載せない。**

    ここに統一しておき、レスポンスの ``songs`` と ``playlist`` の
    曲スロットが同じ規則で変換される。片方だけ曲名が出ると、
    「司会が紹介していない曲が番組表に出る」ズレが発生する
    （``tests/test_job_api.py::test_songs_and_playlist_come_from_the_same_list``
    が両者の位置整合を固定している）。

    実曲名は ``borrowed_song`` に内側だけ残す（原因究明用）。
    """
    songs: List[Dict[str, Any]] = []
    show_store = _show_store_links()
    for raw in raw_songs:
        preview_url = raw.get("previewUrl")
        real_title = raw.get("trackName", "不明")
        real_artist = raw.get("artistName", "不明")
        item: Dict[str, Any] = {
            "title": real_title if preview_url else INTERMISSION_TITLE,
            "artist": real_artist if preview_url else "",
            "preview_url": preview_url,
            "artwork_url": raw.get("artworkUrl100"),
            "is_fallback": preview_url is None,
            # Apple への送客導線（Apple Music の楽曲ページ）。
            # 音源が無い間奏には出さない（曲名が無い枠で Apple のページを
            # 開く導線は嘘になる）。設定で無効化されていれば常に `None`。
            "store_url": (
                store_link(raw.get("trackViewUrl"))
                if (show_store and preview_url)
                else None
            ),
        }
        if not preview_url:
            item["borrowed_song"] = {"title": real_title, "artist": real_artist}
        songs.append(item)
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
    playable_pool: Optional[List[Dict[str, Any]]] = None,
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
    playable_pool:
        番組全体で解決済みの**可聴な**曲。他のパス_LOGICの分。
        曲スロットが足りないとき、まずこれを優先して埋める。

        ここが「歯抜け（無音の溝）をゼロにする」ための要。
        同じ曲が別パスで流れるのはラジオでは普通のことであり、
        聞いたいない無音の溝のほうが悪い。実測では、正本カタログの
        うち iTunes プレビューが取れない曲（古い年ほど多い）が
        2〜3 パス目を丸ごと無音にしていた。
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

    # 1) 番組内の可聴な曲で埋める（無音の溝を作らない）
    for song in playable_pool or []:
        if len(ordered_songs) >= required:
            break
        if not song.get("preview_url"):
            continue
        key = song_key(str(song.get("title", "")), str(song.get("artist", "")))
        if key in used:
            continue
        used.add(key)
        ordered_songs.append(dict(song))

    # playable_pool だけで必要曲数を満たす場合、select_program_songs は
    # 呼ばれない。そのまま下の for に進むと `extra` が未定義のまま参照され
    # UnboundLocalError で生成が 500 になるため、**必ず事前に初期化する**。
    extra: List[Tuple[str, str]] = []
    if len(ordered_songs) < required:
        try:
            extra = select_program_songs(
                target_year, count=deficit + len(used), exclude=used
            )
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
            # 音源が無いので**実際には鳴らない**。この曲名は原稿へ
            # 渡っておらず（`server._step_resolve_previews` は音源の
            # 取れた曲だけを告げる）、フロントは間奏として扱う。
            # 「鳴らない曲名が番組表に出るのを避ける」ため、
            # title は曲名ではなく間奏であることを示す。
            "title": INTERMISSION_TITLE,
            "artist": "",
            "preview_url": None,
            "artwork_url": None,
            "is_fallback": True,
            # 借りた曲名は内側のメタデータに残す（原因究明用）。
            "borrowed_song": {"title": title, "artist": artist},
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
    playable_pool: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """トークで始まり曲で終わるラジオ番組のプレイリストを構築する。

    構成:
        オープニングトーク → 曲1 → トーク1 → 曲2 → … →
        トークN → エンディング曲

    司会の声で番組を開き、各トークの直後に対応する曲が来る順番。
    旧実装は「曲 → トーク」順（曲で始まり曲で終わる）だったが、
    再生順序の指定により逆にした。

    **各トークの後ろに必ず1曲**を置くことで、LLM が何セグメントを返しても
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
    _top_up_songs_for_program(
        ordered_songs,
        talk_total + 1,
        year,
        reserve=reserve,
        playable_pool=playable_pool,
    )

    playlist: List[Dict[str, Any]] = []
    song_idx = 0
# トークで始めて曲で終わる（オープニングトーク → 曲 → トーク → 曲 → …）。
    # 曲が足りなくてもトークが連続しないよう、並べるのは必ず「トーク → 曲」。
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
        #: パスごとのトーク（原稿をパースして TTS 済みのもの）。
        #: 1 パス目の原稿を全パスで使い回すと「曲 A を告げて B が流れる」ため。
        self.passes_segments: List[List[ScriptSegment]] = []
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


def _step_resolve_previews(ctx: _GenerationContext) -> None:
    """ステップ 2: 選曲した曲の**音源を先に解決する**。

    これを原稿生成より**前**に置くのが要点。司会が「こういう曲がある」と
    告げたのに実際には無音で流れる（歯抜け）と、ラジオ番組として破綻する。
    そのため **実際に鳴る曲だけ**を原稿の曲一覧へ渡す。
    """
    started = time.monotonic()
    ctx.emit(
        EVENT_MUSIC_STARTED,
        planned=len(ctx.selected_records),
    )
    cache = PreviewCache(settings.song_store_path or None)
    try:
        ctx.enriched = (
            _enrich_with_checkpoints(ctx.selected_records, cache)
            if ctx.selected_records
            else []
        )
    except JobCancelled:
        raise
    except Exception as e:
        logger.error(f"楽曲の音源解決に失敗しました: {e}")
        ctx.enriched = []

    # 原稿に告げられるのは「実際に鳴る曲」だけにする。
    playable = [
        (str(item.get("trackName", "")), str(item.get("artistName", "")))
        for item in ctx.enriched
        if item.get("previewUrl")
    ]
    if playable:
        if len(playable) < len(ctx.selected_pairs):
            logger.info(
                "音源が取れた曲だけを原稿へ告知します: %d / %d 曲",
                len(playable), len(ctx.selected_pairs),
            )
        ctx.selected_pairs = playable
    else:
        # 1 曲も鳴らせないとき。**選曲結果をそのまま告知しない。**
        #
        # 「全部鳴らせないなら、原稿にも曲名を一切書かせない」のが
        # 正解。選曲結果をそのまま渡すと、司会が「次は『○○』です」と
        # 紹介しながら間奏が流れる（= 利用者から見て嘘になる）。
        #
        # ただし原稿が「曲紹介」の構成を保てなくなるため、
        # 選曲した曲名は残したまま**告知だけ止める**。この場合
        # 番組は間奏主体の構成になる（原稿の書き直しは
        # ``enforce_song_allowlist`` が担）。
        logger.warning(
            "音源を 1 曲も解決できませんでした。選曲結果は原稿へ告知しません"
            "（この番組は間奏主体になります）"
        )
        ctx.selected_pairs = []
    ctx.record("music.resolve", started)


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
            # `or None` で潰さない。**空リストと None は別物**で、
            # 空リストは「1 曲も鳴らない」と確定した状態を意味する。
            # None にしてしまうと script_generator がカタログから
            # 曲名を導出し直し、鳴らない曲を紹介してしまう。
            songs=ctx.selected_pairs,
        )
    except AppError:
        # AppError はユーザー向けメッセージと原因区分を既に持っているので、
        # ここで潰すと app_error_handler の判定が死に、常に 400 になってしまう。
        logger.exception("スクリプト生成エラー(AppError)")
        raise
    except JobCancelled:
        raise
    except Exception as e:
        # 技術的な詳細はログのみ。ユーザーには例外文字列をそのまま返さない
        logger.exception(f"スクリプト生成エラー: {e}")
        raise ScriptGenerationError(
            "原稿の生成に失敗しました。時間をおいて再度お試しください。", original=e
        ) from e
    ctx.record("script", started)
    ctx.emit(EVENT_SCRIPT_DONE, chars=len(ctx.script))


def _pass_song_pairs(window: List[Dict[str, Any]]) -> List[tuple]:
    """1 パスで**実際に鳴る**曲だけを ``(曲名, アーティスト)`` の列にする。

    音源の無いスロット（間奏）は含めない。
    「鳴らない曲を司会に告げる」ものを再導入しないため。
    """
    out: List[tuple] = []
    seen: set = set()
    for item in window:
        if not item.get("previewUrl"):
            continue
        title = str(item.get("trackName", "")).strip()
        artist = str(item.get("artistName", "")).strip()
        if not title or not artist:
            continue
        key = song_key(title, artist)
        if key in seen:
            continue
        seen.add(key)
        out.append((title, artist))
    return out


def _step_parse_script(ctx: _GenerationContext) -> None:
    """ステップ 3: 原稿をセグメントにパース（見出しが無ければ空リスト）。

    パスごとの原稿（2 パス目以降）は :func:`_step_pass_scripts` が作る。
    ここでは**音源が確定する前**（`ctx.enriched` は未解決、
    `per_pass_song_count` も未確定）なので、パス別の曲対応は決められない。
    """
    ctx.segments = parse_script_segments(ctx.script)
    if not ctx.segments:
        logger.warning("原稿からトークセグメントを抽出できませんでした")
    ctx.passes_segments = [ctx.segments]


def _step_pass_scripts(ctx: _GenerationContext) -> None:
    """2 パス目以降用の原稿を、そのパスで**実際に鳴る曲**から作り直す。

    かつては全パスで 1 パス目の原稿（= パス 1 の曲紹介）を再生していたため、
    「この年のヒット曲 A をお届けします」と言いながら B が流れる、
    が既定 3 周のうち 2 周で起きていた（嘘の放送）。

    - **配置は :func:`_step_music` の後**。1 パスあたりの曲数（`per_pass_song_count`）と
      「鳴る曲を先頭へ寄せる」順序が確定するのはこのステップの前後だから。
      ここで分割すると「司会が告げる曲」と「流れる曲」がパス内でずれる。
    - 追加パスの原稿は**外部 API を呼ばない決定的な生成器**
      （`_deterministic_script` → `core.fallback`）で作る。費用ゼロで、
      そのパスの可聴曲だけの曲紹介になる。
    - 生成・TTS に失敗したパスは 1 パス目の原稿に**委譲**する
      （無音のトークを鳴らさない）。
    """
    ctx.passes_segments = [ctx.segments]
    if ctx.loop_count <= 1:
        return

    per_pass = ctx.per_pass_song_count
    enriched = _playable_first(list(ctx.enriched))
    for index in range(1, ctx.loop_count):
        window = enriched[index * per_pass:(index + 1) * per_pass]
        pairs = _pass_song_pairs(window)
        segments: Optional[List[ScriptSegment]] = None
        if pairs:
            try:
                extra_script = _deterministic_script(
                    ctx.req.year,
                    ctx.req.month,
                    ctx.req.day,
                    ctx.req.mode,
                    ctx.req.target_name,
                    pairs,
                )
                segments = parse_script_segments(extra_script or "")
                if not segments:
                    segments = None
            except JobCancelled:
                raise
            except Exception:  # noqa: BLE001 - 追加パスの失敗で番組全体を落とさない
                logger.exception(
                    "追加パスの原稿生成に失敗しました（1 パス目の原稿に委譲します）"
                )
                segments = None

        if segments is None:
            # そのパスで鳴らせる曲がない / 生成に失敗した。
            # 曲名を告げない原稿（= 1 パス目と同じ原稿）で埋める。
            ctx.passes_segments.append(ctx.segments)
            continue

        try:
            segments = generate_tts_for_segments(
                segments, tenant_id=ctx.tenant_id, job=ctx.job
            )
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001 - TTS 失敗で番組全体を落とさない
            logger.exception("追加パスの TTS に失敗しました（1 パス目の原稿に委譲します）")
            segments = None
        ctx.passes_segments.append(segments if segments is not None else ctx.segments)

    ctx.emit(EVENT_SCRIPT_DONE, chars=len(ctx.script), passes=len(ctx.passes_segments))


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

    # 原稿のセグメント数次第で 1 パスに必要な曲数が音源解決済みの数を超える
    # ことがある（例: Gemini が 8 セグメントを返した）。その差だけ補う。
    required = ctx.per_pass_song_count * ctx.loop_count
    if len(ctx.enriched) < required:
        extra_records = ctx.selected_records[len(ctx.enriched):required]
        if extra_records:
            logger.info(
                "1 パスに必要な曲数が不足したため追加で %d 曲を解決します",
                len(extra_records),
            )
            cache = PreviewCache(settings.song_store_path or None)
            try:
                ctx.enriched.extend(
                    _enrich_with_checkpoints(extra_records, cache)
                )
            except JobCancelled:
                raise
            except Exception as e:
                logger.error(f"追加の音源解決に失敗しました: {e}")

    if ctx.enriched:
        # 鳴る曲を先頭へ寄せる。**レスポンスの ``songs`` と
        # ``playlist`` の 1 パス目を同じ順序にするため**。
        #
        # ``tests/test_job_api.py::test_songs_and_playlist_come_from_the_same_list``
        # が「``songs`` は ``passes[0]`` の先頭と一致する」を契約として
        # 固定している。ここで順序が食い違うと、司会が読み上げた曲と
        # 実際に流れる曲が入れ替わる。
        ctx.enriched = _playable_first(ctx.enriched)
        ctx.song_list = _to_song_dicts(ctx.enriched[: settings.medley_song_count])
    else:
        fallback_title, fallback_artist = get_fallback_song(ctx.req.year)
        ctx.song_list = [{
            "title": fallback_title,
            "artist": fallback_artist,
            "preview_url": None,
            "artwork_url": None,
            "is_fallback": True,
        }]
    ctx.record("music", started)
    ctx.emit(EVENT_MUSIC_DONE, count=len(ctx.enriched))


def _step_playlist(ctx: _GenerationContext) -> None:
    """ステップ 7: プレイリスト構築（曲で始まり曲で終わるラジオ番組の構成）。

    **1 パス内で同じ曲を 2 回流さない**（書順が最優先）。

    1. このパスに割り当てられた曲のうち、**実際に鳴る曲**を先に置く
    2. 足りなければ、番組内の他の**鳴る曲**で埋める（別パスとの重複は
       ラジオでは普通）
    3. それでも足りなければ、**音源の無い曲**で埋める。フロントはこれを
       「間奏」として扱うので、番組の骨組み（曲 → 司会 → 曲）は保たれる

    循環埋めに頼らない
    ------------------
    可聴曲数がスロット数に足りないときに**同じ曲を循環して使う**実装は
    撤去した（``可聴な曲だけでは 1 パスの 6 スロットが埋まらないため、
    残りを循環で埋めた`` が実測ログに出ていた）。

    3 曲しか鳴らせないときに 6 スロットを「3 曲 × 2 周」で埋めると、
    1 回の放送の中で同じ曲が 2 回流れることになり、外から
    「同じ 3 曲をループ再生している」ように見える（実測の指摘）。
    音源が無いスロットはフロントが間奏として音を出すため、**間奏の方が
    利用者にとって誠実**である。音の欠落ではなく、「流していない曲」の
    ほうが誤解を招かない。
    """
    ctx.passes = []
    per_pass = ctx.per_pass_song_count
    # 音源の有る曲を先頭へ寄せる（レスポンスの ``songs`` と同じ順序）。
    # ``ctx.enriched`` は既に ``_step_resolve_previews`` で
    # ``_playable_first`` を通しているが、この関数単体でも使うため
    # ここで一度明示する（順序の二重管理を避ける）。
    enriched = _playable_first(list(ctx.enriched))

    def _key(item: Dict[str, Any]) -> str:
        return song_key(
            str(item.get("trackName", "")), str(item.get("artistName", ""))
        )

    for index in range(ctx.loop_count):
        window = enriched[index * per_pass:(index + 1) * per_pass]
        # このパスで**実際に告げる**トーク。2 パス目以降は
        # :func:`_step_pass_scripts` が `window` の可聴曲から作り直したもの。
        # 空なら 1 パス目の原稿（曲名を告げない）に委譲する。
        segments = (
            ctx.passes_segments[index]
            if index < len(ctx.passes_segments) and ctx.passes_segments[index]
            else ctx.segments
        )
        # 既に他のパスで使った曲はこのパスでは使わない
        elsewhere = [
            item for other, item in enumerate(enriched)
            if not (index * per_pass <= other < (index + 1) * per_pass)
        ]

        chunk: List[Dict[str, Any]] = []
        chunk_keys: set = set()

        # 可聴曲を優先する。鳴る曲のほうが間奏より望ましいため。
        for source in (
            [item for item in window if item.get("previewUrl")],
            [item for item in elsewhere if item.get("previewUrl")],
            [item for item in window if not item.get("previewUrl")],
            [item for item in elsewhere if not item.get("previewUrl")],
        ):
            for item in source:
                if len(chunk) >= per_pass:
                    break
                key = _key(item)
                if key in chunk_keys:
                    # 同一パスで同じ曲を 2 回使わない（要求の核心）。
                    continue
                chunk_keys.add(key)
                chunk.append(item)
            if len(chunk) >= per_pass:
                break

        if len(chunk) < per_pass:
            playable_pool = [
                item for item in enriched if item.get("previewUrl")
            ]
            logger.info(
                "1 パスの %d スロットを曲で埋められませんでした（採用 %d 曲）。"
                "残りは間奏になります。原因：その年の正本カタログが薄い"
                "（対象年 %d 曲 / 番組で可聴 %d 曲）。"
                "core/songs/songs.json を拡張すると解消します。",
                per_pass,
                len(chunk),
                len(songs_for_year(ctx.req.year, tolerance=0)),
                len(playable_pool),
            )
        else:
            playable_pool = [item for item in enriched if item.get("previewUrl")]

        ctx.passes.append(
            build_playlist(
                segments,
                _to_song_dicts(chunk),
                year=ctx.req.year,
                reserve=elsewhere,
                playable_pool=playable_pool,
            )
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
    # 音源解決は原稿生成より前。「鳴らない曲を司会に告げない」ため。
    _step_resolve_previews,
    _step_generate_script,
    _step_parse_script,
    _step_tts,
    _step_music,
    # パス別原稿は**音源解決の後**（`per_pass_song_count` と並び順が確定した後）に作る。
    _step_pass_scripts,
    _step_playlist,
    _step_quiz,
)


def _persist_generation(req: GenerateRequest, ctx: "_GenerationContext") -> None:
    """生成済みの内容を `generations` へ 1 行記録する（**例外は投げない**）。

    責務は `services.history_service.record_generation` にある。ここでは
    「どの値をどの列に入れるか」だけを直流し、失敗しても応答は壊さない
    （履歴の欠落は取り直しできるため、利用者の番組生成を 500 にするのは
    ずっと悪い結果になる）。

    .. note::
       `record_generation` 自身が例外を握り潰すが、**import 失敗**と
       **想定外の例外**は依然起こりうるため、ここでも受け止める。
    """
    try:
        from .services.history_service import record_generation
    except Exception:  # noqa: BLE001 - 遅延 import が失敗しても配信は続ける
        logger.warning("生成履歴の記録モジュールを読み込めませんでした", exc_info=True)
        return

    # 音源のある最初の曲だけを代表曲にする（`INTERMISSION_TITLE` を
    # 履歴に残すと、開示 CSV に「間奏」という曲名が並ぶだけで VPN になる）。
    try:
        lead: Dict[str, Any] = next(
            (s for s in ctx.song_list if s.get("preview_url")),
            ctx.song_list[0] if ctx.song_list else {},
        )
        record_generation(
            user_id=ctx.user_id,
            tenant_id=ctx.tenant_id,
            year=req.year,
            month=req.month,
            day=req.day,
            script=ctx.script,
            song_title=str(lead.get("title") or ""),
            artist_name=str(lead.get("artist") or ""),
            preview_url=lead.get("preview_url"),
            audio_path=ctx.audio_url,
            mode=req.mode,
            all_songs=ctx.song_list,
        )
    except Exception:  # noqa: BLE001 - 記録の失敗で配信を落とさない
        logger.warning("生成履歴の記録に失敗しました（配信は続行します）", exc_info=True)

    # 保持期間のない書き込みなので、直後にユーザー単位で剪定する。
    # `generations` は**原稿全文**（`anniversary` では `target_name` を含む）を
    # 持つため、無制限に増えるままだと次の問題が起きる:
    #
    #   - 既定の SQLite（Fly.io のボリューム）が膨張する
    #   - 削除請求（`DELETE /api/me`）で消す行数も際限なく増える
    #
    # 剪定経路は削除請求フローしか無いので、書き込み側で keep-N を守る。
    _prune_generation_history(ctx.user_id)


def _prune_generation_history(user_id: str) -> None:
    """`generations` をユーザー単位の keep-N に収める（例外は投げない）。

    `generations` は 1 行に**原稿全文**を持つ。書き込むたびに無制限に増える
    ままだと、既定の SQLite（Fly.io のボリューム）が膨張し、削除請求
    （`DELETE /api/me`）で消す行数も際限なく増える。削除請求以外の剪定経路が
    無いので、書き込み側で keep-N を守る。

    履歴は「取り直し可能」なので、失敗しても配信は壊さない。
    """
    try:
        from .db.repository import GenerationRepository
        from .db.session import get_db

        with get_db() as db:
            GenerationRepository(db).delete_old(user_id, keep=GENERATION_HISTORY_KEEP)
    except Exception:  # noqa: BLE001 - 剪定の失敗で配信を落とさない
        logger.warning("生成履歴の剪定に失敗しました（配信は続行します）", exc_info=True)


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

    # R2-08/DB-01: 生成履歴（`generations`）への記録。
    # 以前は `record_generation` が**どこからも呼ばれておらず**、
    # `generations` テーブルが恒久的に空だった（= 開示も削除請求も成立しない）。
    # `POST /api/generate` と `POST /api/jobs` はこの関数を共有しているため、
    # ここに 1 か所だけ足せば両方の経路が埋まる。
    #
    # 監査ログ（`_record_generation_audit`）とは**別物**。
    # 監査ログは個人データを入れないため開示の対象にならないが、
    # `generations` は原稿全文を持つので開示・削除の処理対象になる。
    _persist_generation(req, ctx)

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

    # 監査は「実際に起きたこと」を記録する。`finally` で常に
    # `completed/success` を書いていた実装は、失敗した生成を成功として
    # 記録していた（事故調査で「成功した」と嘘になる）。
    # 契約は非同期の `/api/jobs` と同じ: 入場時に `started` を 1 行書き、
    # 終端の phase/outcome を実際に従って 1 行書く（= 常に 2 行）。
    _record_generation_audit(
        principal.tenant_id,
        principal.user_id,
        phase="started",
        outcome="success",
        # 原稿本文・対象年（生年相当）・氏名は監査ログに入れない。
        meta={"path": "generate"},
    )

    # R2-05/JOB-02: 同期 def ハンドラは anyio スレッドプール上で走るため、
    # `acquire(timeout=...)` でブロックするとスレッドプールが枯渇する。
    # 非ブロッキングで取り、取れなければ即 503（待たせない）。
    #
    # .. note::
    #   このルートは `RETRO_RADIO_GENERATION_WAIT_TIMEOUT` を**参照しない**。
    #   非同期の `/api/jobs`（`_run_job`）だけが待ってから 503 になる。
    #   「キューが効いていない」場合は、値を伸ばすのではなく
    #   `RETRO_RADIO_MAX_CONCURRENT_GENERATIONS` を増やすか、リバースプロキシ側で
    #   リトライを返す運用を検討する。ルートの比較表は README の「同時実行制限」。
    if not _generation_slots.acquire(blocking=False):
        logger.warning("番組生成の同時実行上限に達しました（即時 503）")
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="failed",
            outcome="failure",
            meta={"path": "generate", "status": 503},
        )
        raise HTTPException(status_code=503, detail="混雑しています。しばらく待ってから再度お試しください。")

    try:
        response = _build_generate_response(
            req, tenant_id=principal.tenant_id, user_id=principal.user_id
        )
    except JobCancelled:
        # クライアント切断で中断された（`/api/jobs` と同じ扱い）。
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="cancelled",
            outcome="denied",
            meta={"path": "generate"},
        )
        raise
    except HTTPException as exc:
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="failed",
            outcome="failure",
            meta={"path": "generate", "status": exc.status_code},
        )
        raise
    except AppError as exc:
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="failed",
            outcome="failure",
            meta={"path": "generate", "reason": type(exc).__name__},
        )
        raise
    except Exception as exc:
        # 想定外の例外も「失敗」として残す（握り潰して成功に見せない）。
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="failed",
            outcome="failure",
            meta={"path": "generate", "reason": type(exc).__name__},
        )
        raise
    else:
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="completed",
            outcome="success",
            meta={"path": "generate"},
        )
        return response
    finally:
        # 必ず解放する。`finally` を外すと失敗時にデッドロックする。
        _generation_slots.release()


# --- セッションの発行 / 破棄 ------------------------------------------------------
# ブラウザクライアントが `retro_radio_session` Cookie を**入手する唯一の経路**。
#
# なぜ必要か:
#   * `<audio src="/api/audio/{tenant}/{file}">` は `Authorization` ヘッダーを
#     付けられない。Cookie 認証でしか配信を認証できない。
#   * `auth.tokens.issue_session_token` は署名済みだが、可変 Cookie を
#     **発行する経路が存在しなかった**ため、ブラウザは資格情報を入手できなかった。
# 署名は `auth/tokens.py` に既に実装済みなので、ここでは**結線するだけ**で
# 署名ロジックを再実装しない。
#
# 受理する資格情報は 2 種類（どちらも同じ「利用者」の開口部）:
#   1. `email` + `password` → `auth.authenticator.Authenticator`
#      （PBKDF2・連続失敗の指数バックオフを**そのまま使う**）
#   2. `Authorization: Bearer …` / body の `token` →
#      `RETRO_RADIO_SINGLE_USER_KEY` との定数時間比較、または署名済みベアラートークン
#
# 応答にトークンは**含めない**（Cookie だけが資格情報。JS からの読み出しも不可）。
# エラーメッセージは「メールが存在するか」を区別しない。
SESSION_AUTH_DISABLED_DETAIL = (
    "認証が無効（RETRO_RADIO_REQUIRE_AUTH=0）のためセッションを発行しません。"
)
SESSION_AUTH_UNAVAILABLE_DETAIL = (
    "セッションを発行できません。RETRO_RADIO_SECRET_KEY を 32 文字以上の"
    "ランダム値で設定してください。"
)
LOGIN_FAILED_DETAIL = "認証情報が正しくありません"
LOGIN_THROTTLED_DETAIL = "認証の試行回数が多すぎます。しばらく待ってから再度お試しください。"

#: 単一ベアラー資格でセッションを発行したときの主体 ID。
#: `auth.tokens` の payload は `uid` を必須にするため、名前を決める必要がある。
#: DB の `users` 行とは無関係なので、`require_admin` の DB ロール照合には挂からない。
BEARER_SESSION_USER_ID = "single_user"


class SessionRequest(BaseModel):
    """`POST /api/auth/session` のリクエストボディ（すべて任意）。"""

    email: Optional[str] = Field(default=None, max_length=254)
    password: Optional[str] = Field(default=None, max_length=256)
    #: 個人モード用の資格情報。`Authorization: Bearer` を優先し、無ければこれ。
    token: Optional[str] = Field(default=None, max_length=4096)


def _client_ip(request: Request, current_settings: Optional[Settings] = None) -> str:
    """スロットリング記録用の送信元 IP（R2-03 のプロキシ対策）。

    既定は **`X-Forwarded-For` を信用しない**（利用者が自由に書けるため、
    記録キーを回せば連続失敗の制限を回避できてしまう）。その場合
    実際の接続元だけを使う。

    R2-03: プロキシ（Fly / Render / nginx）の背後では
    `request.client.host` が**プロキシ自身の IP** になり、
    `(email, proxy-IP)` のキーが全利用者で共有される。结果として
    **1 人の総当たりが施設全員を恒久ロックアウトする**。
    これを避けるため、運営者が `RETRO_RADIO_TRUSTED_PROXY_HEADER` で
    「このヘッダーの **右から N 番目**を信用する」と宣言したときだけ
    ヘッダーを読む（`trusted_proxy_hops`）。

    .. warning::
       宣言を**しない限り**ヘッダーは読まない。宣言した側の責任であり、
       `hops` が実プロキシ段数より小さいと左端を偽装できる。
    """
    client = request.client
    direct = client.host if client else ""

    settings_for_ip = current_settings if current_settings is not None else settings
    header_name = (settings_for_ip.trusted_proxy_header or "").strip().lower()
    if not header_name:
        return direct

    want = header_name.encode("latin-1", "ignore")
    if not want:
        return direct
    hops = max(1, int(settings_for_ip.trusted_proxy_hops))
    for key, value in (request.scope.get("headers") or ()):
        if key.lower() != want:
            continue
        try:
            parts = [p.strip() for p in value.decode("latin-1").split(",") if p.strip()]
        except (AttributeError, UnicodeDecodeError):
            break
        # 信用段数より足りないなら**信用しない**（安全側）。
        if len(parts) < hops:
            break
        return parts[-hops]
    return direct


def _constant_time_equals(left: str, right: str) -> bool:
    """定数時間比較。比較できない型（ASCII 以外等）は「不一致」扱い。"""
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except (AttributeError, TypeError, UnicodeEncodeError):
        return False


def _verify_bearer_secret(presented: Optional[str], single_user_key: str) -> bool:
    """個人モードの資格情報が一致するか。

    生の `RETRO_RADIO_SINGLE_USER_KEY` と、署名済みベアラートークンの
    **どちら**でも通す（運用者は前者を、プログラムは後者が手持ち）。
    不一致は区別しない（どちらの失敗かも返さない）。
    """
    if not presented or not single_user_key:
        return False
    if _constant_time_equals(presented, single_user_key):
        return True
    try:
        read_bearer_token(presented, key=single_user_key)
    except TokenError:
        return False
    return True


def _principal_after_password_login(
    user_id: str,
) -> tuple:
    """ログイン成功後の (tenant_id, role) を決める。

    **ロール・テナントの正は DB**（`user_security`）。署名済み Cookie に
    焼いた値は TTL 中のスナップショットに過ぎず、`api/deps.require_admin` は
    毎回 DB を見るため、ここが一致しなくても権限昇格にはならない。
    ここでは表示用の情報としてだけ使う。
    """
    try:
        from .db.privacy_repository import UserSecurityRepository
        from .db.session import get_db

        with get_db() as db:
            record = UserSecurityRepository(db).resolve(user_id)
        return record.get("tenant_id") or "default", record.get("role") or "member"
    except Exception:  # noqa: BLE001 - 判定材料が無い = 既定値側（表示のみに使う）
        logger.warning("ログインユーザーのテナント解決に失敗しました", exc_info=True)
        return "default", "member"


@app.post("/api/auth/session")
def create_session(
    request: Request,
    payload: Optional[SessionRequest] = None,
    current_settings: Settings = Depends(settings_dependency),
):
    """資格情報を検証し、**セッション Cookie** を発行する（200）。

    成功時の応答は**主体の情報だけ**を返す。トークン自体は本文にも
    ヘッダーにも含めない（漏れる経路を作らない）。

    - `email` + `password` があれば `Authenticator`（PBKDF2 + バックオフ）
    - `Authorization: Bearer` または `token` があれば単一ベアラー資格情報
    """
    mode = resolve_mode(current_settings)
    if mode == "unavailable":
        raise HTTPException(status_code=503, detail=SESSION_AUTH_UNAVAILABLE_DETAIL)
    if mode == "disabled":
        raise HTTPException(status_code=400, detail=SESSION_AUTH_DISABLED_DETAIL)

    # Cookie の署名に必須。無い状態では 503（fail-closed）にして、
    # 「セッションを拒否したこと」を悟られても資格情報は漏らさない。
    secret = current_settings.secret_key
    if not secret:
        raise HTTPException(status_code=503, detail=SESSION_AUTH_UNAVAILABLE_DETAIL)

    if payload is not None and payload.email and payload.password:
        # --- 経路 1: メールアドレス + パスワード ---------------------------------
        # import は遅延させる（`authenticator` は DB 層を引き込むため、
        # 起動時の import コストと循環 import を避ける）。
        from .auth.authenticator import Authenticator

        client_ip = _client_ip(request, current_settings)
        authenticator = Authenticator()
        delay = authenticator.throttle_delay(payload.email, client_ip)
        if delay > 0:
            # 429 は「その (email, IP) の連続失敗回数」だけを漏らす。
            # 登録の有無は一切含まないので列挙オラクルにはならない。
            logger.info(
                "ログインがスロットリングされました: email_hash=%s", hashlib.sha256(
                    payload.email.strip().lower().encode("utf-8")
                ).hexdigest()[:12]
            )
            raise HTTPException(
                status_code=429,
                detail=LOGIN_THROTTLED_DETAIL,
                headers={"Retry-After": str(max(1, int(delay)))},
            )
        # `wait=False`: バックオフ待ちは上で 429 として返しているため、
        # ここでスレッドを止めない（`login_async` 相当の方針）。
        user = authenticator.login(
            payload.email, payload.password, ip_address=client_ip, wait=False
        )
        if user is None or not getattr(user, "id", None):
            # 「メールが無い」と「パスワードが違う」で**同じ** 401・同じ文言。
            raise HTTPException(status_code=401, detail=LOGIN_FAILED_DETAIL)
        user_id = str(user.id)
        tenant_id, role = _principal_after_password_login(user_id)
    else:
        # --- 経路 2: 単一ベアラー資格情報 -----------------------------------------
        # **この経路にもレート制限が要る**（P0-8）。`single_user_key` は
        # 当たれば 8 時間有効な署名済みセッション Cookie に化ける資格情報で、
        # 429 のブロックは「email + password」の内側にしか無く、
        # token だけを叩く連打には一切効いていなかった。
        from .auth.authenticator import Authenticator, throttle_key

        client_ip = _client_ip(request, current_settings)
        # 個人モードの資格情報は**1 つしか無い**ため、email の代わりに
        # 定数の名前を使う。`presented` をキーにすると、毎回別のキーになり
        # 記録がaccumulateするだけで制限にならない。
        bearer_key = throttle_key("bearer:personal-mode", client_ip)
        bearer_throttle = Authenticator().throttle
        delay = bearer_throttle.delay_for(bearer_key)
        if delay > 0:
            logger.info("ベアラー資格情報の試行がスロットリングされました")
            raise HTTPException(
                status_code=429,
                detail=LOGIN_THROTTLED_DETAIL,
                headers={"Retry-After": str(max(1, int(delay)))},
            )

        presented = extract_bearer(request.headers.get("Authorization"))
        if not presented and payload is not None:
            presented = payload.token
        # 短すぎる `single_user_key` は「資格情報」ではない
        # （`require_auth_config` は `unavailable` に倒す）。重ねて fail-closed。
        current_settings.require_single_user_key()
        if not _verify_bearer_secret(presented, current_settings.single_user_key):
            bearer_throttle.record_failure(bearer_key)
            logger.info("セッション発行要求が認証情報を満たしていません")
            raise HTTPException(status_code=401, detail=LOGIN_FAILED_DETAIL)
        bearer_throttle.record_success(bearer_key)
        user_id = BEARER_SESSION_USER_ID
        tenant_id = "default"
        role = "member"

    token = issue_session_token(
        user_id=user_id,
        secret=secret,
        tenant_id=tenant_id,
        role=role,
        ttl_seconds=DEFAULT_SESSION_TTL_SECONDS,
    )
    response = JSONResponse(
        content={
            "authenticated": True,
            "user_id": user_id,
            "tenant_id": tenant_id,
            "role": role,
            "auth_mode": "session",
            "expires_in": DEFAULT_SESSION_TTL_SECONDS,
        }
    )
    # HttpOnly: JS から読み出す経路（窃取経路）が無い。
    # SameSite=Lax: クロスオリジンの POST からは Cookie が送られない
    #   （CSRF の遮断）。`/api/*` は同一オリジンなので通る。
    # Secure: **HTTPS のときだけ**。平文 HTTP で Secure を付けると
    #   開発環境（http://localhost）でブラウザが Cookie を保存しない。
    #   判定は既存の `_request_is_https` を再利用する。
    response.set_cookie(
        SESSION_COOKIE_NAME,
        token,
        max_age=DEFAULT_SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=_request_is_https(request.scope),
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def destroy_session(response: Response):
    """セッション Cookie を破棄する（200）。常に成功として返す。"""
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return {"authenticated": False, "logged_out": True}


@app.post("/api/webhooks/stripe")
async def stripe_webhook(request: Request):
    # R2-04: 署名検証前に巨大ボディを読み込まないための上限（1MB）。
    # Stripe のイベントは遥かに小さいため、超過は不正リクエストとして早期拒否する。
    """Stripe からの webhook を受ける（署名検証は `WebhookHandler` に委譲）。

    認証はかけない。Stripe が `stripe-signature` ヘッダーで署名するためで、
    検証に失敗したリクエストは 400 で拒否される。
    webhook secret（`RETRO_RADIO_STRIPE_WEBHOOK_SECRET`）が未設定のままでは
    **fail-closed（503）**にする（署名無しのリクエストを受理しないため）。
    """
    # import は遅延させる（billing は stripe SDK を引き込むため、
    # 未設定環境での起動時 import コストと循環 import を避ける）。
    from .billing.webhook import WEBHOOK_VERIFICATION_ERRORS, WebhookHandler

    sig_header = request.headers.get("stripe-signature", "")
    # content-length を**読み込み前に**検査（R2-04: 署名検証前の巨大 read 防止）。
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > WEBHOOK_BODY_MAX_BYTES:
                raise HTTPException(status_code=413, detail="Payload too large")
        except ValueError:
            pass  # 不正なヘッダーは署名検証で拒否される
    payload = await request.body()
    if len(payload) > WEBHOOK_BODY_MAX_BYTES:
        # チャンク転送等で content-length が無い場合の二次防御。
        raise HTTPException(status_code=413, detail="Payload too large")
    handler = WebhookHandler()
    try:
        handler.handle_event(payload, sig_header)
    except RuntimeError:
        # secret 未設定。fail-closed にする（黙って受理しない）。
        raise HTTPException(status_code=503, detail="Webhook is not configured")
    except WEBHOOK_VERIFICATION_ERRORS:
        # 署名/ペイロード検証の失敗。理由の詳細は漏らさない。
        raise HTTPException(status_code=400, detail="Invalid webhook signature")
    return {"received": True}


# --- 提案④: 非同期ジョブ API ------------------------------------------------------
def _require_job(job_id: str, principal: Principal) -> Job:
    """ジョブをテナント照合付きで取り出す。**他テナントには 404** で見せない。"""
    job = jobs.registry.get(job_id)
    if job is None or job.tenant_id != principal.tenant_id:
        logger.info("ジョブが見つからないかテナント不一致: job_id=%s", job_id)
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _run_job(
    job: Job,
    req: GenerateRequest,
    principal: Principal,
    ticket: Optional[_QueueTicket] = None,
) -> GenerateResponse:
    """**専用ワーカースレッド**で 1 ジョブを走らせる本体。

    同時実行スロットは `try/finally` で確実に解放する。キャンセル例外が飛んでも、
    失敗しても、**必ず** `release()` に到達する。

    入場枠チケット（``_job_queue_slots``）は**ここでは解放しない**。
    解放は :func:`retro_radio.jobs.start_worker` の ``on_finish`` に委譲し、
    ジョブが**実際に終端した**瞬間（`running` に入った後、終わった後、
    あるいは開始前にキャンセルされた時）にだけ返す。
    ここで先に返すと、``_generation_slots`` を待つあいだも枠が空き、
    スレッド数が無制限に増える（``fly.toml`` の 1 vCPU では OOM キル）。
    """
    del ticket  # 解放は start_worker の on_finish が担う（ここでは参照しない）
    if not _generation_slots.acquire(timeout=settings.generation_wait_timeout):
        logger.warning("番組生成の同時実行上限に達しました（ジョブ）: job_id=%s", job.job_id)
        raise HTTPException(status_code=503, detail="混雑しています。しばらく待ってから再度お試しください。")
    try:
        response = _build_generate_response(
            req, tenant_id=principal.tenant_id, user_id=principal.user_id, job=job
        )
        # Store the result on the job BEFORE emitting `done`.
        # `start_worker` only marks failed/cancelled on exceptions; without this
        # call the job would stay `running` forever and `GET /api/jobs/{id}`
        # would return `result: null` even after a successful generation.
        job.succeed(response.model_dump())
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

    **入場制御はここで行う**: 枠 (`_job_queue_slots`) が埋まっているときは
    202 を返さず `/api/generate` と同じ **503** で断る。スレッドを
    作ってから 503 にするのではなく、**スレッドを作らないまま**断るため、
    埋まった状態でも**スレッドが増え続けない**。
    """
    ticket = _acquire_job_queue_slot()
    if ticket is None:
        logger.warning(
            "ジョブの入場枠が埋まっているため 503 で拒否: limit=%d", _JOB_QUEUE_LIMIT
        )
        _record_generation_audit(
            principal.tenant_id,
            principal.user_id,
            phase="failed",
            outcome="failure",
            meta={"path": "jobs", "status": 503},
        )
        raise HTTPException(
            status_code=503,
            detail="混雑しています。しばらく待ってから再度お試しください。",
        )

    try:
        job = jobs.new_job(
            jobs.registry, principal.tenant_id, req.model_dump(), settings
        )
    except Exception:
        # ジョブを作れなかった場合は自分の入場枠を戻す（リークさせない）。
        ticket.release()
        raise

    job.emit(EVENT_ESTIMATE, **job.estimate.to_dict() if job.estimate else {})
    _record_generation_audit(
        principal.tenant_id,
        principal.user_id,
        phase="started",
        outcome="success",
        meta={"job_id": job.job_id, "path": "jobs", "year": req.year},
    )
    try:
        thread = jobs.start_worker(
            job,
            lambda j: _run_job(j, req, principal, ticket),
            # 入場枠の解放を「ジョブが終端したとき」に 1 度だけ行う。
            # `target` を呼ばない経路（開始前キャンセル・スレッド生成失敗）でも
            # ここが必ず走るので、入場枠が恒久に漏れることはない。
            on_finish=ticket.release,
        )
    except Exception:
        # `start_worker` が例外を投げても、Job を終端させてチケットを返す。
        job.emit(jobs.EVENT_FAILED, reason="worker_start_failed", retryable=True)
        job.fail("worker_start_failed", True, detail="ジョブの起動に失敗しました")
        ticket.release()
        raise
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
    """SSE の再開位置（`Last-Event-ID` ヘッダー / クエリ）。

    上限なしの整数は**拒否する**（不正値として 0 に落とす）。
    上限が無いと `?last_event_id=999999999999` を 1 つ送るだけで
    「その seq は未来なので永久に空」になり、完成済みジョブの接続が
    `SSE_MAX_SECONDS`（既定 900 秒）不被機的に保持される。1 接続 =
    executor の 1 スレッドを最大 900 秒占有するため、テナント全体が 503 になる。
    """
    raw = request.headers.get("last-event-id") or request.query_params.get("last_event_id")
    try:
        value = int(raw) if raw not in (None, "") else 0
    except (TypeError, ValueError):
        return 0
    if value < 0 or value > _MAX_LAST_EVENT_ID:
        logger.warning(
            "不正な Last-Event-ID を 0 として扱います: value=%s (上限 %d)",
            value, _MAX_LAST_EVENT_ID,
        )
        return 0
    return value


def _is_terminal_event(name: str) -> bool:
    """終端イベントか（そこで SSE ストリームを有限に閉じる）。"""
    return name in jobs.TERMINAL_EVENTS


# R2-06: SSE 1 接続は既定 executor のスレッドを最大 `SSE_MAX_SECONDS`
# 占有するため、接続数を上限で絞る（無制限だとスレッドプールが枯渇する）。
_SSE_MAX_CONNECTIONS = max(4, settings.max_concurrent_generations * 4)
_sse_slots = GenerationSlots(_SSE_MAX_CONNECTIONS)

# `Last-Event-ID` の上限。イベント `seq` は 32bit 整数の端に届かないため、
# これを超える値は「攻撃的な値」か「別環境の seq」としかならない。
_MAX_LAST_EVENT_ID = 2 ** 31 - 1


async def _sse_stream(job: Job, request: Request, ticket: "_QueueTicket"):
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
            # 完了済みジョブは**待たずに**終端させる（P0-7）。
            # `wait_for_events` は keepalive 秒数だけブロックするので、
            # 判定を「空が返ってから」に置くと 1 往復ぶんの待ちが残る。
            if job.is_finished:
                for event in job.events_after(after):
                    yield event.to_sse()
                    after = event.seq
                    if _is_terminal_event(event.name):
                        return
                return
            pending = await loop.run_in_executor(
                None, job.wait_for_events, after, jobs.SSE_KEEPALIVE_SECONDS
            )
            if not pending:
                # 待機中にジョブが完了した場合もここで拾う。
                if job.is_finished:
                    for event in job.events_after(after):
                        yield event.to_sse()
                        after = event.seq
                        if _is_terminal_event(event.name):
                            return
                    return
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
                # `Job.succeed()` は `EVENT_DONE` を emit する**前**に終端状態を決める
                # （`server._run_job` は succeed → emit の順）。
                # そのため `is_finished` を見た瞬間に return すると、
                # バッファに残った終端イベント（done / failed / cancelled）を
                # **読み飛ばす**。クライアントは最後のイベントを見失う。
                # （実測: 生成履歴の記録を挟むとこの窓が広がり恒常的に再現した）
                for event in job.events_after(after):
                    yield event.to_sse()
                    after = event.seq
                    if _is_terminal_event(event.name):
                        return
                return
    finally:
        # 購読者が居なくなってもジョブ自体は止めない（ポーリングで拾えるように）。
        job.client_gone.set()
        # R2-06: SSE 接続スロットを確実に返す（切断・終端・タイムアウトの全経路）。
        ticket.release()


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
    # R2-06: SSE 接続数に上限をかける。空きが無ければ 503（待たせない）。
    if not _sse_slots.acquire(blocking=False):
        logger.warning(
            "SSE 接続数が上限に達したため 503 で拒否: limit=%d", _SSE_MAX_CONNECTIONS
        )
        raise HTTPException(status_code=503, detail="接続が混雑しています。しばらく待ってから再度お試しください。")
    ticket = _QueueTicket(_sse_slots)
    return StreamingResponse(
        _sse_stream(job, request, ticket),
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


def _resolve_flat_audio(filename: str) -> Optional[Path]:
    """`CACHE_DIR` 直下の音声ファイルを解決する（個人モード用）。無ければ `None`。

    2 層のガード only:
    1. ファイル名の形式（`AUDIO_FILENAME_PATTERN`）
    2. 実パスが `CACHE_DIR` 配下か（シンボリックリンク / `..` 対策）

    サイズとマジックバイトの検証は [`_validate_audio_file`] で行う。
    """
    if not AUDIO_FILENAME_PATTERN.match(filename):
        logger.warning(f"拒否された音声ファイル名: {filename!r}")
        return None

    try:
        resolved = (CACHE_DIR / filename).resolve(strict=True)
        cache_root = CACHE_DIR.resolve()
    except OSError:
        return None

    if not resolved.is_relative_to(cache_root) or not resolved.is_file():
        logger.warning(f"キャッシュ領域外を指す音声パスを受理しませんでした: {filename!r}")
        return None
    return resolved


@app.get("/api/audio/{filename}")
async def get_audio(filename: str):
    """生成されたTTS音声のストリーミング配信（**従来のフラット URL**）。

    CACHE_DIR 直下に置かれた個人モードのキャッシュを返す。
    3 層防御（拡張子 → サイズ → マジックバイト）は維持する。
    認証有効時は `relative_url_for` がテナント付き URL を返すため、
    新規クライアントはこのルートを叩かない。
    """
    # 認証有効時はこのフラット ルートを**閉じる**（404 に見せる）。
    # 開いたままだと、認証無効期間に CACHE_DIR 直下へ書かれたファイルが
    # 認証を有効化した後も TTL 経過まで無認証で読める bypass 窓になる。
    # 認証済みクライアントはテナント付き URL（`/api/audio/{tenant}/{file}`）を使う。
    if _auth_enforced():
        raise HTTPException(status_code=404, detail="Audio file not found")
    resolved = _resolve_flat_audio(filename)
    if resolved is None:
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

    認証を切った個人モードでは `generate_tts_cached` が `CACHE_DIR` 直下に書く
    （`_tenant_cache_dir` の挙動）。そのためテナントディレクトリが無くても
    **同一ファイルを配信できる**ようにしておく。認証が有効なときだけは
    テナントディレクトリ内しか解決せず、テナント間の交差を防ぐ。
    """
    resolved = _cache().resolve(tenant, filename)
    if resolved is None and not _auth_enforced():
        # 個人モード: フラット配置の CACHE_DIR だけを対象にする。
        resolved = _resolve_flat_audio(filename)
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
async def health(
    principal: Principal = Depends(optional_principal),
    current_settings: Settings = Depends(settings_dependency),
):
    """ヘルスチェック（**公開**。監視の liveness probe として無認証で叩かれる）

    認証が有効な運用では、**資格情報を持たない**呼び出し（＝匿名の
    プローバ）にモードや資格情報の有無を返すのは攻撃の手掛かりになる。
    公開レスポンスに含めるのは「監視に必要な最小集合」だけ:

    - ``status`` / ``service`` / ``version`` : 監視用
    - ``api_key_configured`` : 障害調査用（公開されている。値そのものは出さない）
    - ``auth_required`` : 利用者に「このデプロイはログインが必要」と伝えるため

    ``auth_mode``（``session`` / ``bearer`` / ``anonymous``）と
    ``auth_ready`` / ``secret_key_configured`` は
    **認証済みの呼び出しにだけ**返す。認証済みかどうかを返すことで
    「窃取した Cookie / Bearer」「未認証の経路」のどれを選ぶかを決める
    手がかりになるため、匿名には出さない。
    """
    has_api_key = bool(current_settings.gemini_api_key)
    payload = {
        "status": "healthy" if has_api_key else "degraded",
        "service": "Retro Radio Time Machine",
        "version": current_settings.app_version,
        "api_key_configured": has_api_key,
        "auth_required": bool(current_settings.require_auth),
        "auth_enforced": _auth_enforced(),
    }
    # `auth_mode` / `auth_ready` / `secret_key_configured` は
    # **認証済みの呼び出しにだけ**返す（docstring が宣言している契約）。
    # 匿名には出さない: この 3 つが攻撃者に
    # 「窃取した Cookie / Bearer を使うか、未認証の経路を探すか」を
    # 選ばせる手がかりになる（実測可能な情報開示）。
    if principal.authenticated:
        payload["secret_key_configured"] = bool(current_settings.secret_key)
        payload["auth_ready"] = bool(current_settings.auth_ready)
        payload["auth_mode"] = principal.auth_mode
    return payload

# 静的ファイル配信
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/", response_class=HTMLResponse)
async def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return HTMLResponse(content=index_file.read_text(encoding="utf-8"))
    return HTMLResponse(content="<h1>Retro Radio Time Machine API Server</h1><p>Frontend static files loading...</p>")
