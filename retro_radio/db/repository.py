import json
import secrets
from datetime import timedelta
from typing import List, Optional, Union

from sqlalchemy.orm import Session

from .models import UserModel, GenerationModel, FavoriteModel, utcnow
from ..models.user import User as UserDomain, PlanType

_GENERATION_REQUIRED_FIELDS = ("year", "month", "day", "script", "song_title", "artist_name")


def normalize_email(email: str) -> str:
    """メールアドレスを**比較・保存の共通形**へ正規化する。

    `strip()` + `casefold()`。

    - `strip()`: 前後の空白。入力欄のコピペで混入りがち。
    - `casefold()`: `lower()` より強い畳み込み。メールアドレスの
      ローカル部分は仕様上 case-sensitive だが、実用上は同一視される
      ため `casefold()` を使う。Unicode の表記差（`ß` / `İ` など）も
      `lower()` の方が差を観測しやすい。

    正規化しないと `Alice@x` と `alice@x` が**別のアカウント**として
    残り、同一人物の同意・開示・履歴が分裂する。
    """
    return email.strip().casefold()


def _coerce_plan(value) -> PlanType:
    """ドメイン / ORM どちらの PlanType も素の文字列も単一の PlanType に揃える。

    DB とドメインで同じ enum を使うため変換は不要だが、呼び出し側が
    文字列を代入していた場合にも ValueError ではなく正規化できるようにしておく。
    """
    if isinstance(value, PlanType):
        return value
    return PlanType(value)

def _loads_or_empty(raw: Optional[str]):
    """壊れたJSONレコードで読み取り全体が失敗しないよう、decode失敗は空にフォールバックする"""
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []

def _dumps(value) -> str:
    if value is None:
        value = []
    return value if isinstance(value, str) else json.dumps(value)

class UserRepository:
    """repositoryは flush() までが責務。commit は get_db() contextmanager が行う。"""

    def __init__(self, db: Session):
        self.db = db
    
    def _to_domain(self, model: UserModel) -> UserDomain:
        return UserDomain(
            id=model.id,
            email=model.email,
            hashed_password=model.hashed_password,
            plan=_coerce_plan(model.plan),
            generation_count=model.generation_count,
            generation_reset_at=model.generation_reset_at,
            created_at=model.created_at,
            updated_at=model.updated_at
        )
    
    def _to_model(self, domain: UserDomain) -> UserModel:
        return UserModel(
            id=domain.id,
            email=domain.email,
            hashed_password=domain.hashed_password,
            plan=_coerce_plan(domain.plan),
            generation_count=domain.generation_count,
            generation_reset_at=domain.generation_reset_at,
            created_at=domain.created_at,
            updated_at=utcnow()
        )
    
    def get_by_email(self, email: str) -> Optional[UserDomain]:
        model = (
            self.db.query(UserModel)
            .filter(UserModel.email == normalize_email(email))
            .first()
        )
        return self._to_domain(model) if model else None
    
    def get_by_id(self, user_id: str) -> Optional[UserDomain]:
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        return self._to_domain(model) if model else None
    
    def create(self, email: str, hashed_password: str) -> UserDomain:
        now = utcnow()
        # **保存時も正規化する。** `get_by_email` は正規化して引くが、
        # ここで正規化しないと `Alice@x` と `alice@x` が**別々の行**として
        # 残ってしまう（`get_by_email` は両方を `alice@x` に取り、
        # 常に後勝ちの行しか返らない）。一意制約があっても
        # `Alice@x` と `alice@x` は別の値なので制約を満たしてしまう。
        # 結果として 1 人が 2 アカウントを持ち、同意・開示の状態が分裂する。
        normalized_email = normalize_email(email)
        domain = UserDomain(
            id=secrets.token_urlsafe(16),
            email=normalized_email,
            hashed_password=hashed_password,
            plan=PlanType.FREE,
            generation_count=0,
            generation_reset_at=now + timedelta(days=30),
            created_at=now,
            updated_at=now
        )
        model = self._to_model(domain)
        self.db.add(model)
        self.db.flush()
        return domain
    
    def update(self, domain: UserDomain) -> UserDomain:
        model = self.db.query(UserModel).filter(UserModel.id == domain.id).first()
        if not model:
            raise ValueError(f"User not found: {domain.id}")
        model.email = domain.email
        model.hashed_password = domain.hashed_password
        model.plan = _coerce_plan(domain.plan)
        model.generation_count = domain.generation_count
        model.generation_reset_at = domain.generation_reset_at
        model.updated_at = utcnow()
        self.db.flush()
        domain.updated_at = model.updated_at
        return domain
    
    def anonymize(self, user_id: str) -> bool:
        """論理削除の実行本体。`users` の行は**消さず**識別子だけ潰す。

        ## なぜ消さないか
        `generations.user_id` と `favorites.user_id` が `users.id` を
        外部キーとして参照しているため、行を消すと履歴が宙に浮くか
        ON DELETE CASCADE で連鎖削除される（= 開示の記録まで消える）。
        利用者の**識別可能性**だけを奪えば、開示・削除の目的は達成できる。

        ## 潰すもの
        - `email`            : 元のメールアドレスを潰す。**一意制約を満たす**必要があるため
          `deleted+<user_id>@invalid.example` 形式にする。
        - `hashed_password`  : 認証に使えない値（PBKDF2 の計算量を残さない）。
        """
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        if not model:
            return False
        model.email = f"deleted+{user_id}@invalid.example"
        model.hashed_password = "!"  # ログイン不可能なプレースホルダ
        model.updated_at = utcnow()
        self.db.flush()
        return True

    def get_raw(self, user_id: str) -> Optional[dict]:
        """削除判定などで、擬似ドメイン変換なしで生行を見るための読み取り。

        `get_by_id` は `hashed_password` を含むドメインを返すため
        ログや応答に載せる用途には使えない。ここでは最小限の項目だけを返す。
        """
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        if not model:
            return None
        return {
            "id": model.id,
            "email": model.email,
            "created_at": model.created_at,
            "updated_at": model.updated_at,
        }

    def find_by_stripe_customer(self, customer_id: Optional[str]) -> Optional[UserDomain]:
        """`stripe_customer_id` でユーザーを逆引きする（DB-02）。

        Stripe の Billing Portal / ダッシュボードから解約された場合、
        subscription の metadata に `user_id` が無いため、
        `customer_id` での逆引きが唯一の手がかりになる。無ければ ``None``。
        """
        if not customer_id:
            return None
        model = (
            self.db.query(UserModel)
            .filter(UserModel.stripe_customer_id == customer_id)
            .first()
        )
        if not model:
            return None
        return self._to_domain(model)

    def update_stripe_ids(self, user_id: str, customer_id: Optional[str] = None,
                          subscription_id: Optional[str] = None) -> bool:
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        if not model:
            return False
        if customer_id:
            model.stripe_customer_id = customer_id
        if subscription_id:
            model.stripe_subscription_id = subscription_id
        model.updated_at = utcnow()
        self.db.flush()
        return True

class GenerationRepository:
    """repositoryは flush() までが責務。commit は get_db() contextmanager が行う。"""

    def __init__(self, db: Session):
        self.db = db
    
    def _to_dict(self, model: GenerationModel) -> dict:
        return {
            "id": model.id,
            "user_id": model.user_id,
            "year": model.year,
            "month": model.month,
            "day": model.day,
            "script": model.script,
            "song_title": model.song_title,
            "artist_name": model.artist_name,
            "preview_url": model.preview_url,
            "audio_path": model.audio_path,
            "all_songs": _loads_or_empty(model.all_songs),
            "errors": _loads_or_empty(model.errors),
            "created_at": model.created_at
        }

    @staticmethod
    def _merge_payload(entry: Optional[dict], kwargs: dict) -> dict:
        data = dict(entry) if entry else {}
        for key, value in kwargs.items():
            if key in data and data[key] != value:
                raise ValueError(
                    f"Conflicting values for {key!r}: entry={data[key]!r}, kwargs={value!r}"
                )
            data[key] = value
        return data

    def create(self, user_id: str, entry: Optional[dict] = None, **kwargs) -> GenerationModel:
        """永続化した GenerationModel を返す。呼び出し側は model.id を使うこと。"""
        data = self._merge_payload(entry, kwargs)
        missing = [f for f in _GENERATION_REQUIRED_FIELDS if data.get(f) is None]
        if missing:
            raise ValueError(f"Missing required generation fields: {', '.join(missing)}")
        
        model = GenerationModel(
            id=secrets.token_urlsafe(16),
            user_id=user_id,
            year=int(data["year"]),
            month=int(data["month"]),
            day=int(data["day"]),
            script=data["script"],
            song_title=data["song_title"],
            artist_name=data["artist_name"],
            preview_url=data.get("preview_url"),
            audio_path=data.get("audio_path"),
            all_songs=_dumps(data.get("all_songs")),
            errors=_dumps(data.get("errors")),
        )
        self.db.add(model)
        self.db.flush()
        return model
    
    def get_by_user(self, user_id: str, limit: int = 10) -> List[dict]:
        # `created_at` だけだと同一秒の行が安定して並ばず、limit  truncation が
        # 実行ごとに変わります。主キー `id` をタイブレーカーに足して順序を安定させる。
        models = self.db.query(GenerationModel)\
            .filter(GenerationModel.user_id == user_id)\
            .order_by(GenerationModel.created_at.desc(), GenerationModel.id.desc())\
            .limit(limit).all()
        return [self._to_dict(m) for m in models]
    
    def get_by_id(self, gen_id: Union[str, GenerationModel]) -> Optional[dict]:
        if isinstance(gen_id, GenerationModel):
            gen_id = gen_id.id
        model = self.db.query(GenerationModel).filter(GenerationModel.id == gen_id).first()
        return self._to_dict(model) if model else None
    
    def delete_old(self, user_id: str, keep: int = 10) -> int:
        """古い履歴を削除（最新keep件残す）。削除した行数を返す。

        関連favoriteの削除はDB側ON DELETE CASCADEに委譲。
        戻り値は「実際に消した行数」。`DELETE /api/me` が
        削除請求の証明として件数を応答に載せるため必要。
        """
        query = self.db.query(GenerationModel).filter(GenerationModel.user_id == user_id)
        if keep > 0:
            keep_ids = [
                row[0]
                for row in (
                    self.db.query(GenerationModel.id)
                    .filter(GenerationModel.user_id == user_id)
                    .order_by(GenerationModel.created_at.desc(), GenerationModel.id.desc())
                    .limit(keep)
                    .all()
                )
            ]
            if not keep_ids:
                return 0
            query = query.filter(GenerationModel.id.notin_(keep_ids))
        return int(query.delete(synchronize_session=False) or 0)

class FavoriteRepository:
    """repositoryは flush() までが責務。commit は get_db() contextmanager が行う。"""

    def __init__(self, db: Session):
        self.db = db
    
    def add(self, user_id: str, generation_id: str) -> bool:
        existing = self.db.query(FavoriteModel)\
            .filter(FavoriteModel.user_id == user_id, FavoriteModel.generation_id == generation_id)\
            .first()
        if existing:
            return False
        model = FavoriteModel(
            id=secrets.token_urlsafe(16),
            user_id=user_id,
            generation_id=generation_id
        )
        self.db.add(model)
        self.db.flush()
        return True
    
    def remove(self, user_id: str, generation_id: str) -> bool:
        model = self.db.query(FavoriteModel)\
            .filter(FavoriteModel.user_id == user_id, FavoriteModel.generation_id == generation_id)\
            .first()
        if model:
            self.db.delete(model)
            self.db.flush()
            return True
        return False
    
    def toggle(self, user_id: str, generation_id: str) -> tuple[bool, bool]:
        """
        お気に入りの切り替え（トグル）
        Returns: (is_now_favorite, action_taken)
        """
        existing = self.db.query(FavoriteModel)\
            .filter(FavoriteModel.user_id == user_id, FavoriteModel.generation_id == generation_id)\
            .first()
        if existing:
            self.db.delete(existing)
            self.db.flush()
            return False, True  # removed
        else:
            model = FavoriteModel(
                id=secrets.token_urlsafe(16),
                user_id=user_id,
                generation_id=generation_id
            )
            self.db.add(model)
            self.db.flush()
            return True, True  # added
    
    def is_favorite(self, user_id: str, generation_id: str) -> bool:
        return self.db.query(FavoriteModel)\
            .filter(FavoriteModel.user_id == user_id, FavoriteModel.generation_id == generation_id)\
            .first() is not None
    
    def get_user_favorites(self, user_id: str) -> List[str]:
        models = self.db.query(FavoriteModel)\
            .filter(FavoriteModel.user_id == user_id)\
            .order_by(FavoriteModel.created_at.desc(), FavoriteModel.id.desc()).all()
        return [m.generation_id for m in models]
