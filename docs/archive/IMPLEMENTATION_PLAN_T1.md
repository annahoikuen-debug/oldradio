# T1: 基盤整備・コアリファクタリング実装計画書
## 概要
改善案1〜5を実装し、保守性・テスタビリティ・堅牢性を大幅向上させる

---

## Step 1: プロジェクト構造作成・モジュール分離準備
**対象ファイル**: 新規作成複数
**作業内容**:
```
retro_radio/
├── __init__.py
├── config.py
├── core/
│   ├── __init__.py
│   ├── script_generator.py
│   ├── music_search.py
│   ├── tts.py
│   └── fallback.py
├── ui/
│   ├── __init__.py
│   ├── components.py
│   └── styles.py
├── services/
│   ├── __init__.py
│   ├── cache_service.py
│   └── history_service.py
├── utils/
│   ├── __init__.py
│   ├── session.py
│   ├── errors.py
│   └── validators.py
└── main.py
```
**実装手順**:
1. ディレクトリ作成: `mkdir -p retro_radio/{core,ui,services,utils,tests}`
2. 各 `__init__.py` 作成（空ファイル可）
3. `app.py` をバックアップ: `cp app.py app.py.backup`
**テスト**: ディレクトリ構造確認 `find retro_radio -type f | sort`
**完了基準**: 全ディレクトリ・ファイル存在、import エラーなし

---

## Step 2: 設定管理モジュール実装 (config.py)
**対象ファイル**: `retro_radio/config.py` 新規
**作業内容**: pydantic-settings で環境変数対応設定クラス実装
```python
from pydantic_settings import BaseSettings
from pydantic import Field
from functools import lru_cache

class Settings(BaseSettings):
    # キャッシュ・リトライ
    cache_ttl: int = Field(default=3600, ge=60, le=86400)
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_wait_min: int = Field(default=2, ge=1, le=60)
    retry_wait_max: int = Field(default=10, ge=1, le=300)
    retry_multiplier: int = Field(default=1, ge=1, le=5)
    
    # 外部API
    itunes_limit: int = Field(default=50, ge=1, le=200)
    itunes_timeout_connect: int = Field(default=5, ge=1, le=30)
    itunes_timeout_read: int = Field(default=10, ge=1, le=60)
    
    # Gemini
    gemini_model: str = "gemini-1.5-flash"
    gemini_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    
    # TTS
    tts_language: str = "ja"
    tts_slow: bool = False
    
    # 年範囲
    default_year: int = Field(default=1960, ge=1950, le=2025)
    min_year: int = 1950
    max_year: int = 2025
    
    # APIキー（環境変数から）
    gemini_api_key: str = ""
    
    # デバッグ
    debug: bool = False
    
    class Config:
        env_file = ".env"
        env_prefix = "RETRO_RADIO_"
        case_sensitive = False

@lru_cache()
def get_settings() -> Settings:
    return Settings()
```
**依存関係追加**: `requirements.txt` に `pydantic-settings>=2.0.0` 追加
**テスト**: `tests/test_config.py` 作成・設定値読み込み・バリデーション確認
**完了基準**: 環境変数・.env・デフォルト値の優先順位正常、バリデーションエラー適切

---

## Step 3: セッション状態管理ユーティリティ実装 (utils/session.py)
**対象ファイル**: `retro_radio/utils/session.py` 新規
**作業内容**: 型安全なセッション初期化・アクセサ関数実装
```python
from typing import TypedDict, List, Optional
from dataclasses import dataclass, asdict, field
import streamlit as st
from datetime import datetime

@dataclass
class HistoryEntry:
    year: int
    date: str
    script: str
    song_title: str
    artist_name: str
    preview_url: Optional[str]
    audio_path: Optional[str]
    timestamp: str
    def to_dict(self) -> dict: return asdict(self)
    @classmethod def from_dict(cls, d: dict) -> 'HistoryEntry': return cls(**d)

class SessionState(TypedDict):
    audio_cache: dict
    generation_history: List[HistoryEntry]
    error_voice_guidance: bool

DEFAULT_STATE: SessionState = {
    "audio_cache": {},
    "generation_history": [],
    "error_voice_guidance": False,
}

def init_session_state() -> None:
    """アプリ起動時に1回呼ぶ"""
    for key, default in DEFAULT_STATE.items():
        if key not in st.session_state:
            st.session_state[key] = default

def get_history() -> List[HistoryEntry]:
    return [HistoryEntry.from_dict(h) for h in st.session_state.generation_history]

def add_history(entry: HistoryEntry) -> None:
    history = get_history()
    history.insert(0, entry)
    st.session_state.generation_history = [h.to_dict() for h in history[:10]]

def clear_history() -> None:
    st.session_state.generation_history = []

def get_audio_cache() -> dict:
    return st.session_state.audio_cache

def set_audio_cache(key: str, value: bytes) -> None:
    st.session_state.audio_cache[key] = value

def get_error_voice_guidance() -> bool:
    return st.session_state.error_voice_guidance

def set_error_voice_guidance(value: bool) -> None:
    st.session_state.error_voice_guidance = value
```
**テスト**: `tests/test_session.py` - 初期化・追加・取得・上限10件・型変換確認
**完了基準**: KeyError発生せず、履歴10件制限動作、型ヒント有効

---

## Step 4: 共通エラーハンドリング実装 (utils/errors.py)
**対象ファイル**: `retro_radio/utils/errors.py` 新規
**作業内容**: 統一エラー基底クラス・ハンドラ・デコレータ実装
```python
import logging
import streamlit as st
from functools import wraps
from typing import Callable, Any, TypeVar

logger = logging.getLogger(__name__)
F = TypeVar('F', bound=Callable[..., Any])

class AppError(Exception):
    """ユーザー向けメッセージを持つ共通エラー"""
    def __init__(self, user_message: str, technical_message: str = "", original: Exception | None = None):
        self.user_message = user_message
        self.technical_message = technical_message or str(original or "")
        self.original = original
        super().__init__(self.technical_message)

class ScriptGenerationError(AppError): pass
class MusicSearchError(AppError): pass
class TTSError(AppError): pass
class ValidationError(AppError): pass
class ConfigurationError(AppError): pass

def handle_error(error: Exception, context: str = "") -> None:
    """統一エラー表示・ログ記録"""
    prefix = f"[{context}] " if context else ""
    if isinstance(error, AppError):
        logger.warning(f"{prefix}{error.technical_message}")
        st.warning(error.user_message)
    else:
        logger.exception(f"{prefix}Unexpected error: {error}")
        st.error("予期しないエラーが発生しました。時間をおいて再試行してください。")

def with_error_handling(context: str, fallback_return=None):
    """関数をラップしてエラー統一処理"""
    def decorator(func: F) -> F:
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                return func(*args, **kwargs)
            except AppError:
                raise  # 再スロー（上位で処理）
            except Exception as e:
                handle_error(e, context)
                return fallback_return
        return wrapper
    return decorator

def with_retry_async(max_attempts: int = 3, min_wait: float = 2.0, max_wait: float = 10.0):
    """非同期関数用リトライデコレータ（Step 8で使用）"""
    from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
    return retry(
        stop=stop_after_attempt(max_attempts),
        wait=wait_exponential(multiplier=1, min=min_wait, max=max_wait),
        retry=retry_if_exception_type((ConnectionError, TimeoutError, IOError)),
        reraise=True
    )
```
**テスト**: `tests/test_errors.py` - 各エラー種別・ハンドラ・デコレータ動作確認
**完了基準**: 既存3箇所のtry/exceptを統一ハンドラで置換可能、ログ出力確認

---

## Step 5: 入力検証・バリデーション実装 (utils/validators.py)
**対象ファイル**: `retro_radio/utils/validators.py` 新規
**作業内容**: Pydanticモデルでリクエスト検証・サニタイズ実装
```python
from pydantic import BaseModel, Field, field_validator, model_validator
from datetime import date
from typing import Optional
import re

class GenerationRequest(BaseModel):
    year: int = Field(ge=1950, le=2025, description="生成対象年")
    month: int = Field(ge=1, le=12, description="月")
    day: int = Field(ge=1, le=31, description="日")
    
    @field_validator('day')
    @classmethod
    def validate_day(cls, v: int, info) -> int:
        if 'month' in info.data and 'year' in info.data:
            try:
                date(info.data['year'], info.data['month'], v)
            except ValueError:
                raise ValueError(f'{info.data["year"]}年{info.data["month"]}月に{v}日は存在しません')
        return v

class ApiKeyConfig(BaseModel):
    gemini_key: str = Field(min_length=10, description="Gemini APIキー")
    
    @field_validator('gemini_key')
    @classmethod
    def validate_key_format(cls, v: str) -> str:
        if not re.match(r'^AIza[A-Za-z0-9_-]{35}$', v):
            raise ValueError('無効なAPIキー形式です')
        return v

class SearchParams(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    country: str = Field(pattern='^[A-Z]{2}$', default='JP')
    media: str = Field(pattern='^(music|podcast|audiobook)$', default='music')
    entity: str = Field(pattern='^(musicTrack|album|artist)$', default='musicTrack')
    limit: int = Field(ge=1, le=200, default=50)

def sanitize_text(text: str, max_len: int = 5000) -> str:
    """XSS対策・文字数制限"""
    cleaned = re.sub(r'[<>"\']', '', text.strip())
    return cleaned[:max_len]

def validate_year_range(year: int, min_y: int = 1950, max_y: int = 2025) -> bool:
    return min_y <= year <= max_y
```
**テスト**: `tests/test_validators.py` - 正常系・境界値・異常系（無効日付・短いキー等）網羅
**完了基準**: 全バリデーション通過、エラーメッセージ日本語で適切

---

## Step 6: フォールバックデータ分離 (core/fallback.py)
**対象ファイル**: `retro_radio/core/fallback.py` 新規
**作業内容**: 代表曲データ・フォールバック原稿生成を独立モジュール化
```python
from typing import List, Tuple
from datetime import datetime
from ..utils.validators import validate_year_range
from ..config import get_settings

FALLBACK_SONGS: dict[int, List[Tuple[str, str]]] = {
    1950: [("リンゴの唄", "並木路子"), ("銀座カンカン娘", "高峰秀子")],
    1960: [("上を向いて歩こう", "坂本九"), ("見上げてごらん夜の星を", "坂本九")],
    1970: [("いい日旅立ち", "山口百恵"), ("君よ抱かれて熱くなれ", "西城秀樹")],
    1980: [("ルビーの指環", "寺尾聰"), ("シルエット・ロマンス", "大橋純子")],
    1990: [("世界に一つだけの花", "SMAP"), ("LA·LA·LA LOVE SONG", "久保田利伸")],
    2000: [("ハナミズキ", "一青窈"), ("TSUNAMI", "サザンオールスターズ")],
    2010: [("前前前世", "RADWIMPS"), ("Lemon", "米津玄師")],
    2020: [("夜に駆ける", "YOASOBI"), ("ドライフラワー", "優里")],
}

def get_fallback_song(year: int) -> Tuple[str, str]:
    """年度に最も近い代表曲を返す"""
    if not validate_year_range(year):
        year = 1960
    if year in FALLBACK_SONGS:
        import random
        return random.choice(FALLBACK_SONGS[year])
    # 範囲外は最寄りの decade
    decade = (year // 10) * 10
    return get_fallback_song(decade)

def generate_fallback_script(year: int, month: int, day: int) -> str:
    """定型フォールバック原稿生成"""
    return f"""皆様、いかがお過ごしでしょうか。
{year}年{month}月{day}日でございますね。
この頃の日本は、季節の移ろいとともに、人々の暮らしも静かに変わってまいりました。
ニュースの準備ができませんでしたが、皆様の記憶の中に、この日の風景がよみがえりますように。
それでは、この年のヒット曲を少しだけお聴きください。"""

def get_available_decades() -> List[int]:
    """データ存在する年代一覧"""
    return sorted(FALLBACK_SONGS.keys())
```
**テスト**: `tests/test_fallback.py` - 全年代・境界値・ランダム性確認
**完了基準**: 既存 FALLBACK_SONGS と同等動作、型ヒント完全

---

## Step 7: スクリプト生成コア実装 (core/script_generator.py)
**対象ファイル**: `retro_radio/core/script_generator.py` 新規
**作業内容**: Gemini連携・リトライ・キャッシュ・フォールバック統合
```python
import logging
import google.generativeai as genai
from tenacity import retry, stop_after_attempt, wait_exponential
from ..config import get_settings
from ..utils.errors import ScriptGenerationError, handle_error
from ..core.fallback import generate_fallback_script

logger = logging.getLogger(__name__)
settings = get_settings()

def _build_prompt(year: int, month: int, day: int) -> str:
    return f"""
あなたは昭和・平成のレトロなラジオパーソナリティです。
{year}年{month}月{day}日の日本で起きたニュースや日常の出来事を1つ紹介する原稿を書いてください。

条件:
- 口調: 丁寧で温かみのある語り口（「皆様、いかがお過ごしでしょうか」「〜でございますね」など）
- 文字数: 300文字程度
- 最後に「それでは、この年のヒット曲を少しだけお聴きください」と曲振りを入れる
- 日本国内の出来事に限定する
"""

@retry(
    stop=stop_after_attempt(settings.max_retries),
    wait=wait_exponential(multiplier=settings.retry_multiplier, min=settings.retry_wait_min, max=settings.retry_wait_max),
    reraise=True
)
def _call_gemini(prompt: str) -> str:
    if not settings.gemini_api_key:
        raise ScriptGenerationError("Gemini APIキーが設定されていません", "GEMINI_API_KEY not configured")
    genai.configure(api_key=settings.gemini_api_key)
    model = genai.GenerativeModel(settings.gemini_model)
    response = model.generate_content(prompt)
    return response.text.strip()

def generate_radio_script(year: int, month: int, day: int) -> str:
    """メイン関数：原稿生成（失敗時フォールバック）"""
    logger.info(f"Gemini生成開始: year={year}, month={month}, day={day}")
    try:
        prompt = _build_prompt(year, month, day)
        result = _call_gemini(prompt)
        logger.info(f"Gemini生成完了: year={year}")
        return result
    except Exception as e:
        logger.error(f"ラジオ原稿生成失敗: {e}")
        handle_error(e, "ScriptGeneration")
        return generate_fallback_script(year, month, day)
```
**テスト**: `tests/test_script_generator.py` - モックでGemini正常/失敗・フォールバック確認
**完了基準**: 既存 `generate_radio_script` と同等動作、リトライ・キャッシュ（後で適用）対応

---

## Step 8: 音楽検索コア実装 (core/music_search.py)
**対象ファイル**: `retro_radio/core/music_search.py` 新規
**作業内容**: iTunes API検索・フィルタリング・フォールバック統合
```python
import logging
import random
import requests
from typing import Optional
from ..config import get_settings
from ..utils.errors import MusicSearchError, handle_error
from ..core.fallback import get_fallback_song
from ..utils.validators import SearchParams

logger = logging.getLogger(__name__)
settings = get_settings()

def search_itunes_songs(year: int) -> list[dict]:
    """iTunes検索（プレビューありのみ）"""
    logger.info(f"iTunes検索開始: year={year}")
    params = SearchParams(
        term=f"{year}年 日本 ヒット曲 ランキング",
        limit=settings.itunes_limit
    )
    try:
        response = requests.get(
            "https://itunes.apple.com/search",
            params=params.model_dump(exclude_none=True),
            timeout=(settings.itunes_timeout_connect, settings.itunes_timeout_read)
        )
        response.raise_for_status()
        data = response.json()
        results = data.get("results", [])
        filtered = [s for s in results if s.get("previewUrl")]
        logger.info(f"iTunes検索完了: year={year}, results={len(filtered)}")
        return filtered
    except Exception as e:
        logger.error(f"楽曲検索失敗: {e}")
        raise MusicSearchError("楽曲検索に失敗しました。代表曲から選曲します。", str(e))

def select_song(year: int, songs: list[dict]) -> tuple[str, str, Optional[str], bool]:
    """楽曲選択（フォールバック込み）"""
    if not songs:
        title, artist = get_fallback_song(year)
        return title, artist, None, True
    song = random.choice(songs)
    return (
        song.get("trackName", "不明"),
        song.get("artistName", "不明"),
        song.get("previewUrl"),
        False
    )
```
**テスト**: `tests/test_music_search.py` - 正常検索・空結果・ネットワークエラー・フォールバック確認
**完了基準**: 既存 `search_itunes_songs` と同等動作、timeout・retry設定反映

---

## Step 9: 音声合成コア実装 (core/tts.py)
**対象ファイル**: `retro_radio/core/tts.py` 新規
**作業内容**: gTTSラッパー・一時ファイル管理・エラー統一
```python
import logging
import tempfile
import os
from gtts import gTTS
from ..config import get_settings
from ..utils.errors import TTSError, handle_error

logger = logging.getLogger(__name__)
settings = get_settings()

def text_to_speech(text: str) -> Optional[str]:
    """テキスト→音声ファイルパス返却（失敗時None）"""
    logger.info(f"TTS合成開始: chars={len(text)}")
    try:
        tts = gTTS(text=text, lang=settings.tts_language, slow=settings.tts_slow)
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as tmp:
            tts.save(tmp.name)
            logger.info(f"TTS合成完了: path={tmp.name}")
            return tmp.name
    except Exception as e:
        logger.error(f"音声合成失敗: {e}")
        handle_error(e, "TTS")
        return None

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
```
**テスト**: `tests/test_tts.py` - 正常合成・空文字・言語コード・クリーンアップ確認
**完了基準**: 既存 `text_to_speech` と同等動作、一時ファイル確実削除

---

## Step 10: キャッシュ・履歴サービス実装 (services/)
**対象ファイル**: `retro_radio/services/cache_service.py`, `history_service.py` 新規
**作業内容**: Streamlitキャッシュ・セッション履歴の抽象化
```python
# cache_service.py
import streamlit as st
from typing import Any, Callable, TypeVar
from ..config import get_settings

F = TypeVar('F', bound=Callable[..., Any])
settings = get_settings()

def cached(ttl: int | None = None, key_prefix: str = ""):
    """キャッシュデコレータ（設定駆動）"""
    cache_ttl = ttl or settings.cache_ttl
    def decorator(func: F) -> F:
        return st.cache_data(ttl=cache_ttl, show_spinner=False)(func)
    return decorator

def clear_all_cache() -> None:
    st.cache_data.clear()
    if hasattr(st.session_state, 'audio_cache'):
        st.session_state.audio_cache.clear()

# history_service.py
from typing import List
from ..utils.session import HistoryEntry, get_history, add_history, clear_history

def get_recent_history(limit: int = 10) -> List[HistoryEntry]:
    return get_history()[:limit]

def save_generation_result(
    year: int, date_str: str, script: str,
    song_title: str, artist_name: str,
    preview_url: str | None, audio_path: str | None
) -> HistoryEntry:
    from datetime import datetime
    entry = HistoryEntry(
        year=year, date=date_str, script=script,
        song_title=song_title, artist_name=artist_name,
        preview_url=preview_url, audio_path=audio_path,
        timestamp=datetime.now().strftime("%H:%M:%S")
    )
    add_history(entry)
    return entry

def clear_all_history() -> None:
    clear_history()
```
**テスト**: `tests/test_services.py` - キャッシュクリア・履歴追加・上限・永続化確認
**完了基準**: 既存サイドバー機能と同等、キャッシュキー衝突なし

---

## Step 11: UI コンポーネント・スタイル分離 (ui/)
**対象ファイル**: `retro_radio/ui/styles.py`, `components.py` 新規
**作業内容**: CSS・HTMLテンプレート・UI部品を分離
```python
# styles.py
STYLES = """
<style>
/* 既存CSS全量をここに移植・整理 */
.main-title { font-size: 2.8rem; text-align: center; margin-bottom: 1rem; }
.subtitle { font-size: 1.4rem; text-align: center; color: #666; margin-bottom: 2rem; }
.script-text { font-size: 1.6rem; line-height: 2.0; background: #fefefe; padding: 2rem; border-radius: 12px; margin: 1rem 0; border: 2px solid #ddd; color: #1a1a1a; }
.stButton > button { font-size: 1.8rem; padding: 1.2rem 2.5rem; width: 100%; min-height: 70px; background: #1f6feb; color: white; border: none; border-radius: 8px; font-weight: bold; }
.stButton > button:hover { background: #1558b8; }
.stButton > button:focus { outline: 3px solid #ffd700; outline-offset: 2px; }
.year-label { font-size: 1.5rem; text-align: center; margin: 1rem 0; }
audio { width: 100%; height: 50px; }
@media (prefers-color-scheme: dark) {
  .main-title { color: #fff; }
  .subtitle { color: #ccc; }
  .script-text { background: #2b2b2b; color: #f0f0f0; border-color: #444; }
  .year-label { color: #ccc; }
  .stButton > button { background: #4a90e2; color: #fff; }
  .stButton > button:hover { background: #357ab8; }
}
@media (prefers-reduced-motion: reduce) { * { animation: none !important; transition: none !important; } }
</style>
"""

PWA_TAGS = """
<link rel="manifest" href="/static/manifest.json">
<script>
if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/static/service-worker.js')
      .then(reg => console.log('SW registered:', reg))
      .catch(err => console.log('SW failed:', err));
  });
}
</script>
"""

# components.py
import streamlit as st
from typing import Optional
from ..utils.session import HistoryEntry

def render_header() -> None:
    st.markdown('<div class="main-title">📻 レトロラジオ・タイムマシン</div>', unsafe_allow_html=True)
    st.markdown('<div class="subtitle">懐かしのあの年へ、ラジオでタイムトラベル</div>', unsafe_allow_html=True)

def render_year_selector(current_month: int, current_day: int) -> int:
    st.markdown(f'<div class="year-label">今日の日付: {current_month}月{current_day}日</div>', unsafe_allow_html=True)
    year = st.slider("西暦を選んでください", 1950, 2025, 1960, 1)
    st.markdown(f'<div class="year-label">選択された年: <strong>{year}年</strong></div>', unsafe_allow_html=True)
    return year

def render_script_area(script: str) -> None:
    st.subheader("📝 ラジオ原稿")
    st.text_area("", value=script, height=200, disabled=True, label_visibility="collapsed")

def render_audio_player(audio_path: Optional[str]) -> None:
    st.subheader("🎙️ ニュース音声")
    if audio_path:
        import os
        if os.path.exists(audio_path):
            with open(audio_path, "rb") as f:
                st.audio(f.read(), format="audio/mp3")
        else:
            st.info("🔊 音声ファイルがありません。テキストを選択→右クリック→読み上げで再生可能です。")

def render_song_info(title: str, artist: str, preview_url: Optional[str], is_fallback: bool) -> None:
    st.subheader("🎵 今日の一曲")
    st.write(f"**{title}** / {artist}")
    if preview_url and not is_fallback:
        st.audio(preview_url, format="audio/mp3")
    else:
        st.info("💡 試聴音源はありません。歌詞やメロディを思い出してお楽しみください。")

def render_sidebar() -> None:
    with st.sidebar:
        if st.button("キャッシュクリア"):
            from ..services.cache_service import clear_all_cache
            clear_all_cache()
            st.success("キャッシュをクリアしました")
        st.markdown("---")
        st.markdown("## ⚙️ システム情報")
        from ..utils.session import get_audio_cache, get_history
        st.caption(f"キャッシュ件数: {len(get_audio_cache())}")
        st.caption(f"履歴件数: {len(get_history())}")
        if st.button("全キャッシュクリア"):
            from ..services.cache_service import clear_all_cache
            clear_all_cache()
            st.success("クリアしました")
            st.rerun()
        st.markdown("## 📜 再生履歴")
        history = get_history()
        if history:
            for i, entry in enumerate(history):
                with st.expander(f"{entry.year}年{entry.date} - {entry.song_title}"):
                    st.caption(f"生成時刻: {entry.timestamp}")
                    if st.button("再生", key=f"replay_{i}"):
                        st.session_state.replay_entry = entry.to_dict()
        else:
            st.caption("まだ履歴がありません")
        if st.button("履歴をクリア", use_container_width=True):
            from ..services.history_service import clear_all_history
            clear_all_history()
            st.rerun()
        st.checkbox("エラー時も音声でお知らせ", key="error_voice_guidance")
```
**テスト**: `tests/test_ui.py` - コンポーネント描画・スタイル適用・サイドバー動作確認
**完了基準**: 既存UI完全再現、CSS重複なし、PWAタグ1箇所のみ

---

## Step 12: メインエントリーポイント統合・既存コード置換
**対象ファイル**: `retro_radio/main.py` 新規、既存 `app.py` 置換
**作業内容**: 全モジュール統合・元の `app.py` と同等動作確認
```python
# main.py
import streamlit as st
from datetime import datetime
from retro_radio.config import get_settings
from retro_radio.utils.session import init_session_state
from retro_radio.utils.errors import handle_error
from retro_radio.core.script_generator import generate_radio_script
from retro_radio.core.music_search import search_itunes_songs, select_song
from retro_radio.core.tts import text_to_speech, cleanup_audio_file, generate_error_audio
from retro_radio.services.history_service import save_generation_result
from retro_radio.ui.styles import STYLES, PWA_TAGS
from retro_radio.ui.components import (
    render_header, render_year_selector, render_script_area,
    render_audio_player, render_song_info, render_sidebar
)

# 初期化
st.set_page_config(page_title="レトロラジオ・タイムマシン", page_icon="📻", layout="centered")
st.markdown(STYLES, unsafe_allow_html=True)
st.markdown(PWA_TAGS, unsafe_allow_html=True)
init_session_state()

# 設定・状態
settings = get_settings()
current_month = datetime.now().month
current_day = datetime.now().day

# UI描画
render_header()
selected_year = render_year_selector(current_month, current_day)

# 生成実行
if st.button("📻 ラジオを再生する", type="primary"):
    # 再生フラグ処理
    if "replay_entry" in st.session_state:
        entry = st.session_state.replay_entry
        del st.session_state.replay_entry
        script = entry["script"]
        song_title = entry["song_title"]
        artist_name = entry["artist_name"]
        preview_url = entry["preview_url"]
        audio_path = entry["audio_path"]
        selected_year = entry["year"]
        st.session_state.skip_generation = True
    else:
        st.session_state.skip_generation = False
    
    if not settings.gemini_api_key:
        st.error("Gemini APIキーが設定されていません。環境変数 `GEMINI_API_KEY` を設定してください。")
        st.stop()
    
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    if st.session_state.get("skip_generation", False):
        st.session_state.skip_generation = False
        status_text.text("🎉 履歴から再生中...")
        progress_bar.progress(100)
    else:
        # 並列実行は Step 16 で実装
        with st.spinner("ラジオ原稿を作成中..."):
            script = generate_radio_script(selected_year, current_month, current_day)
        progress_bar.progress(33)
        
        with st.spinner("その時のヒット曲を探しています..."):
            try:
                songs = search_itunes_songs(selected_year)
                song_title, artist_name, preview_url, use_fallback = select_song(selected_year, songs)
            except Exception as e:
                handle_error(e, "MusicSearch")
                song_title, artist_name = get_fallback_song(selected_year)
                preview_url, use_fallback = None, True
        progress_bar.progress(66)
        
        with st.spinner("音声を合成しています..."):
            audio_path = text_to_speech(script)
        progress_bar.progress(100)
    
    st.success("完成しました！")
    save_generation_result(
        year=selected_year,
        date_str=f"{current_month}月{current_day}日",
        script=script,
        song_title=song_title,
        artist_name=artist_name,
        preview_url=preview_url,
        audio_path=audio_path
    )

# 表示エリア（生成後のみ表示）
if 'script' in locals() or 'replay_entry' in st.session_state:
    render_script_area(script)
    render_audio_player(audio_path)
    render_song_info(song_title, artist_name, preview_url, use_fallback)
    cleanup_audio_file(audio_path)

# サイドバー
render_sidebar()
```
**テスト**: 
- 既存 `test_app.py` を新構造対応に更新
- 統合テスト `tests/test_integration.py` - 全フロー動作確認
- 手動確認チェックリスト実行

**完了基準**: 
- `streamlit run retro_radio/main.py` で既存 `app.py` と全く同等動作
- 全既存テスト通過
- 新規ユニットテスト全通過（カバレッジ70%以上）
- 破損していたHTML/JS重複解消済み

---

## T1 全体リグレッション防止チェックリスト
- [ ] 既存機能: 年選択→生成→音声再生→履歴再生 全フロー
- [ ] APIキー未設定時の警告表示
- [ ] Gemini失敗→フォールバック原稿
- [ ] iTunes検索失敗→代表曲選択
- [ ] TTS失敗→ブラウザ読み上げ案内
- [ ] サイドバー: キャッシュクリア・履歴表示・再生
- [ ] PWA: manifest・SW登録・オフラインページ
- [ ] ダークモード・レスポンシブ表示
- [ ] 全ユニットテスト通過 (`pytest -v`)
- [ ] カバレッジ70%以上 (`pytest --cov=retro_radio`)