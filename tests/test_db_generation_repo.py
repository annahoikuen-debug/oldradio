"""`GenerationRepository` の検証。

Wave 1 で `GenerationId` -generator 型が削除され、`create()` は
`GenerationModel`（ORM インスタンス）を返すようになった。旧テストの
`assertIsInstance(gen_id, str)` と `filter_by(id=gen_id)` は `model.id` を使う形に修正した。
"""

import json

import pytest

from retro_radio.db.models import GenerationModel
from retro_radio.db.repository import GenerationRepository, UserRepository


BASE_ENTRY = {
    "year": 2020,
    "month": 1,
    "day": 1,
    "script": "Test script",
    "song_title": "Test Song",
    "artist_name": "Test Artist",
}


@pytest.fixture
def repo(db_session, make_user):
    return GenerationRepository(db_session), make_user


def test_create_returns_generation_model(repo, db_session):
    """create() は GenerationModel を返す（str ではない）"""
    repository, make_user = repo
    user = make_user(db_session, "gen@example.com")

    created = repository.create(user.id, dict(BASE_ENTRY))

    assert isinstance(created, GenerationModel)
    assert isinstance(created.id, str)
    assert created.id != ""


def test_create_persists_all_fields(repo, db_session):
    repository, make_user = repo
    user = make_user(db_session, "fields@example.com")
    entry = dict(
        BASE_ENTRY,
        preview_url="http://example.com/preview",
        audio_path="/path/to/audio",
        all_songs=["Song1", "Song2"],
        errors=[],
    )

    gen = repository.create(user.id, entry)
    stored = db_session.query(GenerationModel).filter_by(id=gen.id).first()

    assert stored is not None
    assert stored.user_id == user.id
    assert (stored.year, stored.month, stored.day) == (2020, 1, 1)
    assert stored.script == "Test script"
    assert stored.song_title == "Test Song"
    assert stored.artist_name == "Test Artist"
    assert stored.preview_url == "http://example.com/preview"
    assert stored.audio_path == "/path/to/audio"
    assert json.loads(stored.all_songs) == ["Song1", "Song2"]
    assert json.loads(stored.errors) == []


def test_create_accepts_kwargs(repo, db_session):
    """entry 辞書を介さず kwargs でも作成できる"""
    repository, make_user = repo
    user = make_user(db_session, "kwargs@example.com")

    gen = repository.create(user.id, **BASE_ENTRY)
    assert repository.get_by_id(gen.id)["song_title"] == "Test Song"


def test_create_accepts_dict_of_songs(repo, db_session):
    """all_songs に dict のリストを渡しても JSON 化される"""
    repository, make_user = repo
    user = make_user(db_session, "dictsongs@example.com")

    gen = repository.create(user.id, dict(BASE_ENTRY, all_songs=[{"trackName": "A"}]))
    assert json.loads(gen.all_songs) == [{"trackName": "A"}]


def test_create_generation_id_type_is_not_string_alias(repo, db_session):
    """`GenerationId` が復活していない（Wave 1 で削除済み）"""
    import retro_radio.db.repository as repository_module

    assert not hasattr(repository_module, "GenerationId")
    repository, make_user = repo
    user = make_user(db_session, "type@example.com")
    gen = repository.create(user.id, dict(BASE_ENTRY))
    assert type(gen) is GenerationModel


@pytest.mark.parametrize(
    "missing", ["year", "month", "day", "script", "song_title", "artist_name"]
)
def test_create_rejects_missing_required_fields(repo, db_session, missing):
    """必須フィールド欠落は ValueError（silent default ではなく明示的に失敗する）"""
    repository, make_user = repo
    user = make_user(db_session, f"missing-{missing}@example.com")
    entry = dict(BASE_ENTRY)
    entry.pop(missing)

    with pytest.raises(ValueError, match="Missing required generation fields"):
        repository.create(user.id, entry)


def test_create_error_message_lists_all_missing(repo, db_session):
    repository, make_user = repo
    user = make_user(db_session, "missing-all@example.com")
    with pytest.raises(ValueError) as exc:
        repository.create(user.id, {})
    message = str(exc.value)
    for field in ("year", "month", "day", "script", "song_title", "artist_name"):
        assert field in message


def test_create_rejects_conflicting_entry_and_kwargs(repo, db_session):
    """entry と kwargs が矛盾する場合は ValueError（黙って上書きしない）"""
    repository, make_user = repo
    user = make_user(db_session, "conflict@example.com")
    entry = dict(BASE_ENTRY, song_title="entry側")

    with pytest.raises(ValueError, match="Conflicting values for 'song_title'"):
        repository.create(user.id, entry, song_title="kwargs側")


def test_create_allows_identical_entry_and_kwargs(repo, db_session):
    """値が同じなら衝突とみなさない"""
    repository, make_user = repo
    user = make_user(db_session, "same@example.com")
    entry = dict(BASE_ENTRY, song_title="同じ曲")

    gen = repository.create(user.id, entry, song_title="同じ曲")
    assert repository.get_by_id(gen.id)["song_title"] == "同じ曲"


def test_get_by_user_orders_newest_first(repo, db_session):
    repository, make_user = repo
    user = make_user(db_session, "history@example.com")
    repository.create(user.id, dict(BASE_ENTRY, day=1, song_title="Song1"))
    repository.create(user.id, dict(BASE_ENTRY, day=2, song_title="Song2"))

    generations = repository.get_by_user(user.id, limit=10)

    assert len(generations) == 2
    assert generations[0]["song_title"] == "Song2"
    assert generations[1]["song_title"] == "Song1"


def test_get_by_user_respects_limit(repo, db_session):
    repository, make_user = repo
    user = make_user(db_session, "limit@example.com")
    for day in range(1, 6):
        repository.create(user.id, dict(BASE_ENTRY, day=day))
    assert len(repository.get_by_user(user.id, limit=2)) == 2


def test_get_by_id_accepts_model_and_string(repo, db_session):
    """get_by_id は str と GenerationModel の両方を受け付ける"""
    repository, make_user = repo
    user = make_user(db_session, "byid@example.com")
    gen = repository.create(user.id, dict(BASE_ENTRY))

    by_model = repository.get_by_id(gen)
    by_str = repository.get_by_id(gen.id)

    assert by_model["id"] == gen.id
    assert by_str["id"] == gen.id
    assert by_str["song_title"] == "Test Song"


def test_get_by_id_returns_none_for_unknown(repo, db_session):
    repository, _ = repo
    assert repository.get_by_id("does-not-exist") is None


def test_get_by_user_is_scoped_to_user(repo, db_session):
    """他ユーザーの履歴は混ざらない"""
    repository, make_user = repo
    alice = make_user(db_session, "alice@example.com")
    bob = make_user(db_session, "bob@example.com")
    repository.create(alice.id, dict(BASE_ENTRY, song_title="Aliceの曲"))
    repository.create(bob.id, dict(BASE_ENTRY, song_title="Bobの曲"))

    assert [g["song_title"] for g in repository.get_by_user(alice.id)] == ["Aliceの曲"]
    assert [g["song_title"] for g in repository.get_by_user(bob.id)] == ["Bobの曲"]


def test_delete_old_keeps_latest(repo, db_session):
    repository, make_user = repo
    user = make_user(db_session, "delete@example.com")
    for day in range(1, 16):
        repository.create(user.id, dict(BASE_ENTRY, day=day))

    repository.delete_old(user.id, keep=5)
    remaining = repository.get_by_user(user.id, limit=20)

    assert len(remaining) == 5
    assert [g["day"] for g in remaining] == [15, 14, 13, 12, 11]


def test_delete_old_on_empty_user_is_noop(repo, db_session):
    repository, make_user = repo
    user = make_user(db_session, "empty@example.com")
    repository.delete_old(user.id, keep=5)
    assert repository.get_by_user(user.id) == []


def test_corrupt_json_does_not_break_read(repo, db_session):
    """壊れた JSON レコードが1件あっても一覧取得は失敗しない"""
    repository, make_user = repo
    user = make_user(db_session, "corrupt@example.com")
    gen = repository.create(user.id, dict(BASE_ENTRY))
    db_session.query(GenerationModel).filter_by(id=gen.id).update({"all_songs": "{not json"})
    db_session.expire_all()

    record = repository.get_by_id(gen.id)
    assert record["all_songs"] == []


def test_user_repository_update_rejects_unknown_id(db_session):
    from retro_radio.models.user import User

    ghost = User(id="missing", email="missing@example.com", hashed_password="h")
    with pytest.raises(ValueError):
        UserRepository(db_session).update(ghost)
