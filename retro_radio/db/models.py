from sqlalchemy import (
    Column, String, Integer, DateTime, Text, ForeignKey, Enum as SQLEnum, Index
)
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, timezone

from ..models.user import PlanType

Base = declarative_base()

def _enum_values(enum_cls):
    """Enum を DB に書くときの値を取り出す。

    SQLAlchemy の Enum は Python の enum クラスを渡すと既定で「メンバー名」を
    書き込む。PlanType のメンバー名は FREE/PREMIUM/PRO なので、 migration の
    sa.Enum("free", "premium", "pro", name="plantypeenum") と食い違い、
    PostgreSQL では invalid input value for enum plantypeenum: "FREE" で
    insert が全滅する。values_callable により .value（小文字）と一致させる。
    """
    return [member.value for member in enum_cls]

def utcnow():
    """naive UTC。SQLiteのDATETIMEはtzを保持せず文字列格納されるため、
    Column(DateTime) に格納する全タイムスタンプはこの形式に統一する。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

class UserModel(Base):
    __tablename__ = "users"
    
    id = Column(String(32), primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(512), nullable=False)
    plan = Column(
        SQLEnum(PlanType, name="plantypeenum", values_callable=_enum_values),
        default=PlanType.FREE,
        nullable=False,
    )
    stripe_customer_id = Column(String(100), nullable=True)
    stripe_subscription_id = Column(String(100), nullable=True)
    generation_count = Column(Integer, default=0, nullable=False)
    generation_reset_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)
    
    generations = relationship("GenerationModel", back_populates="user", cascade="all, delete-orphan", passive_deletes=True)
    favorites = relationship("FavoriteModel", back_populates="user", cascade="all, delete-orphan", passive_deletes=True)

class GenerationModel(Base):
    __tablename__ = "generations"
    
    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    day = Column(Integer, nullable=False)
    script = Column(Text, nullable=False)
    song_title = Column(String(500), nullable=False)
    artist_name = Column(String(500), nullable=False)
    preview_url = Column(String(1000), nullable=True)
    audio_path = Column(String(1000), nullable=True)
    all_songs = Column(Text, nullable=True)  # JSON文字列
    errors = Column(Text, nullable=True)     # JSON文字列
    created_at = Column(DateTime, default=utcnow, nullable=False)
    
    user = relationship("UserModel", back_populates="generations")
    favorites = relationship("FavoriteModel", back_populates="generation", cascade="all, delete-orphan", passive_deletes=True)
    
    __table_args__ = (Index("ix_generations_user_created", "user_id", "created_at"),)

class FavoriteModel(Base):
    __tablename__ = "favorites"
    
    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    generation_id = Column(String(32), ForeignKey("generations.id", ondelete="CASCADE"), nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    
    user = relationship("UserModel", back_populates="favorites")
    generation = relationship("GenerationModel", back_populates="favorites")
    
    __table_args__ = (Index("ix_favorites_user_gen", "user_id", "generation_id", unique=True),)
