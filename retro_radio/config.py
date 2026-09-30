import json
import warnings
from typing import Annotated, Any, List

from pydantic import Field, ConfigDict, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode
from functools import lru_cache


class ConfigurationError(RuntimeError):
    """設定が実行可能な状態にないことを表すエラー。

    `ValueError` を継承していないのは、設定不備が mere なバリデーション不備ではなく
    サーバーの起動可否に関わるため。呼び出し側で「設定ミス」と区別できるようにする。
    """


def parse_cors_origins(value: Any) -> List[str]:
    """CORS 許可オリジンを、人の手で書くすべての表記からリストへ正規化する。

    pydantic-settings は `List[str]` を環境変数から JSON として解釈するため、
    人が最初に書く `*` や `http://a,http://b` で `SettingsError` になり、
    アプリが起動できなくなる。ここでは `NoDecode` でパースを無効化し、
    JSON 配列 / カンマ区切り / ワイルドカード / 単独 origin をすべて受け付ける。
    """
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    if not isinstance(value, str):
        return value

    text = value.strip()
    if not text:
        return []
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(v).strip() for v in parsed if str(v).strip()]
    if text == "*":
        return ["*"]
    return [origin.strip() for origin in text.split(",") if origin.strip()]


class Settings(BaseSettings):
    # アプリケーション基本設定
    app_name: str = "Retro Radio Time Machine"
    app_version: str = "2.0.0"
    host: str = "0.0.0.0"
    port: int = 8501

    # データベース
    database_url: str = "sqlite:///./retro_radio.db"
    db_pool_recycle: int = Field(default=3600, ge=60, le=86400)

    # キャッシュ・リトライ
    cache_ttl: int = Field(default=3600, ge=60, le=86400)
    max_retries: int = Field(default=3, ge=1, le=10)
    retry_wait_min: int = Field(default=2, ge=1, le=60)
    retry_wait_max: int = Field(default=10, ge=1, le=30)
    retry_multiplier: int = Field(default=2, ge=1, le=5)

    # 外部API (iTunes)
    itunes_limit: int = Field(default=50, ge=1, le=200)
    itunes_timeout_connect: int = Field(default=5, ge=1, le=30)
    itunes_timeout_read: int = Field(default=10, ge=1, le=60)

    # Gemini (LLM)
    gemini_model: str = "gemini-2.5-flash"
    gemini_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    gemini_api_key: str = ""

    # TTS (gTTS)
    tts_language: str = "ja"
    tts_slow: bool = False
    tts_tld: str = "co.jp"
    tts_cache_ttl_days: int = Field(default=7, ge=1, le=365)
    tts_cache_sweep_interval: int = Field(default=50, ge=1, le=100000)

    # ElevenLabs (Premium TTS)
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_api_key: str = ""

    # 年範囲（1950〜2025 が现实に回忆できる期间）
    default_year: int = Field(default=1975, ge=1950, le=2025)
    min_year: int = Field(default=1950, ge=1950, le=2025)
    max_year: int = Field(default=2025, ge=1950, le=2025)

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_premium_monthly: str = "price_test_premium_monthly"
    stripe_price_premium_yearly: str = "price_test_premium_yearly"
    stripe_price_pro_monthly: str = "price_test_pro_monthly"
    stripe_price_pro_yearly: str = "price_test_pro_yearly"
    app_url: str = "http://localhost:8501"

    # デバッグ
    debug: bool = False

    # メドレー設定
    medley_song_count: int = Field(default=3, ge=1, le=10)
    target_script_chars: int = Field(default=1000, ge=500, le=2000)
    script_char_tolerance: int = Field(default=200, ge=50, le=500)

    # リソース保護（gTTS/Gemini/iTunes はいずれもブロッキングHTTPのため同時実行数を制限）
    max_concurrent_generations: int = Field(default=2, ge=1, le=16)
    generation_wait_timeout: int = Field(default=30, ge=1, le=300)

    # UI設定
    page_title: str = "レトロラジオ・タイムマシン"
    page_icon: str = "📻"
    layout: str = "centered"

    # 言語設定
    default_language: str = "ja"

    # 認証
    secret_key: str = ""

    # CORS設定（NoDecode: 環境変数を JSON と解釈せず precovalidator で正規化する）
    cors_origins: Annotated[List[str], NoDecode] = [
        "http://localhost:8501",
        "http://127.0.0.1:8501",
    ]
    cors_allow_credentials: bool = False

    @field_validator('cors_origins', mode="before")
    @classmethod
    def split_cors_origins(cls, v):
        return parse_cors_origins(v)

    @model_validator(mode="after")
    def validate_year_range(self) -> "Settings":
        """min_year <= max_year でなければ論理的に破綻した設定になる"""
        if self.min_year > self.max_year:
            raise ValueError(
                f"min_year ({self.min_year}) が max_year ({self.max_year}) を超えています"
            )
        if not (self.min_year <= self.default_year <= self.max_year):
            raise ValueError(
                f"default_year ({self.default_year}) は "
                f"[{self.min_year}, {self.max_year}] の範囲に収まっていない"
            )
        return self

    @model_validator(mode="after")
    def reject_cors_wildcard_with_credentials(self) -> "Settings":
        """`origins` に `*` と `allow_credentials=True` の組み合わせを拒否する。

        この組合せだとブラウザが攻撃者のオリジンを Access-Control-Allow-Origin に
        そのまま反射し、同時に Access-Control-Allow-Credentials: true を返すため、
        別オリジンのセッションCookie/Authorization を読み出せてしまう。
        `allow_credentials=False`（既定）なら `*` は安全なので受理する。
        """
        if self.cors_allow_credentials and "*" in self.cors_origins:
            raise ValueError(
                "CORS 設定が危険です: cors_origins に `*` と "
                "cors_allow_credentials=True は組み合わせられません"
                "（ブラウザは Access-Control-Allow-Origin を攻撃者に反射し、"
                "資格情報つきリクエストまで通してしまうため）。"
                "RETRO_RADIO_CORS_ORIGINS には具体的なオリジンを列挙してください。"
            )
        return self

    @model_validator(mode="after")
    def warn_insecure_secret_key(self) -> "Settings":
        # 空でも起動は妨げないが、認証機能が使えない状態であることを明示する
        if not self.secret_key:
            warnings.warn(
                "RETRO_RADIO_SECRET_KEY が未設定です。"
                "セッション/認証関連機能は利用できません（本番環境では必ず設定してください）。",
                RuntimeWarning,
                stacklevel=2,
            )
        return self

    @property
    def auth_available(self) -> bool:
        """認証機能（セッション署名・パスワードリセット等）が使えるか。

        `secret_key` が空だと認証基盤は「動いているように見えるが何も署名できない」状態に
        なる。起動を止めずに、その状態を観測できるようにするのがこのプロパティ。
        """
        return bool(self.secret_key)

    def require_secret_key(self) -> str:
        """認証処理の入口で必ず呼ぶ。`secret_key` 未設定なら明示的な設定エラーにする。

        `Settings()` の生成自体は止めない（開発者が `.env` なしで動かせるようにするため）。
        拒絶するのは「認証を実際に使う瞬間」だけ。
        """
        if not self.secret_key:
            raise ConfigurationError(
                "RETRO_RADIO_SECRET_KEY が未設定のため認証機能を使用できません。"
                "32文字以上のランダム値を .env に設定してください"
                "（生成例: python -c \"import secrets; print(secrets.token_urlsafe(48))\"）。"
            )
        return self.secret_key

    model_config = ConfigDict(
        env_file=".env",
        case_sensitive=False,
        env_prefix="RETRO_RADIO_",
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
