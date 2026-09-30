import os
import re
import hashlib
import tempfile
import logging
import threading
import time
from typing import Optional, List, Dict, Any, Literal
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, date

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Receive, Scope, Send
from gtts import gTTS

from .models.radio import ScriptSegment, PlaylistItem, PlaylistItemType
from .config import get_settings
from .utils.async_runner import shutdown_executor
from .utils.errors import AppError, ScriptGenerationError, TTSError, ConfigurationError
from .core.script_generator import generate_radio_script, parse_script_segments
from .core.music_search import search_itunes_songs, select_songs
from .core.fallback import get_fallback_song, get_fallback_songs, get_reminiscence_quiz, HistoricalRadioPrograms
from .utils.logging_config import setup_logging
from .utils.text_cleaner import clean_script_for_tts

logger = logging.getLogger("retro_radio")
setup_logging()

settings = get_settings()

# 音声キャッシュディレクトリ設定
CACHE_DIR = Path(tempfile.gettempdir()) / "retro_radio_audio_cache"

# 静的ファイルディレクトリ
STATIC_DIR = Path(__file__).parent.parent / "static"

# --- セキュリティヘッダ -----------------------------------------------------------
# 外部リソースは static/index.html と static/app.js を実読して列挙したものだけ許可する。
#   * Google Fonts   : fonts.googleapis.com（CSS）/ fonts.gstatic.com（webfont）
#   * data:          : favicon・manifest のアイコン・app.css のノイズテクスチャ
#   * blob:          : app.js が object URL を素通しで許可している（toAbsoluteUrl）
#   * media-src      : iTunes プレビュー（https://*.mzstatic.com / audio-ssl.itunes.apple.com）
#   * worker-src     : /static/service-worker.js（PWA）
#   * cdn.jsdelivr   : FastAPI 内蔵の /docs・/redoc（Swagger UI / ReDoc の読み込み元）
# 未知のホストは一切許可しない（許可内容は環境変数 RETRO_RADIO_CSP で上書きできる）。
CONTENT_SECURITY_POLICY = "; ".join(
    [
        "default-src 'self'",
        "base-uri 'none'",
        "object-src 'none'",
        "frame-ancestors 'none'",
        "form-action 'none'",
        "script-src 'self' https://cdn.jsdelivr.net",
        # 'unsafe-inline' は index.html の style 属性（チューナー針・クイズ枠の表示制御）に必須
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

# このアプリはマイク・カメラ・決済・位置情報を使わないため全無効でよい。
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
    for key, value in scope.get("headers") or ():
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
# TTS キャッシュには gTTS が書いた mp3 しか無いが、公開 GET エンドポイントなので
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


# 同時実行できる番組生成の上限（Gemini / gTTS / iTunes はいずれもブロッキングHTTPのため）
_generation_slots = threading.BoundedSemaphore(settings.max_concurrent_generations)
_cache_lock = threading.Lock()
_sweep_counter = 0

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
    # 「その月の31日は無い」ため 422 になる（実測: 今日=30日のとき 2月だけ 422）。
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
    # 1 パスの playlist をクライアントが何周するか（既定の推奨周回数）。
    # クライアントは UI で 1〜5 に変更できる。
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

def _save_tts_to_temp(tts) -> str:
    """同一ファイルシステム上の一時ファイルへ書き出す（os.replace の atomic rename 用）"""
    fd, tmp_path = tempfile.mkstemp(dir=str(CACHE_DIR), prefix=".tts_", suffix=".tmp")
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
            # 配信中のファイルを Windows がロックしているケースの短い間隔リトライ
            if attempt == 2:
                raise
            time.sleep(0.1 * (attempt + 1))

def generate_tts_cached(text: str) -> str:
    """テキストからgTTSで音声を生成し、音声設定込みのハッシュ名でキャッシュ保存"""
    _sweep_tts_cache()

    filename = _tts_cache_filename(text)
    filepath = CACHE_DIR / filename

    if filepath.exists():
        logger.info(f"TTSキャッシュヒット: file={filename}")
        return filename

    _ensure_cache_dir()
    logger.info(f"新規TTS音声生成: chars={len(text)}, file={filename}")

    tmp_path: Optional[str] = None
    try:
        try:
            tts = gTTS(text=text, lang=settings.tts_language, tld=settings.tts_tld, slow=settings.tts_slow)
            tmp_path = _save_tts_to_temp(tts)
            _atomic_install(tmp_path, filepath)
            tmp_path = None
        except Exception as e:
            logger.error(f"TTS生成エラー: {e}")
            if tmp_path:
                _remove_quietly(tmp_path)
                tmp_path = None
            # 汎用ドメインでリトライ
            tts = gTTS(text=text, lang=settings.tts_language, tld="com", slow=settings.tts_slow)
            tmp_path = _save_tts_to_temp(tts)
            _atomic_install(tmp_path, filepath)
            tmp_path = None
    except Exception as e:
        logger.error(f"TTS生成リトライも失敗しました: {e}")
        raise TTSError("音声の合成に失敗しました。時間をおいて再度お試しください。", original=e) from e
    finally:
        if tmp_path:
            _remove_quietly(tmp_path)

    return filename

def generate_tts_for_segments(segments: List[ScriptSegment]) -> List[ScriptSegment]:
    """各トークセグメントのTTS音声を生成"""
    for segment in segments:
        try:
            # TTS用にクリーニング
            cleaned_content = clean_script_for_tts(segment.content)
            if cleaned_content.strip():
                audio_filename = generate_tts_cached(cleaned_content)
                segment.metadata = segment.metadata or {}
                segment.metadata["audio_url"] = f"/api/audio/{audio_filename}"
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
    # `segment_index` を載せることで、フロント側（app.js）が
    # 「今どの原稿セグメントを朗読中か」を原稿用紙の該当行へ紐付けられる。
    # 見出し名は重複しうるので index を正とする。
    # 既存 dict を直接触らず複製する（呼び出し元の metadata を汚さない）。
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

    同名の見出しが複数来た場合は **先勝ち** にして、余りは「黙って消さずに」
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
    ordered_songs: List[Dict[str, Any]], required: int, year: Optional[int]
) -> List[Dict[str, Any]]:
    """曲で始まり曲で終わる構成を成立させるため、スロット数ぶん曲子で埋める。

    短いと、1) トーク同士が連続し 2) 末尾が無音のまま終わる。
    そのため不足分は FALLBACK 曲（メタデータのみ）で必ず埋める。
    プレビュー音源の無いスロットはフロント側が「間奏」として扱う。
    """
    if len(ordered_songs) >= required:
        return ordered_songs

    deficit = required - len(ordered_songs)
    target_year = year if year is not None else settings.default_year
    try:
        extra = get_fallback_songs(target_year, count=deficit)
    except Exception:
        logger.exception(f"番組用曲の補完に失敗しました: year={target_year}")
        return ordered_songs

    for title, artist in extra:
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
    year: Optional[int] = None
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
        # セグメントがない場合は曲だけ並べる
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
    _top_up_songs_for_program(ordered_songs, talk_total + 1, year)

    playlist: List[Dict[str, Any]] = []
    song_idx = 0
    if len(ordered_songs) >= talk_total:
        # 曲で始めて曲で終わる（テーマ曲 → トーク → 曲 → … → テーマ曲）
        for segment in talk_order:
            playlist.append(_song_item(ordered_songs[song_idx], song_idx))
            song_idx += 1
            playlist.append(_talk_item(segment))
    else:
        # 曲満たない極端な場合のみトーク優先（無音トークを作らない）
        for segment in talk_order:
            playlist.append(_talk_item(segment))
            if song_idx < len(ordered_songs):
                playlist.append(_song_item(ordered_songs[song_idx], song_idx))
                song_idx += 1
    # 曲が余った場合は末尾に並べる（トークの連続は起きない）
    while song_idx < len(ordered_songs):
        playlist.append(_song_item(ordered_songs[song_idx], song_idx))
        song_idx += 1

    return playlist

def _build_generate_response(req: GenerateRequest) -> GenerateResponse:
    # 1. スクリプト原稿の生成
    try:
        script = generate_radio_script(
            req.year, req.month, req.day, mode=req.mode, target_name=req.target_name
        )
    except AppError:
        # AppError はユーザー向けメッセージと原因区分（APIキー → 503 等）を既に持っている。
        # ここで包むと app_error_handler の判定が死に、必ず 400 になってしまうためそのまま再送出する。
        logger.exception("スクリプト生成エラー(AppError)")
        raise
    except Exception as e:
        # 技術的な詳細はログのみ。用户には例外文字列をそのまま返さない
        logger.exception(f"スクリプト生成エラー: {e}")
        raise ScriptGenerationError(
            "原稿の生成に失敗しました。時間をおいて再度お試しください。", original=e
        ) from e

    # 2. スクリプトをセグメントにパース（見出しが無ければ空リスト）
    segments = parse_script_segments(script)
    if not segments:
        logger.warning("原稿からトークセグメントを抽出できませんでした")

    # 3. 各トークセグメントのTTS音声を生成
    segments = generate_tts_for_segments(segments)

    # 4. 全体用のTTS音声も生成（後方互換性のため）
    #    セグメント単位の音声とほぼ同一内容で 1 リクエストあたり gTTS を 1 回余計に消費するが、
    #    既存クライアント（api/v1.py 等）が `audio_url` を参照し得るため既定では生成する。
    #    コストを許容できない環境では RETRO_RADIO_FULL_SCRIPT_TTS=0 で無効化できる
    #    （その場合 audio_url は None = レスポンス互換性は保たれる）。
    audio_url = None
    if _env_flag("RETRO_RADIO_FULL_SCRIPT_TTS", True):
        try:
            tts_script = clean_script_for_tts(script)
            audio_filename = generate_tts_cached(tts_script)
            audio_url = f"/api/audio/{audio_filename}"
        except Exception as e:
            logger.error(f"全体音声合成処理失敗: {e}")
            audio_url = None
    else:
        logger.info("RETRO_RADIO_FULL_SCRIPT_TTS が無効のため全体版TTSをスキップします")

    # 5. 楽曲検索・選定（複数曲）
    #
    # 応答の `songs` は従来どおり `medley_song_count` 件に保つ（公開契約）。
    # 一方 `build_playlist` は「オープニング曲 + トークN + エンディング曲」で
    # N+1 スロットを必要とし、プレビュー音源が足りないと先頭・末尾が無音になる。
    # そのためプレイリストには別に `program_min_song_count` 件ぶんの候補を渡す。
    # HTTP 呼び出しは増えない（同じ `songs` を `select_songs` に2回通すだけ）。
    playlist_song_count = max(settings.medley_song_count, settings.program_min_song_count)
    try:
        songs = search_itunes_songs(req.year)
        selected_songs = select_songs(req.year, songs, count=settings.medley_song_count)
        song_list = _to_song_dicts(selected_songs)
        playlist_songs = _to_song_dicts(
            select_songs(req.year, songs, count=playlist_song_count)
        )
    except Exception as e:
        logger.error(f"楽曲検索エラー: {e}")
        fallback_title, fallback_artist = get_fallback_song(req.year)
        fallback_entry = {
            "title": fallback_title,
            "artist": fallback_artist,
            "preview_url": None,
            "artwork_url": None,
            "is_fallback": True
        }
        song_list = [fallback_entry]
        playlist_songs = [fallback_entry]

    # 6. プレイリスト構築（曲で始まり曲で終わるラジオ番組の構成）
    playlist = build_playlist(segments, playlist_songs, year=req.year)

    # 7. 回想法モード時のクイズデータ
    quiz_data = None
    if req.mode == "care_recreation":
        quiz_data = get_reminiscence_quiz(req.year)

    return GenerateResponse(
        year=req.year,
        month=req.month,
        day=req.day,
        mode=req.mode,
        script=script,
        audio_url=audio_url,
        songs=song_list,
        song=song_list[0] if song_list else None,
        reminiscence_quiz=quiz_data,
        target_name=req.target_name,
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        segments=[s.to_dict() for s in segments] or None,
        program_guide=HistoricalRadioPrograms.get_program_guide(
            req.year, req.month, req.day
        ).to_dict(),
        playlist=playlist
    )

# 同期 def にしたのは意図的: Gemini / gTTS / iTunes はいずれもブロッキングHTTPで、
# async のまま呼ぶとイベントループが数分間ブロックされる（FastAPI は同期ハンドラをスレッドプールで実行する）
@app.post("/api/generate", response_model=GenerateResponse)
def generate_radio(req: GenerateRequest):
    """ラジオ番組生成API"""
    logger.info(f"番組生成リクエスト受信: year={req.year}, month={req.month}, day={req.day}, mode={req.mode}")

    if not _generation_slots.acquire(timeout=settings.generation_wait_timeout):
        logger.warning("番組生成の同時実行上限に達しました")
        raise HTTPException(status_code=503, detail="混雑しています。しばらく待ってから再度お試しください。")

    try:
        return _build_generate_response(req)
    finally:
        _generation_slots.release()

@app.get("/api/audio/{filename}")
async def get_audio(filename: str):
    """生成されたTTS音声のストリーミング配信

    CACHE_DIR には `generate_tts_cached` が書いた mp3 しか無い前提だが、
    認証のない公開 GET エンドポイントなので **拡張子 → サイズ → マジックバイト** の
    3層で検証する（後勝ちの 1 層の os.path.basename だけでは
    `.txt` でも `b"old"` 3バイトでも `audio/mpeg` として配信できていた）。
    """
    if not AUDIO_FILENAME_PATTERN.match(filename):
        logger.warning(f"拒否された音声ファイル名: {filename!r}")
        raise HTTPException(status_code=404, detail="Audio file not found")

    try:
        resolved = (CACHE_DIR / filename).resolve(strict=True)
        cache_root = CACHE_DIR.resolve()
    except OSError:
        raise HTTPException(status_code=404, detail="Audio file not found")

    # basename の直後にも 1 枚ガード（シンボリックリンク / ディレクトリ指定の排除）
    if not resolved.is_relative_to(cache_root) or not resolved.is_file():
        logger.warning(f"キャッシュ領域外を指す音声パスを受理しませんでした: {filename!r}")
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

    return FileResponse(
        resolved,
        media_type="audio/mpeg",
        filename=filename,
        headers={"X-Content-Type-Options": "nosniff"},
    )

@app.get("/api/decades")
async def get_decades():
    """利用可能な年代一覧"""
    # 1950から2025年まで10年刻みで生成
    decades = list(range(settings.min_year, settings.max_year + 1, 10))
    # 2025を含むように調整
    if settings.max_year not in decades:
        decades.append(settings.max_year)
    return {
        "decades": sorted(list(set(decades))),  # 重複を除去してソート
        "default_year": settings.default_year
    }

@app.get("/health")
async def health():
    # APIキーの状態もヘルスチェックに含める
    has_api_key = bool(settings.gemini_api_key)
    return {
        "status": "healthy" if has_api_key else "degraded",
        "service": "Retro Radio Time Machine",
        "version": settings.app_version,
        "api_key_configured": has_api_key,
        "secret_key_configured": bool(settings.secret_key)
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
