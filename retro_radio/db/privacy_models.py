"""施設運用 readiness のテーブル定義（提案⑧・S4）。

## なぜ `retro_radio.db.models.Base` ではなく **別の MetaData** なのか

既存テーブル（`users` / `generations` / `favorites`）は
`tests/test_db_models.py:11-14` が **テーブル集合の完全一致**、
`:16-23` が **`users` の列集合の完全一致** を検証している。
ここに列を足すと既存 2067 件が落ちるため、**既存テーブルは一切拡張しない**。

-roll / テナント所属 / 論理削除フラグは **`user_security`** という
別テーブルに「既存 user_id を主キーとして」持たせる。
つまり既存スキーマは読み取り専用のまま、
S4 の属性だけが新しいテーブルに載る。

## 外部キー設計の方針

- `tenants` への外部キー **は張る**（同一 MetaData 内なので移植可能）。
- `users` / `generations` への外部キー **は張らない**。
  理由:
  1. 参照先が別 MetaData にある（移植不可・方言差が出る）。
  2. `users.email` は論理削除時に匿名化するが、`ON DELETE CASCADE` は
     「行が消える」前提の規則であり、匿名化（行は残る）と相性が悪い。
     削除の実処理は [`retro_radio.db.privacy_repository.UserSecurityRepository`]
     が明示的に行う（`users` を行ごと消さない）。
- 全ての `user_id` は **インデックス付き**にして、`user_id` による絞り込みが
  テーブルスキャンにならないようにする。

標準ライブラリ + SQLAlchemy のみ。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import declarative_base

#: S4 が追加するテーブルのメタデータ。**既存 Base とは独立**。
PrivacyBase = declarative_base()

#: 個人利用（認証なし・テナント指定なし）で使う暗黙のテナント ID。
#: 「個人利用の世帯 = 1 テナント」という読み方。ログには必ずこの値が入るため、
#: 監査ログの `tenant_id` を NOT NULL にできる。
DEFAULT_TENANT_ID = "default"

#: `user_security.role` の取りうる値。
ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"
ALL_ROLES = (ROLE_MEMBER, ROLE_ADMIN)

#: `favorite_tracks.reaction` の取りうる値。
#: SQL の Enum は方言差が大きいため `String` + CheckConstraint で表現する
#: （既存 `users.plan` は Enum を使っているが、あれは revision 化済み）。
REACTION_VALUES = ("positive", "neutral", "negative")
DEFAULT_REACTION = "neutral"

#: `favorite_tracks.familiarity_score` の範囲。
#: `core.music_profile.MIN_FAMILIARITY` / `MAX_FAMILIARITY` と一致させる。
MIN_FAMILIARITY = 1
MAX_FAMILIARITY = 5
DEFAULT_FAMILIARITY = 3


def utcnow() -> datetime:
    """naive UTC。`retro_radio.db.models.utcnow` と同一形式（SQLite は tz を保持しない）。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class TenantModel(PrivacyBase):
    """テナント（= -Deployed 単位）。

    個人利用では `DEFAULT_TENANT_ID` の 1 行しか使わない。
    施設では 1 施設 = 1 テナント。グループ共有プロファイルは
    `music_profiles.group_id` が同じテナントを指すことで実現する。
    """

    __tablename__ = "tenants"

    id = Column(String(32), primary_key=True)
    name = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class UserSecurityModel(PrivacyBase):
    """既存 `users` 行に付随する S4 の属性（ロール / テナント / 論理削除）。

    `user_id` を主キーにすることで、`users` テーブルを一切触らずに
    「あの人は admin」「あの人はこの施設のもの」「あの人は削除済み」を表現する。
    この行が無いユーザーは「ロール未設定の一般利用者」として扱う
    （= `ROLE_MEMBER` 相当、`tenant_id` は `DEFAULT_TENANT_ID`）。
    """

    __tablename__ = "user_security"

    #: `users.id`（FK は張らない。理由はモジュール docstring を参照）
    user_id = Column(String(32), primary_key=True)
    role = Column(String(16), nullable=False, default=ROLE_MEMBER)
    #: 所属テナント。`NULL` は「個人利用（暗黙の default テナント）」。
    tenant_id = Column(String(32), nullable=True, index=True)
    #: 論理削除の受付日時。`NULL` なら生存。
    deletion_requested_at = Column(DateTime, nullable=True)
    #: 論理削除の完了日時（ anonymize 完了時点）。
    deleted_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        CheckConstraint(
            "role IN ('member', 'admin')",
            name="ck_user_security_role",
        ),
    )


class MusicProfileModel(PrivacyBase):
    """個人音楽プロファイル（`core.music_profile.MusicProfile` の永続化先）。

    - `owner_id` は **UNIQUE**（1 利用者 1 プロファイル）。
      `MusicProfileRepository.get()` はプロファイルが無い利用者にも
      空プロファイルを返すが、行は**/get のたびに作らない**。
      作ると `save()` との競合・冪等性違反が起きる。
    - `group_id` は施設向け。**NULL なら個人専用**、
      入れば同テナント内で共有できる（提案②の「グループ単位」）。
    """

    __tablename__ = "music_profiles"

    id = Column(String(32), primary_key=True)
    #: `users.id`。FK は張らない（既存テーブルを触らない方針）。
    owner_id = Column(String(32), nullable=False, unique=True, index=True)
    #: 共有グループ = テナント。`tenants.id` への FK。
    group_id = Column(String(32), nullable=True, index=True)
    #: 利用者が 10〜20 代だった年代（例: `[1970, 1980]`）。JSON 配列の文字列。
    teenage_decades = Column(Text, nullable=False, default="[]")
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)


class FavoriteTrackModel(PrivacyBase):
    """favorite 1 曲（`core.music_profile.FavoriteTrack` の永続化先）。

    `(owner_id, title, artist)` に UNIQUE 制約を張ることで、
    「読み取り→書き込み」の 2 手順を排し、原子的な 1 UPDATE で
    familiarity を減算できるようにする。
    """

    __tablename__ = "favorite_tracks"

    id = Column(String(32), primary_key=True)
    owner_id = Column(String(32), nullable=False, index=True)
    title = Column(String(500), nullable=False)
    artist = Column(String(500), nullable=False, default="")
    familiarity_score = Column(Integer, nullable=False, default=DEFAULT_FAMILIARITY)
    last_played_at = Column(DateTime, nullable=True)
    reaction = Column(String(16), nullable=False, default=DEFAULT_REACTION)
    created_at = Column(DateTime, default=utcnow, nullable=False)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("owner_id", "title", "artist", name="uq_favorite_tracks_owner_title_artist"),
        CheckConstraint(
            "familiarity_score >= 1 AND familiarity_score <= 5",
            name="ck_favorite_tracks_familiarity",
        ),
        CheckConstraint(
            "reaction IN ('positive', 'neutral', 'negative')",
            name="ck_favorite_tracks_reaction",
        ),
    )


class ConsentModel(PrivacyBase):
    """利用規約への同意記録。

    同意は **版（`terms_version`）単位**で保存する。版を上げると
    過去の実諾では現在の版に同意していないと判定できるため、
    運用（同意の撤回・再同意）が可能になる。
    """

    __tablename__ = "consents"

    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), nullable=False, index=True)
    tenant_id = Column(String(32), nullable=False, default=DEFAULT_TENANT_ID, index=True)
    terms_version = Column(String(32), nullable=False)
    accepted = Column(Boolean, nullable=False)
    accepted_at = Column(DateTime, nullable=False, default=utcnow)
    #: 同意の撤回。撤回時刻が入ると「現在の版に同意していない」になる。
    withdrawn_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_consents_user_version", "user_id", "terms_version"),
    )


class AuditLogModel(PrivacyBase):
    """監査ログ（生成・再生・同意・削除の各イベント）。

    `tenant_id` は **NOT NULL**。「テナント不明のイベント」を作れないように
    することで、「誰の識別情報が、いつ、どこで扱われたか」を必ず追えるようにする。
    個人利用でも `DEFAULT_TENANT_ID` が入る。
    """

    __tablename__ = "audit_logs"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), nullable=False, index=True)
    user_id = Column(String(32), nullable=True, index=True)
    #: `generation` / `playback` / `consent` / `deletion` / `auth` / `export` など。
    action = Column(String(64), nullable=False, index=True)
    #: 対象の種類（`generation` / `user` / `consent` / `audio`）。
    resource_type = Column(String(64), nullable=True)
    #: 対象 ID。
    resource_id = Column(String(64), nullable=True)
    #: `success` / `denied` / `failure`。
    outcome = Column(String(16), nullable=False, default="success")
    #: 付随情報（JSON）。**個人データ（氏名・生年など）を入れないこと。**
    meta_json = Column(Text, nullable=False, default="{}")
    created_at = Column(DateTime, default=utcnow, nullable=False)

    __table_args__ = (
        Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),
        Index("ix_audit_logs_tenant_action", "tenant_id", "action"),
    )


#: 論理削除の purge 対象（削除要求を受けたときに消す個人データ）。
#: `users` / `generations` / `favorites` は行を消さず anonymize する。
PURGE_TABLES = (
    FavoriteTrackModel,
    MusicProfileModel,
    ConsentModel,
    UserSecurityModel,
)


def create_privacy_tables(bind) -> None:
    """S4 のテーブルを作る（`init_db` から呼ばれる）。既存テーブルは触らない。"""
    PrivacyBase.metadata.create_all(bind=bind)


def drop_privacy_tables(bind) -> None:
    """S4 のテーブルだけを落とす（テスト用）。"""
    PrivacyBase.metadata.drop_all(bind=bind)


__all__ = [
    "PrivacyBase",
    "TenantModel",
    "UserSecurityModel",
    "MusicProfileModel",
    "FavoriteTrackModel",
    "ConsentModel",
    "AuditLogModel",
    "PURGE_TABLES",
    "DEFAULT_TENANT_ID",
    "ROLE_MEMBER",
    "ROLE_ADMIN",
    "ALL_ROLES",
    "REACTION_VALUES",
    "DEFAULT_REACTION",
    "MIN_FAMILIARITY",
    "MAX_FAMILIARITY",
    "DEFAULT_FAMILIARITY",
    "create_privacy_tables",
    "drop_privacy_tables",
    "utcnow",
]
