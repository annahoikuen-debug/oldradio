import logging
import tempfile
import os
import threading
import requests as _requests
from gtts import gTTS
from ..config import get_settings
from ..utils.errors import handle_error
from ..utils.text_cleaner import clean_script_for_tts
from typing import Optional

logger = logging.getLogger(__name__)
settings = get_settings()

# ElevenLabs 用のタイムアウト（config.py に項目が無いため定数で運用）
ELEVENLABS_TIMEOUT = (5, 30)

_SESSION_LOCK = threading.Lock()


class _LazySession:
    """モジュール内で使い回す `requests.Session` への薄い窓口。

    呼び出しごとに `requests.post` / `requests.get` を使うと、
    TCP/TLS のハンドシェイクが毎回やり直しになるため 1 本に束ねる。
    ただし `requests.<method>` を直接 monkeypatch する既存テストがあるため、
    **モジュール属性 `tts.requests` 経由の呼び出しを提供し続ける**。
    テストが `tts.requests.post` を差し替えればその関数が使われる。
    """

    def __init__(self) -> None:
        self._session: Optional[_requests.Session] = None

    def get_session(self) -> _requests.Session:
        with _SESSION_LOCK:
            if self._session is None:
                self._session = _requests.Session()
            return self._session

    def reset(self) -> None:
        """セッションを捨てる（テスト隔離用）。"""
        with _SESSION_LOCK:
            session, self._session = self._session, None
        if session is not None:
            try:
                session.close()
            except Exception as e:  # pragma: no cover - 環境依存
                logger.debug(f"HTTPセッションのクローズに失敗しました: {e}")

    def request(self, *args, **kwargs):
        return self.get_session().request(*args, **kwargs)

    def post(self, *args, **kwargs):
        return self.get_session().post(*args, **kwargs)

    def get(self, *args, **kwargs):
        return self.get_session().get(*args, **kwargs)

    def __getattr__(self, name):
        # `RequestException` など、それ以外の属性は素の requests モジュールに委譲する。
        return getattr(_requests, name)


requests = _LazySession()

def text_to_speech(text: str, force_quality: str = None) -> Optional[str]:
    """テキスト→音声ファイルパス返却（失敗時None）"""
    # TTS用に原稿をクリーニング（構造要素・アスタリスク除去）
    text = clean_script_for_tts(text)
    logger.info(f"TTS合成開始: chars={len(text)}")
    quality = force_quality or "standard"
    try:
        # 高品質音声生成を試み、失敗した場合は標準品質にフォールバック
        if quality == "premium":
            if settings.elevenlabs_api_key:
                try:
                    return elevenlabs_tts(text)
                except Exception as e:
                    logger.warning(f"プレミアム品質音声生成失敗、標準品質にフォールバック: {e}")
                    return gtts_tts(text)
            else:
                # APIキーがない場合は標準品質を使用
                logger.info("ELEVENLABS_API_KEYが設定されていないため、標準品質を使用します")
                return gtts_tts(text)
        elif quality == "high":
            try:
                return gtts_tts(text, lang=settings.tts_language, tld=settings.tts_tld, slow=settings.tts_slow)
            except Exception as e:
                logger.warning(f"高品質音声生成失敗、標準品質にフォールバック: {e}")
                return gtts_tts(text)
        else:
            # 標準品質
            return gtts_tts(text)
    except Exception as e:
        logger.error(f"音声合成失敗: {e}")
        handle_error(e, "TTS")
        return None

import hashlib
import shutil
import time
from pathlib import Path

TTS_CACHE_DIR = Path(tempfile.gettempdir()) / "retro_radio_tts_cache"
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

# --- キャッシュの上限 -----------------------------------------------------------
# ここは `TenantTtsCache` とは別の、プロセスローカルなファイルキャッシュ。
# TTL も上限も無いと `%TEMP%` が際限なく膨らむため、**件数と日数を両方**で縛る。
TTS_CACHE_MAX_ENTRIES = 200
TTS_CACHE_TTL_DAYS = 7
#: 書き込み N 回ごとに 1 回だけ掃除する（`TenantTtsCache` と同じ間引き方式）。
TTS_CACHE_SWEEP_INTERVAL = 20

_sweep_lock = threading.Lock()
_sweep_counter = 0


def _cache_entries() -> list:
    """キャッシュ内の `(mtime, path)` を古い順に返す。"""
    entries = []
    try:
        for path in TTS_CACHE_DIR.glob("*.mp3"):
            try:
                entries.append((path.stat().st_mtime, path))
            except OSError:
                continue
    except OSError as e:  # pragma: no cover - 環境依存
        logger.warning(f"TTSキャッシュの列挙に失敗しました: {e}")
        return []
    entries.sort(key=lambda item: item[0])
    return entries


def _unlink_quietly(path: Path) -> None:
    try:
        path.unlink()
    except OSError as e:
        logger.debug(f"TTSキャッシュの削除に失敗しました: {path} ({e})")


def sweep_tts_cache(force: bool = False) -> int:
    """古いキャッシュを削除して**上限内に収める**。戻り値は削除件数。

    - TTL（`TTS_CACHE_TTL_DAYS`）より古いものを削除する。
    - それでも `TTS_CACHE_MAX_ENTRIES` を超えるなら、mtime が古い側（LRU）から削る。
    - `force=False`（既定）は `TTS_CACHE_SWEEP_INTERVAL` 回に 1 回だけ実行する。
    """
    global _sweep_counter
    with _sweep_lock:
        _sweep_counter += 1
        count = _sweep_counter
    if not force and count % max(1, TTS_CACHE_SWEEP_INTERVAL) != 0:
        return 0

    entries = _cache_entries()
    if not entries:
        return 0
    cutoff = time.time() - TTS_CACHE_TTL_DAYS * 86400
    kept = []
    removed = 0
    for mtime, path in entries:
        if mtime < cutoff:
            _unlink_quietly(path)
            removed += 1
        else:
            kept.append((mtime, path))
    if len(kept) > TTS_CACHE_MAX_ENTRIES:
        for _mtime, path in kept[: len(kept) - TTS_CACHE_MAX_ENTRIES]:
            _unlink_quietly(path)
            removed += 1
    if removed:
        logger.info(f"TTSキャッシュを整理しました: {removed}件削除 / 残り{len(kept)}件")
    return removed


def reset_tts_cache_sweep_counter() -> None:
    """間引きカウンタをリセットする（テスト隔離用）。"""
    global _sweep_counter
    with _sweep_lock:
        _sweep_counter = 0

def gtts_tts(text: str, lang: str = None, tld: str = None, slow: bool = None) -> Optional[str]:
    """gTTSを使用した音声合成（キャッシュ対応）"""
    try:
        actual_lang = lang or settings.tts_language
        actual_tld = tld or settings.tts_tld
        actual_slow = slow if slow is not None else settings.tts_slow
        
        # キャッシュキー生成
        cache_key = hashlib.sha256(f"{text}_{actual_lang}_{actual_tld}_{actual_slow}".encode("utf-8")).hexdigest()
        cached_path = TTS_CACHE_DIR / f"{cache_key}.mp3"
        
        # キャッシュが存在する場合はコピーして返却（cleanup_audio_fileでの削除対策）
        if cached_path.exists() and cached_path.stat().st_size > 0:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
                target = Path(tmp.name)
            try:
                shutil.copy2(cached_path, target)
                # LRU: 参照されたので mtime を更新する（sweep が新しい順に保つため）
                try:
                    os.utime(cached_path, None)
                except OSError:
                    pass
                logger.info(f"TTSキャッシュヒット: path={target}")
                return str(target)
            except Exception:
                # 部分的にコピーできたファイルを残さない
                cleanup_audio_file(str(target))
                raise

        tts = gTTS(
            text=text, 
            lang=actual_lang,
            tld=actual_tld,
            slow=actual_slow
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            target = Path(tmp.name)
        try:
            tts.save(target)
            try:
                shutil.copy2(target, cached_path)
            except Exception as ce:
                logger.warning(f"TTSキャッシュ保存失敗: {ce}")
            sweep_tts_cache()
            logger.info(f"TTS合成完了: path={target}")
            return str(target)
        except Exception:
            # 合成が途中で失敗したときの temp ファイルを残さない
            cleanup_audio_file(str(target))
            raise
    except Exception as e:
        logger.error(f"gTTS音声合成失敗: {e}")
        handle_error(e, "TTS")
        return None

def elevenlabs_tts(text: str) -> Optional[str]:
    """ElevenLabs APIを使用した高品質音声合成（プレミアム機能）"""
    api_key = settings.elevenlabs_api_key
    if not api_key:
        logger.warning("ELEVENLABS_API_KEYが設定されていません。gTTSにフォールバックします。")
        return gtts_tts(text)

    try:
        response = requests.post(
            f"https://api.elevenlabs.io/v1/text-to-speech/{settings.elevenlabs_voice_id}",
            headers={
                "xi-api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "audio/mpeg",
            },
            json={
                "text": text,
                "model_id": settings.elevenlabs_model_id,
                "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
            },
            timeout=ELEVENLABS_TIMEOUT,
        )
        response.raise_for_status()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            target = Path(tmp.name)
            # write は **with ブロックの中**で行う。外に出すとハンドルが閉じた
            # 後に書くことになり、API 成功後も必ず "write to closed file" で
            # 失敗して gTTS に黙ってフォールバックする。
            try:
                tmp.write(response.content)
            except Exception:
                # 部分書き込みで失敗した temp ファイルを残さない
                cleanup_audio_file(str(target))
                raise
        logger.info(f"ElevenLabs音声合成完了: path={target}")
        return str(target)
    except Exception as e:
        logger.error(f"ElevenLabs TTS失敗: {e}")
        # フォールバックとして標準品質を使用
        return gtts_tts(text)

def cleanup_audio_file(path: Optional[str]) -> None:
    """一時ファイル安全削除"""
    if path and os.path.exists(path):
        try:
            os.unlink(path)
            logger.debug(f"一時ファイル削除: {path}")
        except Exception as e:
            logger.warning(f"一時ファイル削除失敗: {e}")

def generate_error_audio(message: str) -> Optional[bytes]:
    """エラー用音声バイト列生成（再生後即破棄）"""
    target = None
    try:
        tts = gTTS(text=message, lang=settings.tts_language)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            target = Path(tmp.name)
        tts.save(target)
        with open(target, "rb") as f:
            return f.read()
    except Exception:
        return None
    finally:
        if target is not None:
            cleanup_audio_file(str(target))
