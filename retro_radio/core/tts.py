import logging
import tempfile
import os
import requests
from gtts import gTTS
from ..config import get_settings
from ..utils.errors import handle_error
from ..utils.text_cleaner import clean_script_for_tts
from typing import Optional

logger = logging.getLogger(__name__)
settings = get_settings()

# ElevenLabs 用のタイムアウト（config.py に項目が無いため定数で運用）
ELEVENLABS_TIMEOUT = (5, 30)

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
from pathlib import Path

TTS_CACHE_DIR = Path(tempfile.gettempdir()) / "retro_radio_tts_cache"
TTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)

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
                shutil.copy2(cached_path, tmp.name)
                logger.info(f"TTSキャッシュヒット: path={tmp.name}")
                return tmp.name

        tts = gTTS(
            text=text, 
            lang=actual_lang,
            tld=actual_tld,
            slow=actual_slow
        )
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tts.save(tmp.name)
            try:
                shutil.copy2(tmp.name, cached_path)
            except Exception as ce:
                logger.warning(f"TTSキャッシュ保存失敗: {ce}")
            logger.info(f"TTS合成完了: path={tmp.name}")
            return tmp.name
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
            tmp.write(response.content)
            logger.info(f"ElevenLabs音声合成完了: path={tmp.name}")
            return tmp.name
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
    try:
        tts = gTTS(text=message, lang=settings.tts_language)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tts.save(tmp.name)
            with open(tmp.name, "rb") as f:
                audio_bytes = f.read()
        os.unlink(tmp.name)
        return audio_bytes
    except Exception:
        return None
