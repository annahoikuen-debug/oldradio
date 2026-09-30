"""DB トランザクション境界の回帰テスト。

Wave 1 で「repository は flush() まで、commit は `get_db()` contextmanager のみ」
という設計に統一された。ここでは:

1. `get_db()` が正常終了で commit / 例外で rollback することを固定する
2. repository が commit しないこと（部分失敗時に中途半端な状態が残らない）を固定する
3. `get_engine()` / `SessionLocal` の後方互換 import が引き続き生きていることを固定する
"""

import pytest
from sqlalchemy import inspect as sa_inspect, text

from retro_radio.db.models import Base, GenerationModel, UserModel
from retro_radio.db.repository import GenerationRepository, UserRepository


# --- get_db() のコミット境界 ----------------------------------------------------
def test_get_db_commits_on_success():
    from retro_radio.db.session import get_db, get_engine

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with get_db() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())

    with get_db() as db:
        UserRepository(db).create("commit@example.com", "hashed")

    with get_db() as db:
        assert UserRepository(db).get_by_email("commit@example.com") is not None
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())


def test_get_db_rolls_back_on_exception():
    """例外時に commit されない（データが永続化されない）"""
    from retro_radio.db.session import get_db, get_engine

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with get_db() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with get_db() as db:
            UserRepository(db).create("rollback@example.com", "hashed")
            raise Boom("fail after create")

    with get_db() as db:
        assert UserRepository(db).get_by_email("rollback@example.com") is None


def test_get_db_rolls_back_partial_generation():
    """生成レコード作成途中で失敗しても残らない（部分失敗の状態を保存しない）"""
    from retro_radio.db.session import get_db, get_engine

    engine = get_engine()
    Base.metadata.create_all(bind=engine)
    with get_db() as db:
        for table in reversed(Base.metadata.sorted_tables):
            db.execute(table.delete())

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom):
        with get_db() as db:
            user = UserRepository(db).create("partial@example.com", "hashed")
            GenerationRepository(db).create(
                user.id,
                {"year": 1980, "month": 5, "day": 15, "script": "s",
                 "song_title": "t", "artist_name": "a"},
            )
            raise Boom("fail after create")

    with get_db() as db:
        assert db.query(GenerationModel).count() == 0
        assert db.query(UserModel).filter_by(email="partial@example.com").first() is None


def test_repository_alone_does_not_commit(db_session, make_user, make_generation):
    """repository は flush() まで。commit しないこと"""
    user = make_user(db_session, "nocommit@example.com")
    gen = make_generation(db_session, user.id)

    # flush 済みなので同じセッション内では見える
    assert db_session.query(GenerationModel).filter_by(id=gen.id).first() is not None

    # ただし別セッション（= commit されていない証拠）からは見えない
    from retro_radio.db.session import get_db_sync

    other = get_db_sync()
    try:
        assert other.query(GenerationModel).filter_by(id=gen.id).first() is None
    finally:
        other.rollback()
        other.close()


# --- session.py の後方互換 -----------------------------------------------------
def test_engine_attribute_is_still_importable():
    """`from retro_radio.db.session import engine` が従来どおり動く"""
    from retro_radio.db.session import engine, get_engine

    assert engine is get_engine()


def test_session_local_attribute_is_still_importable():
    from retro_radio.db.session import SessionLocal, get_session_factory

    assert SessionLocal is get_session_factory()


def test_unknown_attribute_raises_attribute_error():
    import retro_radio.db.session as session_module

    with pytest.raises(AttributeError):
        session_module.definitely_not_a_real_attribute


def test_get_engine_is_cached():
    from retro_radio.db.session import get_engine

    assert get_engine() is get_engine()


def test_get_session_factory_is_cached():
    from retro_radio.db.session import get_session_factory

    assert get_session_factory() is get_session_factory()


def test_sqlite_pragmas_are_applied():
    """WAL と foreign_keys=ON が接続時に設定される"""
    from retro_radio.db.session import get_db_sync

    session = get_db_sync()
    try:
        assert session.execute(text("PRAGMA foreign_keys")).scalar() == 1
        assert session.execute(text("PRAGMA journal_mode")).scalar().lower() == "wal"
        assert session.execute(text("PRAGMA busy_timeout")).scalar() == 60000
    finally:
        session.close()


def test_get_db_sync_does_not_commit():
    """get_db_sync は commit しない（後方互換ラッパー）"""
    from retro_radio.db.session import get_db_sync

    session = get_db_sync()
    try:
        UserRepository(session).create("sync-nocommit@example.com", "hashed")
        session.rollback()
        assert UserRepository(session).get_by_email("sync-nocommit@example.com") is None
    finally:
        session.close()


# --- スキーマ ------------------------------------------------------------------
def test_all_tables_are_created():
    from retro_radio.db.session import get_engine

    Base.metadata.create_all(bind=get_engine())
    inspector = sa_inspect(get_engine())
    tables = set(inspector.get_table_names())
    assert {"users", "generations", "favorites"} <= tables


def test_indexes_exist_for_query_paths():
    """履歴取得・お気に入り取得で使うインデックスが定義されている"""
    from retro_radio.db.session import get_engine

    Base.metadata.create_all(bind=get_engine())
    inspector = sa_inspect(get_engine())

    generation_indexes = {i["name"] for i in inspector.get_indexes("generations")}
    favorite_indexes = {i["name"] for i in inspector.get_indexes("favorites")}
    assert "ix_generations_user_created" in generation_indexes
    assert "ix_favorites_user_gen" in favorite_indexes


def test_favorites_unique_constraint_exists():
    from retro_radio.db.session import get_engine

    Base.metadata.create_all(bind=get_engine())
    inspector = sa_inspect(get_engine())
    unique = {tuple(u["column_names"]) for u in inspector.get_indexes("favorites") if u["unique"]}
    assert ("user_id", "generation_id") in unique
