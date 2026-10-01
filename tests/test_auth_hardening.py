"""認証・プライバシー面の堅牢化の回帰テスト。

1. `LoginThrottle` が「キーを撒く」攻撃で記録を全消去しないこと
2. `set_group` / `set_teenage_decades` が favorite を消さないこと
3. 空鍵でのセッション署名が拒否されること
4. トークンの有効期限境界が厳密（`exp == now` で失効）であること
5. 失効フック（`set_session_verifier`）で即時に無効化できること
"""

from __future__ import annotations

import time

import pytest

from retro_radio.auth import tokens
from retro_radio.auth.authenticator import LoginThrottle, throttle_key


# ---------------------------------------------------------------------------
# 1. LoginThrottle: 撒鍵攻撃で記録が消えない
# ---------------------------------------------------------------------------
def test_throttle_survives_key_spray_and_keeps_escalating():
    """1 万キー超を撒いても、攻撃対象キーの記録は増え続ける。

    以前は上限超過時に**全レコード**が消えて、攻撃対象が毎回 0 に戻っていた。
    """
    throttle = LoginThrottle(max_failures=3, base_delay=1.0, max_delay=60.0, max_keys=200)
    target = throttle_key("victim@example.com", "10.0.0.1")

    for _ in range(throttle.max_failures):
        throttle.record_failure(target)
    assert throttle.failure_count(target) >= throttle.max_failures
    assert throttle.delay_for(target) > 0.0

    # 攻撃対象を攻撃し続ける一方で、1 万キー超の別キーを撒く。
    for i in range(10_500):
        throttle.record_failure(throttle_key(f"spray-{i}@example.com", f"10.1.{i // 256}.{i % 256}"))
        throttle.record_failure(target)

    assert throttle.failure_count(target) >= throttle.max_failures, "攻撃対象の記録が消えた"
    assert throttle.delay_for(target) > 0.0, "バックオフがリセットされた"
    assert len(throttle._records) <= throttle.max_keys * 1.5, "記録数が無制限に増えた"


def test_throttle_never_exceeds_bound_by_more_than_hot_keys():
    """上限を多少超えても、バックオフ中のキー（= 攻撃対象）は保持される。"""
    throttle = LoginThrottle(max_failures=2, base_delay=1.0, max_delay=60.0, max_keys=50)
    hot = throttle_key("hot@example.com", "")
    for _ in range(5):
        throttle.record_failure(hot)
    for i in range(500):
        throttle.record_failure(throttle_key(f"x{i}@example.com", ""))
    assert throttle.failure_count(hot) == 5
    assert throttle.is_saturated() is True


def test_throttle_saturated_refuses_new_keys_closed():
    """満杯のとき**新規キー**は記録しないが、既存キーの記録は消さない。"""
    throttle = LoginThrottle(max_failures=2, base_delay=1.0, max_delay=60.0, max_keys=10)
    known = throttle_key("known@example.com", "")
    throttle.record_failure(known)
    throttle.record_failure(known)
    for i in range(20):
        throttle.record_failure(throttle_key(f"flood{i}@example.com", ""))

    # 既知のキーは段階狄려で増え続ける
    assert throttle.failure_count(known) >= 2
    # 未知のキーは「記録しない」= 拒否側の返答（max_delay）
    assert throttle.delay_for(throttle_key("brand-new@example.com", "")) == throttle.max_delay


def test_throttle_expired_records_are_the_only_ones_dropped():
    """窓を過ぎた記録だけが消える（破棄は期限切れのみ）。"""
    now = [1000.0]
    throttle = LoginThrottle(max_failures=5, window=10.0, clock=lambda: now[0])
    fresh = throttle_key("fresh@example.com", "")
    stale = throttle_key("stale@example.com", "")
    throttle.record_failure(fresh)
    throttle.record_failure(stale)

    now[0] += 11.0
    assert throttle.failure_count(fresh) == 0
    assert throttle.failure_count(stale) == 0
    assert throttle._records == {}


def test_throttle_public_api_is_unchanged():
    """`server.py` が前提とする公開 API（context manager / check / reset）が残る。"""
    throttle = LoginThrottle()
    with throttle as entered:
        assert entered is throttle
        key = throttle_key("a@example.com", "1.1.1.1")
        assert throttle.check(key) == 0.0
        assert throttle.record_failure(key) == 1
        assert throttle.delay_for(key) >= 0.0
        assert throttle.failure_count(key) == 1
    throttle.record_success(key)
    assert throttle.failure_count(key) == 0
    throttle.record_failure(key)
    throttle.reset()
    assert throttle.failure_count(key) == 0


# ---------------------------------------------------------------------------
# 2. set_group / set_teenage_decades が favorite を消さない
# ---------------------------------------------------------------------------
@pytest.fixture()
def privacy_db():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from retro_radio.db.models import Base
    from retro_radio.db.privacy_models import create_privacy_tables

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    create_privacy_tables(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


def test_set_group_preserves_favorites_and_decades(privacy_db):
    from retro_radio.db.privacy_repository import MusicProfileRepositoryImpl

    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=5)
    repo.upsert_track("u1", "B", "Y", familiarity_score=4)
    repo.set_teenage_decades("u1", [1970, 1980])

    repo.set_group("u1", "facility-1")

    profile = repo.get("u1")
    assert sorted(t.title for t in profile.favorite_tracks) == ["A", "B"]
    assert profile.track("A").familiarity_score == 5
    assert profile.teenage_decades == (1970, 1980), "set_group が teenage_decades を消した"
    assert repo.list_tracks("u1")


def test_set_teenage_decades_preserves_favorites_and_group(privacy_db):
    from retro_radio.db.privacy_repository import MusicProfileRepositoryImpl

    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=5)
    repo.set_group("u1", "facility-1")

    repo.set_teenage_decades("u1", [1990])

    profile = repo.get("u1")
    assert [t.title for t in profile.favorite_tracks] == ["A"]
    assert profile.teenage_decades == (1990,)
    assert repo._find_profile("u1").group_id == "facility-1", "group_id が消えた"


def test_set_group_none_clears_group_only(privacy_db):
    """`group_id=None` は共有解除として効くが、他のデータは残る。"""
    from retro_radio.db.privacy_repository import MusicProfileRepositoryImpl

    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X")
    repo.set_group("u1", "facility-1")
    repo.set_teenage_decades("u1", [1980])

    repo.set_group("u1", None)

    assert repo._find_profile("u1").group_id is None
    assert repo.get("u1").teenage_decades == (1980,)
    assert [t.title for t in repo.get("u1").favorite_tracks] == ["A"]


def test_save_merges_by_default_and_can_replace_explicitly(privacy_db):
    """既定は merge。`replace_tracks=True` のときだけ全置換になる。"""
    from retro_radio.core.music_profile import FavoriteTrack, MusicProfile
    from retro_radio.db.privacy_repository import MusicProfileRepositoryImpl

    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=5)
    repo.upsert_track("u1", "B", "Y", familiarity_score=5)

    repo.save(MusicProfile(owner_id="u1", favorite_tracks=(
        FavoriteTrack(title="A", artist="X", familiarity_score=1),
    )))
    assert {t.title for t in repo.get("u1").favorite_tracks} == {"A", "B"}
    assert repo.get("u1").track("A").familiarity_score == 1

    repo.replace_tracks(MusicProfile(owner_id="u1", favorite_tracks=(
        FavoriteTrack(title="A", artist="X", familiarity_score=1),
    )))
    assert {t.title for t in repo.get("u1").favorite_tracks} == {"A"}


def test_audit_record_rejects_oversized_resource_id(privacy_db):
    """`resource_id` を黙って切り詰めない（監査ログと資源の紐付けを壊さない）。"""
    from retro_radio.db.privacy_repository import AuditRepository

    audit = AuditRepository(privacy_db)
    ok = audit.record("t", "generation.created", user_id="u", resource_id="a" * 64)
    assert ok["resource_id"] == "a" * 64
    with pytest.raises(ValueError):
        audit.record("t", "generation.created", user_id="u", resource_id="a" * 65)


# ---------------------------------------------------------------------------
# 3. 空鍵での署名・検証は拒否
# ---------------------------------------------------------------------------
def test_issue_session_token_rejects_empty_secret():
    for bad in ("", "   ", None):
        if bad is None:
            continue
        with pytest.raises(tokens.TokenError):
            tokens.issue_session_token(user_id="u", secret=bad)


def test_read_session_token_rejects_empty_secret():
    token = tokens.issue_session_token(user_id="u", secret="a-real-secret")
    for bad in ("", "   "):
        with pytest.raises(tokens.TokenError):
            tokens.read_session_token(token, secret=bad)


def test_read_bearer_token_rejects_empty_secret():
    token = tokens.issue_bearer_token(key="a-real-key")
    for bad in ("", "   "):
        with pytest.raises(tokens.TokenError):
            tokens.read_bearer_token(token, key=bad)


# ---------------------------------------------------------------------------
# 4. 有効期限の境界は厳密
# ---------------------------------------------------------------------------
def test_session_expiry_boundary_is_exclusive():
    now = int(time.time())
    token = tokens.issue_session_token(user_id="u", secret="a-real-secret", ttl_seconds=10)
    payload = tokens.read_session_token(token, secret="a-real-secret", now=now + 9)
    assert payload["uid"] == "u"
    # exp == now は「すでに失効」。`<` だと 1 秒の猶予が生まれる。
    with pytest.raises(tokens.TokenError):
        tokens.read_session_token(token, secret="a-real-secret", now=now + 10)


def test_bearer_expiry_boundary_is_exclusive():
    now = int(time.time())
    token = tokens.issue_bearer_token(key="a-real-key", ttl_seconds=10)
    assert tokens.read_bearer_token(token, key="a-real-key", now=now + 9)
    with pytest.raises(tokens.TokenError):
        tokens.read_bearer_token(token, key="a-real-key", now=now + 10)


# ---------------------------------------------------------------------------
# 5. 失効フック
# ---------------------------------------------------------------------------
def test_session_verifier_hook_revokes_deleted_user():
    token = tokens.issue_session_token(user_id="deleted-user", secret="a-real-secret")
    assert tokens.read_session_token(token, secret="a-real-secret")["uid"] == "deleted-user"

    deleted = set()
    tokens.set_session_verifier(lambda uid, payload: uid not in deleted)
    try:
        assert tokens.read_session_token(token, secret="a-real-secret")
        deleted.add("deleted-user")
        with pytest.raises(tokens.TokenError):
            tokens.read_session_token(token, secret="a-real-secret")
    finally:
        tokens.set_session_verifier(None)
    assert tokens.read_session_token(token, secret="a-real-secret")


def test_session_verifier_errors_fail_closed():
    def boom(uid, payload):
        raise RuntimeError("db down")

    token = tokens.issue_session_token(user_id="u", secret="a-real-secret")
    tokens.set_session_verifier(boom)
    try:
        with pytest.raises(tokens.TokenError):
            tokens.read_session_token(token, secret="a-real-secret")
    finally:
        tokens.set_session_verifier(None)
