import json
import secrets
from datetime import timedelta
from typing import List, Optional, Union

from sqlalchemy.orm import Session

from .models import UserModel, GenerationModel, FavoriteModel, utcnow
from ..models.user import User as UserDomain, PlanType

_GENERATION_REQUIRED_FIELDS = ("year", "month", "day", "script", "song_title", "artist_name")

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
        model = self.db.query(UserModel).filter(UserModel.email == email.lower()).first()
        return self._to_domain(model) if model else None
    
    def get_by_id(self, user_id: str) -> Optional[UserDomain]:
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        return self._to_domain(model) if model else None
    
    def create(self, email: str, hashed_password: str) -> UserDomain:
        now = utcnow()
        domain = UserDomain(
            id=secrets.token_urlsafe(16),
            email=email,
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
        models = self.db.query(GenerationModel)\
            .filter(GenerationModel.user_id == user_id)\
            .order_by(GenerationModel.created_at.desc())\
            .limit(limit).all()
        return [self._to_dict(m) for m in models]
    
    def get_by_id(self, gen_id: Union[str, GenerationModel]) -> Optional[dict]:
        if isinstance(gen_id, GenerationModel):
            gen_id = gen_id.id
        model = self.db.query(GenerationModel).filter(GenerationModel.id == gen_id).first()
        return self._to_dict(model) if model else None
    
    def delete_old(self, user_id: str, keep: int = 10):
        """古い履歴を削除（最新keep件残す）。関連favoriteの削除はDB側ON DELETE CASCADEに委譲"""
        query = self.db.query(GenerationModel).filter(GenerationModel.user_id == user_id)
        if keep > 0:
            keep_ids = [
                row[0] for row in self.db.query(GenerationModel.id)\
                    .filter(GenerationModel.user_id == user_id)\
                    .order_by(GenerationModel.created_at.desc())\
                    .limit(keep).all()
            ]
            if not keep_ids:
                return
            query = query.filter(GenerationModel.id.notin_(keep_ids))
        query.delete(synchronize_session=False)

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
            .order_by(FavoriteModel.created_at.desc()).all()
        return [m.generation_id for m in models]
