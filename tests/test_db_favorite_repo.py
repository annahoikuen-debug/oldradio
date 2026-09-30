"""`FavoriteRepository` とFavorite 関係_foreign key の検証。

Wave 1 で全 FK に `ondelete="CASCADE"` と `PRAGMA foreign_keys=ON` が追加された。
「お気に入りに登録済みの generation を消しても FK 違反にならない」ことを
回帰テストとして固定する。
"""

import pytest
from sqlalchemy.exc import IntegrityError

from retro_radio.db.models import Base, FavoriteModel, GenerationModel
from retro_radio.db.repository import FavoriteRepository, GenerationRepository


BASE_ENTRY = {
    "year": 2020,
    "month": 1,
    "day": 1,
    "script": "Script",
    "song_title": "Song",
    "artist_name": "Artist",
}


@pytest.fixture
def fav_repo(db_session, make_user, make_generation):
    user = make_user(db_session, "fav@example.com")

    def _gen(**overrides):
        return make_generation(db_session, user.id, **overrides)

    return FavoriteRepository(db_session), db_session, user, _gen


def test_add_favorite(fav_repo):
    favorites, db, user, make_gen = fav_repo
    gen = make_gen()

    assert favorites.add(user.id, gen.id) is True

    stored = db.query(FavoriteModel).filter_by(user_id=user.id, generation_id=gen.id).first()
    assert stored is not None
    assert stored.user_id == user.id
    assert stored.generation_id == gen.id


def test_add_duplicate_favorite_is_idempotent(fav_repo):
    favorites, _db, user, make_gen = fav_repo
    gen = make_gen()

    assert favorites.add(user.id, gen.id) is True
    assert favorites.add(user.id, gen.id) is False


def test_remove_favorite(fav_repo):
    favorites, db, user, make_gen = fav_repo
    gen = make_gen()
    favorites.add(user.id, gen.id)

    assert favorites.remove(user.id, gen.id) is True
    assert db.query(FavoriteModel).filter_by(user_id=user.id, generation_id=gen.id).first() is None


def test_remove_non_existent_favorite(fav_repo):
    favorites, _db, user, make_gen = fav_repo
    gen = make_gen()
    assert favorites.remove(user.id, gen.id) is False


def test_is_favorite(fav_repo):
    favorites, _db, user, make_gen = fav_repo
    gen = make_gen()

    assert favorites.is_favorite(user.id, gen.id) is False
    favorites.add(user.id, gen.id)
    assert favorites.is_favorite(user.id, gen.id) is True
    favorites.remove(user.id, gen.id)
    assert favorites.is_favorite(user.id, gen.id) is False


def test_get_user_favorites_newest_first(fav_repo):
    favorites, _db, user, make_gen = fav_repo
    first = make_gen(song_title="曲1")
    second = make_gen(song_title="曲2")
    favorites.add(user.id, first.id)
    favorites.add(user.id, second.id)

    result = favorites.get_user_favorites(user.id)
    assert len(result) == 2
    assert result[0] == second.id
    assert result[1] == first.id


def test_toggle_adds_then_removes(fav_repo):
    favorites, _db, user, make_gen = fav_repo
    gen = make_gen()

    assert favorites.toggle(user.id, gen.id) == (True, True)
    assert favorites.toggle(user.id, gen.id) == (False, True)
    assert favorites.is_favorite(user.id, gen.id) is False


def test_favorites_are_scoped_to_user(fav_repo):
    favorites, _db, user, make_gen = fav_repo
    gen = make_gen()
    favorites.add(user.id, gen.id)

    other_id = "someone-else"
    assert favorites.get_user_favorites(other_id) == []
    assert favorites.is_favorite(other_id, gen.id) is False


# --- FK CASCADE 回帰防止 ---------------------------------------------------------
def test_foreign_keys_pragmas_are_enabled(db_session):
    """SQLite の foreign_keys 強制が ON（これがないと CASCADE が効かない）"""
    from sqlalchemy import text

    enabled = db_session.execute(text("PRAGMA foreign_keys")).scalar()
    assert enabled == 1


def test_deleting_favorited_generation_does_not_raise_integrity_error(db_session, make_user, make_generation):
    """お気に入りに登録済みの generation を消しても IntegrityError にならない"""
    user = make_user(db_session, "cascade@example.com")
    user_id = user.id
    gen_id = make_generation(db_session, user_id).id
    FavoriteRepository(db_session).add(user_id, gen_id)
    db_session.commit()

    db_session.query(GenerationModel).filter_by(id=gen_id).delete()
    db_session.commit()
    db_session.expunge_all()

    assert db_session.query(FavoriteModel).filter_by(generation_id=gen_id).first() is None


def test_delete_old_with_favorites_does_not_raise(db_session, make_user, make_generation):
    """一括削除（delete_old）でも favorites が巻き添えで消え IntegrityError にならない"""
    user = make_user(db_session, "cascade2@example.com")
    favorites = FavoriteRepository(db_session)
    generations = [
        make_generation(db_session, user.id, day=day) for day in range(1, 6)
    ]
    for gen in generations:
        favorites.add(user.id, gen.id)
    db_session.commit()

    GenerationRepository(db_session).delete_old(user.id, keep=1)
    db_session.commit()

    remaining = db_session.query(FavoriteModel).filter_by(user_id=user.id).count()
    assert remaining == 1


def test_deleting_user_cascades_generations_and_favorites(db_session, make_user, make_generation):
    """ユーザー削除で generations / favorites が連鎖削除される"""
    from retro_radio.db.models import UserModel

    user = make_user(db_session, "cascade3@example.com")
    user_id = user.id
    gen_id = make_generation(db_session, user_id).id
    FavoriteRepository(db_session).add(user_id, gen_id)
    db_session.commit()

    db_session.query(UserModel).filter_by(id=user_id).delete()
    db_session.commit()
    db_session.expunge_all()

    assert db_session.query(GenerationModel).filter_by(id=gen_id).first() is None
    assert db_session.query(FavoriteModel).filter_by(user_id=user_id).first() is None


def test_foreign_key_violation_is_detected(db_session, make_user):
    """FK 制約が実際に効いていることの対照ケース（存在しない user_id は弾かれる）"""
    with pytest.raises(IntegrityError):
        db_session.add(
            FavoriteModel(id="orphan", user_id="no-such-user", generation_id="no-such-gen")
        )
        db_session.flush()
    db_session.rollback()


def test_models_declare_ondelete_cascade():
    """全 FK に ondelete='CASCADE' が宣言されている"""

    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            for fk in column.foreign_keys:
                assert fk.ondelete == "CASCADE", f"{table.name}.{column.name} に CASCADE が無い"


def test_favorites_relationship_is_delete_orphan(db_session, make_user, make_generation):
    """ORM 側にも delete-orphan が設定されている（DB 直接削除と整合する）"""
    from retro_radio.db.models import UserModel

    user = make_user(db_session, "cascade4@example.com")
    user_id = user.id
    make_generation(db_session, user_id)
    second_gen_id = make_generation(db_session, user_id).id
    FavoriteRepository(db_session).add(user_id, second_gen_id)
    db_session.commit()

    db_session.delete(db_session.query(UserModel).filter_by(id=user_id).first())
    db_session.commit()
    db_session.expunge_all()

    assert db_session.query(GenerationModel).count() == 0
    assert db_session.query(FavoriteModel).count() == 0
