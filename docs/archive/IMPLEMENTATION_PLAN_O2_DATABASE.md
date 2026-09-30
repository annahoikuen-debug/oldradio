# O2: データベース層 実装計画書

## 目的
インメモリDBをSQLite/PostgreSQL対応の永続化層に置き換え、ユーザー・生成履歴・お気に入りを永続管理する。

## 前提
- O1完了済み（AuthenticatorがインメモリDB使用中）
- 依存追加: `sqlalchemy>=2.0`, `alembic`, `python-dotenv`
- 環境変数: `DATABASE_URL` (例: `sqlite:///./retro_radio.db` または `postgresql://...`)

---

## ステップ 1～24

### Step 1: 依存関係追加・ディレクトリ作成
**作業**: 
```bash
pip install sqlalchemy==2.0.23 alembic==1.12.1 python-dotenv==1.0.0
mkdir -p retro_radio/db
```
**ファイル作成**: `retro_radio/db/__init__.py`, `retro_radio/db/models.py`, `retro_radio/db/session.py`, `retro_radio/db/repository.py`

---

### Step 2: SQLAlchemyモデル定義
**ファイル**: `retro_radio/db/models.py`
**作業**: 以下のテーブル定義
```python
from sqlalchemy import (
    Column, String, Integer, DateTime, Boolean, Text, ForeignKey, Enum as SQLEnum, Index
)
from sqlalchemy.orm import declarative_base, relationship
from datetime import datetime
import enum

Base = declarative_base()

class PlanTypeEnum(str, enum.Enum):
    FREE = "free"
    PREMIUM = "premium"
    PRO = "pro"

class UserModel(Base):
    __tablename__ = "users"
    
    id = Column(String(32), primary_key=True)
    email = Column(String(255), unique=True, nullable=False, index=True)
    hashed_password = Column(String(512), nullable=False)
    plan = Column(SQLEnum(PlanTypeEnum), default=PlanTypeEnum.FREE, nullable=False)
    stripe_customer_id = Column(String(100), nullable=True)
    stripe_subscription_id = Column(String(100), nullable=True)
    generation_count = Column(Integer, default=0, nullable=False)
    generation_reset_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    
    generations = relationship("GenerationModel", back_populates="user", cascade="all, delete-orphan")
    favorites = relationship("FavoriteModel", back_populates="user", cascade="all, delete-orphan")

class GenerationModel(Base):
    __tablename__ = "generations"
    
    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
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
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    user = relationship("UserModel", back_populates="generations")
    
    __table_args__ = (Index("ix_generations_user_created", "user_id", "created_at"),)

class FavoriteModel(Base):
    __tablename__ = "favorites"
    
    id = Column(String(32), primary_key=True)
    user_id = Column(String(32), ForeignKey("users.id"), nullable=False, index=True)
    generation_id = Column(String(32), ForeignKey("generations.id"), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    
    user = relationship("UserModel", back_populates="favorites")
    generation = relationship("GenerationModel")
    
    __table_args__ = (Index("ix_favorites_user_gen", "user_id", "generation_id", unique=True),)
```
**テスト作成**: `tests/test_db_models.py` - モデルインポート、テーブル定義確認

---

### Step 3: データベース接続・セッション管理
**ファイル**: `retro_radio/db/session.py`
**作業**: 
```python
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
import os
from .models import Base

_database_url = os.environ.get("DATABASE_URL", "sqlite:///./retro_radio.db")

# SQLiteの場合の特別設定
connect_args = {"check_same_thread": False} if _database_url.startswith("sqlite") else {}

engine = create_engine(_database_url, connect_args=connect_args, echo=False)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

def init_db():
    """テーブル作成（開発用・本番はAlembic使用）"""
    Base.metadata.create_all(bind=engine)

@contextmanager
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def get_db_sync() -> Session:
    """同期用（Streamlit同期コンテキストで使用）"""
    return SessionLocal()
```
**テスト作成**: `tests/test_db_session.py` - 接続・テーブル作成・セッション取得・ロールバック

---

### Step 4: リポジトリパターン実装（ユーザー）
**ファイル**: `retro_radio/db/repository.py`
**作業**: `UserRepository` クラス
```python
from typing import Optional
from sqlalchemy.orm import Session
from .models import UserModel, PlanTypeEnum
from ..models.user import User as UserDomain, PlanType
import secrets
from datetime import datetime, timedelta

class UserRepository:
    def __init__(self, db: Session):
        self.db = db
    
    def _to_domain(self, model: UserModel) -> UserDomain:
        return UserDomain(
            id=model.id,
            email=model.email,
            hashed_password=model.hashed_password,
            plan=PlanType(model.plan.value),
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
            plan=PlanTypeEnum(domain.plan.value),
            generation_count=domain.generation_count,
            generation_reset_at=domain.generation_reset_at,
            created_at=domain.created_at,
            updated_at=datetime.utcnow()
        )
    
    def get_by_email(self, email: str) -> Optional[UserDomain]:
        model = self.db.query(UserModel).filter(UserModel.email == email.lower()).first()
        return self._to_domain(model) if model else None
    
    def get_by_id(self, user_id: str) -> Optional[UserDomain]:
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        return self._to_domain(model) if model else None
    
    def create(self, email: str, hashed_password: str) -> UserDomain:
        domain = UserDomain(
            id=secrets.token_urlsafe(16),
            email=email,
            hashed_password=hashed_password,
            plan=PlanType.FREE,
            generation_count=0,
            generation_reset_at=datetime.utcnow() + timedelta(days=30),
            created_at=datetime.utcnow()
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
        model.plan = PlanTypeEnum(domain.plan.value)
        model.generation_count = domain.generation_count
        model.generation_reset_at = domain.generation_reset_at
        model.updated_at = datetime.utcnow()
        self.db.flush()
        return domain
    
    def update_stripe_ids(self, user_id: str, customer_id: str = None, subscription_id: str = None):
        model = self.db.query(UserModel).filter(UserModel.id == user_id).first()
        if model:
            if customer_id:
                model.stripe_customer_id = customer_id
            if subscription_id:
                model.stripe_subscription_id = subscription_id
            model.updated_at = datetime.utcnow()
            self.db.flush()
```
**テスト作成**: `tests/test_db_user_repo.py` - CRUD全操作、重複メール拒否、Stripe ID更新

---

### Step 5: リポジトリ実装（生成履歴）
**ファイル**: `retro_radio/db/repository.py` (続き)
**作業**: `GenerationRepository` クラス追加
```python
import json
from typing import List, Optional
from .models import GenerationModel

class GenerationRepository:
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
            "all_songs": json.loads(model.all_songs) if model.all_songs else [],
            "errors": json.loads(model.errors) if model.errors else [],
            "created_at": model.created_at
        }
    
    def create(self, user_id: str, entry: dict) -> str:
        import secrets
        gen_id = secrets.token_urlsafe(16)
        model = GenerationModel(
            id=gen_id,
            user_id=user_id,
            year=entry["year"],
            month=entry["month"],
            day=entry["day"],
            script=entry["script"],
            song_title=entry["song_title"],
            artist_name=entry["artist_name"],
            preview_url=entry.get("preview_url"),
            audio_path=entry.get("audio_path"),
            all_songs=json.dumps(entry.get("all_songs", [])),
            errors=json.dumps(entry.get("errors", [])),
        )
        self.db.add(model)
        self.db.flush()
        return gen_id
    
    def get_by_user(self, user_id: str, limit: int = 10) -> List[dict]:
        models = self.db.query(GenerationModel)\
            .filter(GenerationModel.user_id == user_id)\
            .order_by(GenerationModel.created_at.desc())\
            .limit(limit).all()
        return [self._to_dict(m) for m in models]
    
    def get_by_id(self, gen_id: str) -> Optional[dict]:
        model = self.db.query(GenerationModel).filter(GenerationModel.id == gen_id).first()
        return self._to_dict(model) if model else None
    
    def delete_old(self, user_id: str, keep: int = 10):
        """古い履歴を削除（最新keep件残す）"""
        subq = self.db.query(GenerationModel.id)\
            .filter(GenerationModel.user_id == user_id)\
            .order_by(GenerationModel.created_at.desc())\
            .limit(keep).subquery()
        self.db.query(GenerationModel)\
            .filter(GenerationModel.user_id == user_id)\
            .filter(~GenerationModel.id.in_(subq))\
            .delete(synchronize_session=False)
```
**テスト作成**: `tests/test_db_generation_repo.py` - 作成・取得・古い履歴削除・JSONシリアライズ

---

### Step 6: リポジトリ実装（お気に入り）
**ファイル**: `retro_radio/db/repository.py` (続き)
**作業**: `FavoriteRepository` クラス追加
```python
from typing import List, Optional
from .models import FavoriteModel

class FavoriteRepository:
    def __init__(self, db: Session):
        self.db = db
    
    def add(self, user_id: str, generation_id: str) -> bool:
        import secrets
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
    
    def is_favorite(self, user_id: str, generation_id: str) -> bool:
        return self.db.query(FavoriteModel)\
            .filter(FavoriteModel.user_id == user_id, FavoriteModel.generation_id == generation_id)\
            .first() is not None
    
    def get_user_favorites(self, user_id: str) -> List[str]:
        models = self.db.query(FavoriteModel)\
            .filter(FavoriteModel.user_id == user_id)\
            .order_by(FavoriteModel.created_at.desc()).all()
        return [m.generation_id for m in models]
```
**テスト作成**: `tests/test_db_favorite_repo.py` - 追加・削除・判定・一覧・重複防止

---

### Step 7: AuthenticatorをDB対応に変更
**ファイル**: `retro_radio/auth/authenticator.py`
**作業**: インメモリDB削除、リポジトリ使用に変更
```python
# インポート追加
from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import UserRepository

# コンストラクタ修正
def __init__(self):
    self.session_mgr = SessionManager()
    self._db = get_db_sync()
    self._user_repo = UserRepository(self._db)

# _get_user_by_email, _get_user_by_id, _save_user を削除し、リポジトリ呼び出しに置換
def _get_user_by_email(self, email: str) -> Optional[User]:
    return self._user_repo.get_by_email(email)

def _get_user_by_id(self, user_id: str) -> Optional[User]:
    return self._user_repo.get_by_id(user_id)

def _save_user(self, user: User):
    self._user_repo.update(user)
```
**テスト作成**: `tests/test_auth_db_integration.py` - 実DBでのログイン・登録・更新

---

### Step 8: history_serviceをDB対応に変更
**ファイル**: `retro_radio/services/history_service.py`
**作業**: 
```python
from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import GenerationRepository

def save_generation_result(entry: dict, user_id: str):
    db = get_db_sync()
    try:
        repo = GenerationRepository(db)
        repo.create(user_id, entry)
    finally:
        db.close()
```
**テスト作成**: `tests/test_history_service_db.py` - ユーザーID付き保存・取得確認

---

### Step 9: app.py 履歴取得をDB経由に変更
**ファイル**: `app.py` (履歴表示部分)
**作業**: セッションステートからDB取得に変更
```python
# サイドバー履歴表示
from retro_radio.db.session import get_db_sync
from retro_radio.db.repository import GenerationRepository

db = get_db_sync()
try:
    repo = GenerationRepository(db)
    history = repo.get_by_user(user.id, limit=10)
finally:
    db.close()

for i, entry in enumerate(history):
    with st.expander(f"{entry['year']}年{entry['month']}月{entry['day']}日 - {entry['song_title']}"):
        ...
```
**テスト作成**: `tests/test_app_history_db.py` - DBから履歴表示、10件制限

---

### Step 10: 再生機能をDB経由に変更
**ファイル**: `app.py` (replay_entry処理)
**作業**: 
```python
# セッションステートのreplay_entryはIDのみ保持
if st.button(f"再生", key=f"replay_{i}"):
    st.session_state.replay_entry_id = entry["id"]
    st.rerun()

# 再生処理
if st.session_state.get("replay_entry_id"):
    db = get_db_sync()
    try:
        repo = GenerationRepository(db)
        entry = repo.get_by_id(st.session_state.replay_entry_id)
        if entry:
            # 既存の復元ロジック
            script = entry["script"]
            ...
        st.session_state.replay_entry_id = None
    finally:
        db.close()
```
**テスト作成**: `tests/test_app_replay_db.py` - ID指定で正しい履歴取得

---

### Step 11: Alembicマイグレーション設定
**作業**: 
```bash
cd retro_radio
alembic init db/migrations
```
**ファイル編集**: `retro_radio/db/migrations/env.py`
```python
# target_metadata 設定
from retro_radio.db.models import Base
target_metadata = Base.metadata

# sqlalchemy.url を環境変数から取得
import os
config.set_main_option("sqlalchemy.url", os.environ.get("DATABASE_URL", "sqlite:///./retro_radio.db"))
```
**初期マイグレーション作成**:
```bash
alembic revision --autogenerate -m "Initial migration"
alembic upgrade head
```
**テスト作成**: `tests/test_db_migration.py` - マイグレーション適用・ロールバック確認

---

### Step 12: 開発用DB初期化スクリプト
**ファイル**: `scripts/init_db.py`
**作業**: 
```python
#!/usr/bin/env python
"""開発用DB初期化"""
import os
os.environ.setdefault("DATABASE_URL", "sqlite:///./retro_radio.db")

from retro_radio.db.session import init_db
from retro_radio.db.repository import UserRepository
from retro_radio.db.session import get_db_sync

if __name__ == "__main__":
    init_db()
    print("Database initialized")
```
**確認**: `python scripts/init_db.py` でテーブル作成

---

### Step 13: 単体テスト実行（DB層）
**作業**: 
```bash
pytest tests/test_db_models.py tests/test_db_session.py tests/test_db_user_repo.py tests/test_db_generation_repo.py tests/test_db_favorite_repo.py tests/test_db_migration.py -v
```
**完了基準**: 全テストパス

---

### Step 14: 統合テスト実行（Auth + DB）
**作業**: 
```bash
pytest tests/test_auth_db_integration.py tests/test_history_service_db.py tests/test_app_history_db.py tests/test_app_replay_db.py -v
```
**完了基準**: 全テストパス

---

### Step 15: 既存テスト回帰確認
**作業**: O1のテスト含む全テスト実行
```bash
pytest tests/ -v
```
**完了基準**: 全テストパス（リグレッションなし）

---

### Step 16: 接続プーリング設定（本番向け）
**ファイル**: `retro_radio/db/session.py`
**作業**: PostgreSQL用プール設定追加
```python
# PostgreSQLの場合
if _database_url.startswith("postgresql"):
    engine = create_engine(
        _database_url,
        pool_size=5,
        max_overflow=10,
        pool_pre_ping=True,
        pool_recycle=3600,
        echo=False
    )
```
**確認**: 負荷テストでコネクション枯渇しないこと

---

### Step 17: トランザクション境界の見直し
**作業**: 
- `get_db()` コンテキストマネージャ使用箇所の洗い出し
- 長時間保持箇所の修正（Streamlitの再実行モデルに合わせる）

---

### Step 18: インデックス最適化確認
**作業**: 
```sql
-- 実行計画確認
EXPLAIN QUERY PLAN SELECT * FROM generations WHERE user_id = ? ORDER BY created_at DESC LIMIT 10;
```
**確認**: インデックス使用されていること

---

### Step 19: バックアップ・リストア手順書
**ファイル**: `docs/db_backup.md`
**作業**: SQLite/PostgreSQLそれぞれのバックアップコマンド記載

---

### Step 20: マイグレーション運用ルール文書化
**ファイル**: `docs/db_migration.md`
**作業**: 
- 本番適用手順
- ロールバック手順
- 破壊的変更時の対応

---

### Step 21: パフォーマンステスト（簡易）
**作業**: 
```python
# tests/test_db_performance.py
def test_generation_insert_performance():
    import time
    repo = GenerationRepository(db)
    start = time.time()
    for i in range(100):
        repo.create(user_id, {...})
    assert time.time() - start < 5.0  # 100件5秒以内
```

---

### Step 22: 同時接続テスト（SQLite制限確認）
**作業**: 複数スレッドから同時アクセスでエラーにならないこと確認

---

### Step 23: 本番環境変数設定ガイド
**ファイル**: `DEPLOYMENT.md` に DATABASE_URL 設定追加

---

### Step 24: ドキュメント・完了報告
**作業**: 
- `README.md` にDB設定手順追加
- 完了チェックリスト全項目確認

---

## 完了定義
- [ ] 全24ステップ完了
- [ ] SQLite/PostgreSQL両対応動作確認
- [ ] Alembicマイグレーション適用・ロールバック確認
- [ ] 全テストパス（リグレッションなし）
- [ ] 100件履歴登録・取得が2秒以内