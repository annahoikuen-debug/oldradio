import json
import warnings
from typing import Annotated, Any, List, Literal, Optional

from pydantic import Field, ConfigDict, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode

#: `secret_key` の最小文字数。セッション署名の HMAC 鍵としてそのまま使われるため、
#: 短いとオフライン総当たりで Cookie を偽造できてしまう。
MIN_SECRET_KEY_LENGTH = 32
#: `single_user_key` の最小文字数。個人モードの**唯一の**資格情報であり、
#: 当たれば 8 時間有効な署名済みセッション Cookie を発行される。
#: ここに床が無いと `RETRO_RADIO_SINGLE_USER_KEY=x` で警告 0 のまま起動し、
#: 無制限に総当たりすれば 8 時間有効な署名済みセッション Cookie を得る
#: （= 他人の原稿開示 / データ削除）。
MIN_SINGLE_USER_KEY_LENGTH = 32
from functools import lru_cache

from .utils.app_errors import AppError


class ConfigurationError(AppError):
    """設定が実行可能な状態にないことを表すエラー。

    `AppError` を継承する理由（かつ唯一の理由）:
    `server.app_error_handler` は `isinstance(exc, ConfigurationError)` で
    設定不備を **500** に振り分ける。`AppError` のサブクラスでないと
    この判定を素通りし、設定不備がクライアントに 400 として見える（原因が隠れる）。

    以前は `retro_radio.utils.errors.ConfigurationError`（`AppError` の子）と
    **二重定義**されていて、`config.require_secret_key()` が投げる本クラスが
    `app_error_handler` に届いていなかった。**本クラスが唯一の正本**。
    """

    def __init__(self, message: str, original: Exception | None = None) -> None:
        # 設定不備は利用者（運用者）に伝えるべきなので `user_message` に入れておく。
        # `technical_message` は既定で同じ文言になる。
        super().__init__(user_message=message, technical_message=message, original=original)


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
    # 個人利用モードの単一ベアラーートークン（RETRO_RADIO_SINGLE_USER_KEY）。
    # 空なら個人モードでも認証できない。ログには絶対に出さない。
    single_user_key: str = ""

    # 外部API (iTunes)
    itunes_limit: int = Field(default=50, ge=1, le=200)
    itunes_timeout_connect: int = Field(default=5, ge=1, le=30)
    itunes_timeout_read: int = Field(default=10, ge=1, le=60)
    # Apple アフィリエイトトークン（Apple Search Ads の `at` パラメータ）。
    # 未設定（空文字）なら付与しない。**Affiliate Program 登録済みの
    # トークンのみを入れること**。未登録のものを書くと Apple 側の計測が
    # 壊れるだけなので、未登録なら必ず空文字のままにする。
    itunes_affiliate_token: str = ""
    # キャンペーン識別子（`ct` パラメータ）。計測用。空なら付けない。
    itunes_affiliate_campaign: str = ""
    # 送客導線（「Apple Music で聴く」）を UI に出すか。
    # Apple の規約上、プレビューを自社アプリで使うなら Apple への
    # リンクを併せて提供することが望まれる。既定は有効。
    itunes_show_store_links: bool = True
    # iTunes の候補のうち、発売年が対象年から ±N 年以内のものを採用する。
    # 0 にすると発売年を気にせず先頭を使う（旧挙動）。
    itunes_year_tolerance: int = Field(default=1, ge=0, le=10)

    # Gemini (LLM)
    # gemini-3.5-flash-lite: GA（2026-07-21）。入力 1,048,576 / 出力 65,536 トークン。
    gemini_model: str = "gemini-3.5-flash-lite"
    gemini_temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    gemini_api_key: str = ""

    # TTS
    # エンジンの選択。`edge` は Microsoft Edge のニューラル音声（edge-tts）で
    # gTTS と比べjustedに自然。`auto` は edge-tts があれば edge、無ければ gTTS。
    # gTTS は連結調でロボット的なので。既定は auto（= 無償で高品質な方を自動採用）。
    tts_engine: Literal["auto", "edge", "gtts"] = "auto"
    # edge-tts の音声 ID。既定は落ち着いた女性声（`SUPPORTED_JA_VOICES` を参照）。
    tts_edge_voice: str = "ja-JP-NanamiNeural"
    # 話速 / ピッチ / 音量。これらはキャッシュキーに含まれる（変えると音声を作り直す）。
    tts_edge_rate: str = "+0%"
    tts_edge_pitch: str = "+0Hz"
    tts_edge_volume: str = "+0%"

    # gTTS 固有設定（edge エンジンでは tld / slow は使わない）
    tts_language: str = "ja"
    tts_slow: bool = False
    tts_tld: str = "co.jp"
    tts_cache_ttl_days: int = Field(default=7, ge=1, le=365)
    tts_cache_sweep_interval: int = Field(default=50, ge=1, le=100000)

    # gTTS（Google Translate の TTS）は短時間に連続すると 429 で弾かれる。
    # 1 回の /api/generate は「セグメント数 + 全体版」で最大 6 回が連続実行されるため、
    # 実際のネットワーク呼び出しの間に最低この秒数を空ける。0 で無効化。
    tts_min_interval_seconds: float = Field(default=1.0, ge=0.0, le=10.0)
    # 429 を受けたときの再試行までの待機秒数（空けずに叩くと必ず失敗する）。
    tts_retry_backoff_seconds: float = Field(default=2.5, ge=0.0, le=30.0)
    # 429 を受けた後、この秒数だけ gTTS の呼び出しを休止する（サーキットブレーカー）。
    # レート制限が IP 単位で恒久的なとき、毎回 6 回叩いても 1 バイトも取れずに
    # 要求が 20 秒以上かかることがあるため、そのあいだは即座に諦める。0 で無効化。
    tts_circuit_breaker_seconds: float = Field(default=120.0, ge=0.0, le=3600.0)

    # ElevenLabs (Premium TTS)
    elevenlabs_voice_id: str = "21m00Tcm4TlvDq8ikWAM"
    elevenlabs_model_id: str = "eleven_multilingual_v2"
    elevenlabs_api_key: str = ""

    # 年範囲（1950〜2025 が現実的に回顧できる期間）
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

    # 選曲ローテーションとプレビュー解決結果の永続化先（SQLite）。
    # 空文字のときは database_url と同じディレクトリに
    # ``retro_radio_song_store.db`` を作る。相対パスでも可。
    song_store_path: str = ""

    # 1 回の番組（1 パス）で実際に流す曲の下限。
    # build_playlist は「オープニング曲 + トークN + エンディング曲」で
    # N+1 スロットを作る。1 パスのトーク数は 5 なので 6 曲になる。
    # 曲数が足りないとフロントが同じ曲を繰り回し、1 パス内で
    # 同じ曲が 2 度流れることになるため、6 曲配信を既定にする。
    program_min_song_count: int = Field(default=6, ge=1, le=20)

    # 番組を何周するか（クライアント側の既定値。1〜5）。
    # 「オープニング曲→原稿→曲→原稿→ … ×N →エンディング曲」という
    # ラジオ番組のループ構造を既定で成立させる。
    #
    # **周回するパスごとに別々の曲を送る**ため、旧実装は 1 パスぶんの曲しか
    # バックエンドから返さず、同じパスがそのまま N 回再生されていた。
    # 結果として 1 回の放送の中で同じ曲が N 回流れていた。バックエンドは
    # 周回数ぶんの曲を用意し、`passes` としてパスごとに返す。
    program_loop_count: int = Field(default=3, ge=1, le=5)

    # リソース保護（gTTS/Gemini/iTunes はいずれもブロッキングHTTPのため同時実行数を制限）
    max_concurrent_generations: int = Field(default=2, ge=1, le=16)
    generation_wait_timeout: int = Field(default=30, ge=1, le=300)

    # 決定論（R2-03/04/06 コアの seed 設計）。
    # None は「決定論なし・現行挙動」（random モジュールのグローバル状態を使う）。
    # int を設定すると選曲・話題選択が「同一入力なら同一結果」になる
    # （テストの再現性・監査性が必要な施設運用・eval 用）。
    rng_seed: Optional[int] = None

    # TTS キャッシュの個数上限（CACHE-01）。TTL 判定に加えてテナント配下の
    # キャッシュファイル数がこの値を超えたら古い順に削除する。
    tts_cache_max_files: int = Field(default=2000, ge=100, le=100000)

    # --- クライアント IP の解決（R2-03: プロキシ背後のロックアウト） ---------------
    # ログインスロットリングの記録キーは `(email, IP)` の組。
    # リバースプロキシ（Fly / Render / nginx）の背後では
    # `request.client.host` が**プロキシ自身の IP** になり、
    # 全利用者が 1 つのキーに押し潰される（＝1 人の総当たりが全員を 429 で締め出す）。
    #
    # ただしヘッダーは利用者が自由に書けるため、**無条件に信頼してはいけない**。
    # 「誰が設定したか」を運営者が宣言したときだけ信頼する。
    # 既定 `None` = 信用しない（現行挙動 = 直結クライアントのみ）。
    #
    # 値は**利用者を表すエントリが右から何番目か**（1 始まり）。
    # `X-Forwarded-For: <利用者, <CDN>, ...>` の並びを前提にする。
    #   nginx 1 台  = ヘッダーは利用者の 1 件だけ → 1
    #   CDN + nginx = `利用者, CDN` の 2 件     → 2
    # 値が小さいと攻撃者が左端を偽装でき、大きいと正当な利用者まで偽装される。
    trusted_proxy_header: Optional[str] = None
    trusted_proxy_hops: int = Field(default=1, ge=1, le=10)

    # UI設定
    page_title: str = "レトロラジオ・タイムマシン"
    page_icon: str = "📻"
    layout: str = "centered"

    # 言語設定
    default_language: str = "ja"

    # 認証
    secret_key: str = ""

    # --- 施設運用 readiness（提案⑧・S4） --------------------------------------
    # `/api/generate` と `/api/audio/*` の認証要否。
    # **既定 1（安全側）**。個人利用で認証なしで動かす場合のみ 0 にする。
    # 既定を 0 にすると「施設にデプロイしたのに認証が無い」状態が
    # 何も言わずに成立してしまうため、既定はrequireする側に倒す。
    require_auth: bool = True

    # admin ロールの判定に使うメールアドレス（NoDecode: CSV 表記を緩く受ける）。
    # 恒久的な管理権限は users.role 側（マイグレーションで追加）を正とし、
    # ここは **ログイン前の初期管理者**（初回デプロイ時に手動で当てる/bootstrap用）。
    admin_emails: Annotated[List[str], NoDecode] = []

    # 利用規約の版。同意記録はこの版と紐づけて保存する。
    terms_version: str = "1.0.0"

    # 同意取得の強制。1 のとき、未同意の利用者は個人データを取り込む API を叩けない。
    # 0 は「同意記録自体を運用しない」個人利用モード。
    require_consent: bool = False

    # 論理削除の受付から完了までの運用 SLA（hours）。監査ログの運用指標。
    deletion_sla_hours: int = Field(default=24, ge=1, le=8760)

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

    @field_validator('admin_emails', mode="before")
    @classmethod
    def split_admin_emails(cls, v):
        """`RETRO_RADIO_ADMIN_EMAILS` を人の手で書くすべての表記からリストへ正規化する。

        List 型の設定は pydantic-settings が JSON と解釈するため、人が
        `a@example.com,b@example.com` と書くと起動できなくなる。
        `parse_cors_origins` と同じ方針でカンマ区切り / JSON 配列 / 単独を受け付ける。
        """
        parsed = parse_cors_origins(v)
        return [e.strip().lower() for e in parsed if e.strip()]

    @field_validator('rng_seed', mode="before")
    @classmethod
    def empty_rng_seed_is_none(cls, v):
        """`RETRO_RADIO_RNG_SEED=`（空）なら「未設定」= ``None`` にする。

        ``Optional[int]`` でも pydantic-settings は**空文字列を整数として
        解析**するため、`rng_seed: Optional[int] = None` だけでは
        `.env.example` をそのままコピーした環境で
        ``Input should be a valid integer, unable to parse string as an
        integer`` で**起動できなくなる**（`INT` 系の設定に同じ罠がある）。

        「空 = OS のエントロピー」が本設定の契約なので、空を未設定として扱う。
        """
        if v is None:
            return None
        if isinstance(v, str) and not v.strip():
            return None
        return v

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
    def validate_retry_wait_range(self) -> "Settings":
        """`retry_wait_min <= retry_wait_max` でなければ指数バックオフが破綻する。

        tenacity の `wait_exponential(multiplier=1, min=min_wait, max=max_wait)` は
        `min > max` のとき **例外を投げずに** `min` 側（= より大きい下限）へ丸める。
        つまり `min=30, max=5` と書くと「0〜30 秒待つ」になり、
        設定した `retry_wait_max=5` が黙って無視される。

        設定ミスとして**起動時に落とす**。警告だけだと運用者は気づかない。
        """
        if self.retry_wait_min > self.retry_wait_max:
            raise ValueError(
                f"retry_wait_min ({self.retry_wait_min}) が "
                f"retry_wait_max ({self.retry_wait_max}) を超えています。"
                "指数バックオフの上限が min に丸められ、"
                "RETRO_RADIO_RETRY_WAIT_MAX が黙って無視されます。"
            )
        return self

    @model_validator(mode="after")
    def validate_auth_consistency(self) -> "Settings":
        """認証を有効にしたまま資格情報が1つも無い状態を起動時に明示する。

        `require_auth=True` かつ `secret_key` も `single_user_key` も空のとき、
        画面ログインもベアラーートークンも**両方**成立しない。
        この状態は「認証を有効にしたつもりだが誰も認証できない」={
        「認証が無い」と同じなので、fail-closed 側の利用者をえるため
        警告を上げて [`auth_ready`] が False になるようにする。
        起動そのものは止めない（開発者が .env なしで起動できるようにするため）。
        実際の保護は `retro_radio.api.deps` が 503 で拒否する。
        """
        if self.require_auth and not self.auth_ready:
            warnings.warn(
                "認証が有効（RETRO_RADIO_REQUIRE_AUTH=1）ですが、"
                "RETRO_RADIO_SECRET_KEY も RETRO_RADIO_SINGLE_USER_KEY も未設定です。"
                "この状態では /api/generate と /api/audio/* が **すべて 503** を返します"
                "（require_auth_config() == 'unavailable'、fail-closed）。"
                "動作する設定は次のどちらか一方だけです。\n"
                "  (1) 個人利用（認証なし）: RETRO_RADIO_REQUIRE_AUTH=0 を設定する。"
                " .env.example が出荷する既定の構成です。\n"
                "  (2) 施設利用（認証あり）: RETRO_RADIO_REQUIRE_AUTH=1 のまま"
                " RETRO_RADIO_SECRET_KEY（画面ログイン）または"
                " RETRO_RADIO_SINGLE_USER_KEY（ベアラーートークン）を設定する。"
                " 鍵の生成: python -c \"import secrets;"
                " print(secrets.token_urlsafe(32))\"",
                RuntimeWarning,
                stacklevel=2,
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
        # `secret_key` だけを見ていたため、短い `single_user_key` では
        # 警告が 0 だった。個人モードの唯一の資格なので同じ扱いにする。
        for message in self.warn_insecure_keys():
            warnings.warn(message, RuntimeWarning, stacklevel=2)
        return self

    @property
    def auth_available(self) -> bool:
        """認証機能（セッション署名・パスワードリセット等）が使えるか。

        `secret_key` が空だと認証基盤は「動いているように見えるが何も署名できない」状態に
        なる。起動を止めずに、その状態を観測できるようにするのがこのプロパティ。
        """
        return bool(self.secret_key)

    @property
    def auth_ready(self) -> bool:
        """**認証を実際に成立させられる**か（資格情報が1つでも存在するか）。

        - 施設（オペレータ）モード: `secret_key` があれば画面ログイン + セッション Cookie。
        - 個人モード: `single_user_key` があれば単一ベアラーートークン。
        - どちらも無いなら `require_dependent` な保護は 503（fail-closed）。
        """
        # Round 3: `require_auth_config()` と一本化する。
        # 以前は `bool(secret_key) or bool(single_user_key)` だったので、
        # Round 2 で追加した「32 文字未満の `secret_key` は
        # `unavailable`」になるにも、`auth_ready is True` になって
        # 並列になっていた。
        # ここで「存在する」でなく**使える**による。
        if not self.require_auth:
            return False
        if self.secret_key:
            return len(self.secret_key) >= MIN_SECRET_KEY_LENGTH
        return len(self.single_user_key) >= MIN_SINGLE_USER_KEY_LENGTH

    @property
    def auth_disabled(self) -> bool:
        """`require_auth=0` で意図的に保護を切った状態か。"""
        return not self.require_auth

    def require_secret_key(self) -> str:
        """認証処理の入口で必ず呼ぶ。`secret_key` が無ければ、または**短ければ**、
        明示的な設定エラーにする。

        `Settings()` の生成自体は止めない（開発者が `.env` なしで動かせるようにするため）。
        拒絶するのは「認証を実際に使う瞬間」だけ。

        長さの検証が要る理由: このキーは PBKDF2/HMAC の入力として**そのまま**使われる
        （`auth/tokens.py` で raw）。``RETRO_RADIO_SECRET_KEY=abc`` だと
        セッション Cookie の HMAC が**1 猜測あたり 1 回**の計算で検証できてしまい、
        オフラインで総当たりすると **Cookie を偽造できる**。
        エラーメッセージは元から「32文字以上」と指示しているのに、
        **どこも長さを検査していなかった**。指示と検査が食い違っていた。
        """
        if not self.secret_key:
            raise ConfigurationError(
                "RETRO_RADIO_SECRET_KEY が未設定のため認証機能を使用できません。"
                f"{MIN_SECRET_KEY_LENGTH}文字以上のランダム値を .env に設定してください"
                "（生成例: python -c \"import secrets; print(secrets.token_urlsafe(48))\"）。"
            )
        if len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
            raise ConfigurationError(
                f"RETRO_RADIO_SECRET_KEY が短すぎます（現在 {len(self.secret_key)} 文字）。"
                f"セッション署名の HMAC 鍵として使うため、最低 {MIN_SECRET_KEY_LENGTH} 文字が"
                "必要です（生成例: python -c \"import secrets; "
                "print(secrets.token_urlsafe(48))\"）。"
            )
        return self.secret_key

    def require_auth_config(self) -> str:
        """保護対象エンドポイントの入口で必ず呼ぶ資格情報を返す。

        返り値の意味:
        - ``"session"``  : `secret_key` あり → 画面ログイン + セッション Cookie。
        - ``"bearer"``  : `secret_key` 無し / `single_user_key` あり → 単一ベアラートークン。
        - ``"disabled"``: `require_auth=0` → 意図的に保護を切った個人利用モード。
        - ``"unavailable"``: 認証を有効にしているのに資格情報が無い、または
          ``secret_key`` が短い → fail-closed。

        .. important::
           短すぎる ``secret_key`` を「資格情報がある」と見なすと、
           ``tokens.MIN_SECRET_LENGTH``（32）の検査を通り越す**正式な経路**に落ちる。
           実際には ``POST /api/auth/session`` が 500 を返し、既存 Cookie も
           ``invalid_session`` で全 API が 401 になる = **ログイン画面が開かない**。
        """
        if not self.require_auth:
            return "disabled"
        if self.secret_key:
            if len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
                return "unavailable"
            return "session"
        if self.single_user_key:
            # 短い `single_user_key` は「使える資格情報」ではない。
            # ここで `bearer` にすると、総当たりで当てた値が
            # 8 時間有効な署名済みセッション Cookie に化ける。
            if len(self.single_user_key) < MIN_SINGLE_USER_KEY_LENGTH:
                return "unavailable"
            return "bearer"
        return "unavailable"

    def require_single_user_key(self) -> str:
        """個人モードの資格情報を返す。**短すぎる場合は設定エラー**にする。

        `secret_key` と同じ方針（`require_secret_key`）。指示と検査が
        食い違わないよう、片方だけの床を残さない。
        """
        if not self.single_user_key:
            raise ConfigurationError(
                "RETRO_RADIO_SINGLE_USER_KEY が未設定のため個人モードの"
                "認証を使用できません。"
                f"{MIN_SINGLE_USER_KEY_LENGTH}文字以上のランダム値を .env に"
                "設定してください（生成例: python -c "
                "\"import secrets; print(secrets.token_urlsafe(48))\"）。"
            )
        if len(self.single_user_key) < MIN_SINGLE_USER_KEY_LENGTH:
            raise ConfigurationError(
                f"RETRO_RADIO_SINGLE_USER_KEY が短すぎます"
                f"（現在 {len(self.single_user_key)} 文字）。"
                f"個人モードの唯一の資格であるため、最低 "
                f"{MIN_SINGLE_USER_KEY_LENGTH} 文字が必要です"
                "（生成例: python -c \"import secrets; "
                "print(secrets.token_urlsafe(48))\"）。"
            )
        return self.single_user_key

    def warn_insecure_keys(self) -> List[str]:
        """短すぎる資格情報があれば警告文を返す（起動を止めない）。

        `secret_key` だけを見ていたため、`single_user_key` に
        1 文字の値を仕込んでも警告が 0 だった。
        """
        warnings_list: List[str] = []
        if self.secret_key and len(self.secret_key) < MIN_SECRET_KEY_LENGTH:
            warnings_list.append(
                f"RETRO_RADIO_SECRET_KEY が短すぎます（{len(self.secret_key)} 文字）。"
                f"最低 {MIN_SECRET_KEY_LENGTH} 文字が必要です。"
            )
        if self.single_user_key and len(self.single_user_key) < MIN_SINGLE_USER_KEY_LENGTH:
            warnings_list.append(
                f"RETRO_RADIO_SINGLE_USER_KEY が短すぎます"
                f"（{len(self.single_user_key)} 文字）。"
                f"最低 {MIN_SINGLE_USER_KEY_LENGTH} 文字が必要です。"
            )
        return warnings_list

    model_config = ConfigDict(
        env_file=".env",
        case_sensitive=False,
        env_prefix="RETRO_RADIO_",
        extra="ignore"
    )


@lru_cache()
def get_settings() -> Settings:
    return Settings()
