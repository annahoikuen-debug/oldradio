from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from functools import lru_cache
from ..config import get_settings
from .models import Base

def _database_url() -> str:
    return get_settings().database_url

def _get_connect_args(database_url: str) -> dict:
    """Get connection arguments with timeout for SQLite"""
    if database_url.startswith("sqlite"):
        return {"check_same_thread": False, "timeout": 60}
    return {}

def _configure_sqlite_wal(dbapi_connection, connection_record):
    """Configure SQLite WAL mode, foreign key enforcement and busy timeout"""
    if _database_url().startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON;")
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA busy_timeout=60000;")
        cursor.execute("PRAGMA synchronous=NORMAL;")
        cursor.close()

@lru_cache(maxsize=1)
def get_engine():
    """Engine生成（初回利用時に遅延生成。import時はsettingsを読まない）"""
    database_url = _database_url()
    # PostgreSQLの場合
    if database_url.startswith("postgresql"):
        return create_engine(
            database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False
        )
    # SQLiteの場合の特別設定
    engine = create_engine(
        database_url,
        connect_args=_get_connect_args(database_url),
        echo=False
    )
    event.listen(engine, "connect", _configure_sqlite_wal)
    return engine

@lru_cache(maxsize=1)
def get_session_factory():
    return sessionmaker(autocommit=False, autoflush=False, bind=get_engine())

def __getattr__(name: str):
    """後方互換: `from retro_radio.db.session import engine, SessionLocal` を遅延生成で維持する"""
    if name == "engine":
        return get_engine()
    if name == "SessionLocal":
        return get_session_factory()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

def init_db():
    """テーブル作成（開発用・本番はAlembic使用）"""
    Base.metadata.create_all(bind=get_engine())

@contextmanager
def get_db() -> Session:
    """セッションを開き、正常終了でcommit / 例外でrollback / 終了でcloseする。
    このcontextmanagerがアプリケーション内で唯一のcommit境界であり、
    repositoryは flush() までしか行わない。"""
    db = get_session_factory()()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

def get_db_sync() -> Session:
    """内部用（非推奨）。後方互換のためのラッパーで、commitは行わない。
    呼び出し側は必ず db.close() すること。新規コードは `with get_db() as db:` を使うこと。"""
    return get_session_factory()()

# For explicit context manager usage
get_db_context = get_db
