from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

class PlanType(Enum):
    FREE = "free"
    PREMIUM = "premium"
    PRO = "pro"

    def __str__(self) -> str:
        """str() / f-string がメンバー名ではなく値を返すようにする。
        DB に保存される値・URL・JSON と同一の表記に揃えるため。"""
        return self.value

    @classmethod
    def _missing_(cls, value):
        """PlanType("pro") に加え、修正前に書き込まれた大文字（"PRO"）も受け付ける。

        SQLAlchemy の Enum は既定でメンバー名を DB に書くため、旧バージョンで
        作られた SQLite DB には 'FREE'/'PREMIUM'/'PRO' が残っている。
        読み取り経路がそれを見失わないようにしておく。
        """
        if isinstance(value, str):
            normalized = value.strip().lower()
            for member in cls:
                if normalized in (member.value, member.name.lower()):
                    return member
        return None

def utcnow() -> datetime:
    """naive UTC。DBのDateTimeカラムおよびbilling側のdatetime.utcnow()と揃える"""
    return datetime.now(timezone.utc).replace(tzinfo=None)

@dataclass
class User:
    id: str
    email: str
    hashed_password: str
    plan: PlanType = PlanType.FREE
    generation_count: int = 0
    generation_reset_at: Optional[datetime] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)
