"""パスワードハッシュの検証。

Wave 1 で `hash_password` が PBKDF2-SHA256 600,000回 + 16バイトのランダムソルトに変更された。
旧フォーマット（100,000回 + salt.encode()）は後方互換で照合でき、
照合成功時に新強度へ再ハッシュ（transparent rehash）される。
"""

import hashlib
import hmac
import secrets

import pytest

from retro_radio.auth import authenticator as auth_mod
from retro_radio.auth.authenticator import (
    LEGACY_PBKDF2_ITERATIONS,
    PBKDF2_ITERATIONS,
    SALT_BYTES,
    UNLIMITED_GENERATIONS,
    Authenticator,
    hash_password,
    verify_password,
)

# NIST SP 800-63B 準拠のパスワードポリシー（`MIN_PASSWORD_LENGTH = 8`）を満たす値。
# ポリシーは `signup()` のみを対象とするため、`verify_password` / `hash_password`
# を直接呼ぶテストは意図的に短い文字列を使い続ける。
VALID_PASSWORD = "TestPass123"


def _legacy_hash(password: str) -> str:
    """Wave 1 以前のフォーマット（100,000回 + salt.encode()）のハッシュを再現する"""
    salt = secrets.token_bytes(SALT_BYTES).hex()
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt.encode("utf-8"), LEGACY_PBKDF2_ITERATIONS
    ).hex()
    return f"{digest}:{salt}"


# --- 新フォーマット -------------------------------------------------------------
def test_hash_and_verify():
    hashed = hash_password("securepassword123")
    assert verify_password("securepassword123", hashed) is True
    assert verify_password("wrongpassword", hashed) is False


def test_hash_format():
    """`sha256hex(64) : salthex(32)` 形式"""
    hashed = hash_password("test")
    hash_part, _, salt_part = hashed.partition(":")
    assert len(hash_part) == 64
    assert len(salt_part) == 32
    int(hash_part, 16)
    int(salt_part, 16)


def test_hash_uses_current_iteration_count():
    """ハッシュが PBKDF2_ITERATIONS 回で計算されている"""
    password = "iteration-check"
    salt = secrets.token_bytes(SALT_BYTES)
    expected = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS
    ).hex()
    assert verify_password(password, f"{expected}:{salt.hex()}") is True


def test_salt_is_unique_per_hash():
    """同じパスワードでもソルトは毎回異なる（同一ハッシュが並ばない）"""
    assert hash_password("same") != hash_password("same")


def test_hash_iteration_constants_are_hardened():
    """リグレッション: 反復回数が 600,000 に、中間値まで下げられている"""
    assert PBKDF2_ITERATIONS >= 600_000
    assert SALT_BYTES >= 16


# --- 旧フォーマット（後方互換） ---------------------------------------------------
def test_legacy_hash_is_still_verifiable():
    """旧フォーマットで保存済みのパスワードを照合できる（回帰防止）"""
    password = "old-style-password"
    legacy = _legacy_hash(password)
    assert verify_password(password, legacy) is True
    assert verify_password("not-the-password", legacy) is False


def test_legacy_verification_flags_rehash_needed():
    """旧フォーマットは照合できるが再ハッシュが必要と判定される"""
    password = "old-style-password"
    matched, needs_rehash = auth_mod._verify_password(password, _legacy_hash(password))
    assert matched is True
    assert needs_rehash is True


def test_current_hash_does_not_need_rehash():
    """新フォーマットは再ハッシュ不要"""
    matched, needs_rehash = auth_mod._verify_password("pw", hash_password("pw"))
    assert matched is True
    assert needs_rehash is False


def test_login_transparently_rehashes_legacy_password(db_session):
    """旧ハッシュでログインすると新強度へ移行される"""
    password = "legacy-secret"
    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("legacy@example.com", password)

    from retro_radio.db.repository import UserRepository

    repo = UserRepository(db_session)
    stored = repo.get_by_email("legacy@example.com")
    legacy = _legacy_hash(password)
    stored.hashed_password = legacy
    repo.update(stored)
    db_session.commit()

    session_auth = Authenticator(db=db_session)
    logged_in = session_auth.login("legacy@example.com", password)
    assert logged_in is not None
    assert logged_in.id == user.id

    new_hash = UserRepository(db_session).get_by_email("legacy@example.com").hashed_password
    assert new_hash != legacy
    assert new_hash != _legacy_hash(password), "ソルトが同じだと再ハッシュにならない"
    assert verify_password(password, new_hash) is True


# --- タイミング攻撃対策 -----------------------------------------------------------
def test_verify_password_uses_constant_time_compare(monkeypatch):
    """`hmac.compare_digest` による定数時間比較を使っている（タイミング攻撃対策）"""
    calls = []
    real_compare_digest = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real_compare_digest(a, b)

    monkeypatch.setattr(auth_mod.hmac, "compare_digest", spy)

    assert verify_password("pw", hash_password("pw")) is True
    assert calls, "compare_digest が呼ばれていない（== 比較になっている疑い）"
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_verify_password_rejects_malformed_hashes():
    """壊れたハッシュは例外を投げず False"""
    for broken in ("", "no-colon", ":salt", "hash:", "x:y"):
        assert verify_password("pw", broken) is False


def test_verify_password_rejects_non_hex_salt():
    """16進数として解釈できないソルトは旧フォーマット照合を試み、False になる"""
    salt = "z" * 32
    digest = hashlib.pbkdf2_hmac(
        "sha256", b"pw", salt.encode("utf-8"), LEGACY_PBKDF2_ITERATIONS
    ).hex()
    assert verify_password("pw", f"{digest}:{salt}") is True  # 旧フォーマットとして通る
    assert verify_password("other", f"{digest}:{salt}") is False


# --- 課金ESCO association --------------------------------------------------------
def test_unlimited_generations_constant():
    """月間生成制限の撤廃（README 方針）を数値で固定する"""
    assert UNLIMITED_GENERATIONS == -1


def test_check_can_generate_always_allows(db_session):
    """check_can_generate は常に (True, -1) を返す"""
    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("can@example.com", VALID_PASSWORD)
    user.generation_count = 999

    assert auth.check_can_generate(user) == (True, UNLIMITED_GENERATIONS)
    assert auth.check_can_generate(None) == (True, UNLIMITED_GENERATIONS)


def test_upgrade_to_premium_does_not_grant_plan(db_session):
    """未課金で `upgrade_to_premium` を呼んでもプランは変わらない（課金は webhook のみ）"""
    from retro_radio.models.user import PlanType

    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("upgrade@example.com", VALID_PASSWORD)
    assert user.plan is PlanType.FREE

    url = auth.upgrade_to_premium(user, PlanType.PREMIUM)

    assert user.plan is PlanType.FREE, "購入前にプレミアム化する母親がある"
    assert auth.is_feature_enabled(user, "favorites") is False
    assert "/upgrade" in url
    assert "premium" in url

    from retro_radio.db.repository import UserRepository

    assert UserRepository(db_session).get_by_id(user.id).plan is PlanType.FREE


def test_upgrade_to_premium_does_not_write_to_db(db_session):
    """upgrade_to_premium は永続化も一切行わない"""
    from retro_radio.db.repository import UserRepository
    from retro_radio.models.user import PlanType

    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("upgrade2@example.com", VALID_PASSWORD)
    before = UserRepository(db_session).get_by_id(user.id).updated_at

    auth.upgrade_to_premium(user, PlanType.PRO, cycle="yearly")

    after = UserRepository(db_session).get_by_id(user.id)
    assert after.plan is PlanType.FREE
    assert after.updated_at == before


@pytest.mark.parametrize("plan_value", ["premium", "pro"])
def test_premium_feature_gates_stay_closed_without_payment(db_session, plan_value):
    """決済=webhook の責務。paywall 経路ではどの有料機能が有効化されない"""
    from retro_radio.models.user import PlanType

    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup(f"gate-{plan_value}@example.com", VALID_PASSWORD)
    auth.upgrade_to_premium(user, PlanType(plan_value))

    for feature in ("favorites", "history_export", "high_quality_audio", "no_ads", "api_access"):
        assert auth.is_feature_enabled(user, feature) is False, f"{feature} が開いてしまった"


def test_feature_gates_open_after_webhook_sets_plan(db_session):
    """webhook が plan を書き込んだ後は機能ゲートが開く（対比ケース）"""
    from retro_radio.models.user import PlanType

    auth = Authenticator(db=db_session)
    _ok, _msg, user = auth.signup("paid@example.com", VALID_PASSWORD)
    assert auth.is_feature_enabled(user, "favorites") is False

    user.plan = PlanType.PREMIUM
    assert auth.is_feature_enabled(user, "favorites") is True
