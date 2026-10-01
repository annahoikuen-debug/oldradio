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
    """Engine生成（初回利用時に遅延生成。import時はsettingsを読まない）

    `hide_parameters=True` について:
    SQLAlchemy の既定は `False` で、`str(exc)` に**バインドパラメータ**が
    `[parameters: ('1', 'b@x', 'pbkdf2_sha256$...')]` 形で含まれる。
    `authenticator.py` は例外を `logger.error(f"...: {e}")` でログに落とすため、
    同一 email の 2 並列 signup が UNIQUE 制約違反になると
    **パスワードハッシュと email（PII）が平文でアプリログに残る**。
    ログ閲覧者はオフライン総当たり、または pass-the-hash が可能になる。
    したがって engine 側で hide する（第 1 防衛線）。
    """
    database_url = _database_url()
    # PostgreSQLの場合
    if database_url.startswith("postgresql"):
        return create_engine(
            database_url,
            pool_size=5,
            max_overflow=10,
            pool_pre_ping=True,
            pool_recycle=3600,
            echo=False,
            hide_parameters=True,
        )
    # SQLiteの場合の特別設定
    engine = create_engine(
        database_url,
        connect_args=_get_connect_args(database_url),
        echo=False,
        hide_parameters=True,
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
    """テーブル作成（**開発専用**。本番は Alembic を使うこと）

    S4（提案⑧）が追加した privacy 系テーブルもここで一緒に作る。
    別 MetaData なので `Base.metadata` には含まれず、`create_all` を2回呼ぶ。
    既存テーブル（users / generations / favorites）の定義は変えない。

    .. warning::
       **この関数をリクエスト経路から呼んではいけない。**
       `create_all()` は Alembic の `alembic_version` を書き換えないため、
       先に init_db() を実行すると、その後 `alembic upgrade head` が
       ``table tenants already exists`` で**恒久的に失敗**する
       （実測。`Authenticator.__init__` が毎リクエストで呼んでいた）。
       スキーマの所有者は Alembic 一本に統一すること。
    """
    bind = get_engine()
    Base.metadata.create_all(bind=bind)
    from .privacy_models import create_privacy_tables

    create_privacy_tables(bind)


def schema_is_ready() -> bool:
    """アプリが**実際に必要とする**テーブルが揃っているか（読み取り専用）。

    ここで `init_db()` を呼ぶ诱惑は**避ける**: `create_all()` は
    `alembic_version` を書き換えないため、先に走らせるとその後の
    `alembic upgrade head` が ``table tenants already exists`` で
    **恒久的に失敗**する（解決しない。実測）。
    スキーマの所有者は Alembic 一本。

    ``alembic_version`` を判定材料にしない理由: その有無は
    「migration が適用されたか」であって「スキーマがあるか」ではない。
    実際の必要テーブルを見るほうが運用の実態に合い、
    ``create_all`` で組んだテスト用 DB も正しく判定できる。
    """
    from sqlalchemy import inspect

    present = set(inspect(get_engine()).get_table_names())
    return REQUIRED_TABLES <= present


#: アプリが「そのテーブルが無いと 500 / 503 になる」もの。
#: `create_all()` でも `alembic upgrade head` でも同じ集合が揃う。
#: privacy 系（`tenants` / `user_security`）は `require_auth=1` のときだけ
#: 必要で、個人モードでは参照されないため**ここに含めない**
#: （含めると `create_all` で組んだテスト DB が「未準備」扱いになる）。
REQUIRED_TABLES = frozenset({"users", "generations", "favorites"})


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
