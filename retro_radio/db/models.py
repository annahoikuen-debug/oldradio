from sqlalchemy import (
    Column, String, Integer, DateTime, Text, ForeignKey, Enum as SQLEnum, Index
)
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime, timezone
import secrets
import threading
import time

from ..models.user import PlanType

Base = declarative_base()

#: `new_row_id()` のミリ秒プレフィックスを配るときの直列化。
_id_lock = threading.Lock()
#: 最後に配ったミリ秒（時計が進まないときも**必ず増える**ようにする）。
_last_id_millis = 0


def new_row_id() -> str:
    """**時刻でソートできる**行 ID（13 桁の時刻プレフィックス + 16 文字の乱数）。

    なぜ通常の ``secrets.token_urlsafe(16)`` では駄目か
    --------------------------------........................
    履歴（`GenerationRepository.get_by_user`）やお気に入り・同意履歴・監査ログは
    ``ORDER BY <時刻> DESC, id DESC`` で「新しい順」を組み立てる。
    つまり `id` は**時刻のタイブレーカー**であり、ここがランダムだと
    同じタイムスタンプの行の順序が**毎回入れ替わる**。

    Windows のシステム時計は 15.6 ms 刻みで（実測: `time.monotonic()` の
    連続 2 万回の読み取りが全て同一値）、背中合わせに insert した 2 行が
    同じ `created_at` になることは珍しくない。実測で履歴の並び替えが
    8 回中 3 回逆順になり、そのテストが 25% の確率で赤になっていた。

    プレフィックスは **プロセス内で必ず単調増加**する（時計が同じ値のまま
    止まっていても ``_last_id_millis + 1`` へ進める）。したがって
    `id DESC` は作成順の降順になり、タイブレーカーとして正しく機能する。
    クロックが巻き戻った場合も単調性は保たれる。

    13 桁は Unix ミリ秒（10^13 ms ≈ 1970 年から数百年分）で、
    `String(32)` に収まる（13 + 16 = 29 文字）。
    """
    global _last_id_millis
    now = int(time.time() * 1000)
    with _id_lock:
        _last_id_millis = now if now > _last_id_millis else _last_id_millis + 1
        prefix = _last_id_millis
    return f"{prefix:013d}{secrets.token_hex(8)}"

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

class StripeWebhookEventModel(Base):
    """処理済み Stripe イベントの台帳（webhook の冪等化 / M-7）。

    Stripe は **2xx を返すまで同じイベントを再送**してくる（タイムアウト後の
    再送、ネットワーク断、Docker の再起動など）。また短時間に並行して
    届くこともある。処理済みの判定が無いと、同じ `checkout.session.completed`
    が 2 回処理され、さらに**古い** `customer.subscription.updated` が
    後から届くと、解約済みの利用者が**勝手にプレミアムへ復帰**する。

    `id` は Stripe のイベント ID（`evt_...`）。PK なので「処理済み」は
    INSERT の成否そのものになり、判定と記録が別トランザクションにならない。
    """
    __tablename__ = "stripe_webhook_events"

    #: Stripe のイベント ID（`evt_...`）。これが冪等性のキー。
    id = Column(String(64), primary_key=True)
    event_type = Column(String(120), nullable=False)
    #: 対象利用者（特定できなかったときは NULL）。古いイベントの棄却に使う。
    user_id = Column(String(32), nullable=True, index=True)
    #: Stripe 側のイベント発生時刻（Unix 秒）。**順序**の判定材料。
    #: None は「イベントに `created` が無い」ことを意味し、順序判定を無効にする。
    event_created = Column(Integer, nullable=True)
    received_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (Index("ix_stripe_webhook_events_user_created", "user_id", "event_created"),)
