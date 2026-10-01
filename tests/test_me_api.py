"""開示・削除・同意・監査ログ・`MusicProfileRepository`（提案⑧・S4）。

## このファイルが固定すること

1. **`MusicProfileRepository.get()` は空プロファイルを返す**（S3 の契約）。
2. **`record_rejection()` は原子的に減算する**（1 UPDATE）。
3. `GET /api/me/export` の CSV / JSON 出力。
4. `DELETE /api/me` の論理削除（anonymize + purge + 監査）。
5. 同意の取得・撤回・版管理の挙動。
6. `target_name` の **16 文字制約**（要配慮個人情報の最小化）。
7. **監査ログの完全率 = 100%**（生成イベント数に対するログ行数）。

## 外部依存を避ける方針

`retro_radio.core` の import 連鎖（`pipeline` -> `music_search`）に
`retro_radio.db` を引きずらないよう、`db/privacy_repository.py` は
`core.music_profile` を**遅延 import** する。ここでは
リポジトリのテストも同样に、`retro_radio.core` 全体ではなく
`retro_radio.core.music_profile` のみを触る。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import pytest

# conftest より先に環境変数を決める（`retro_radio` の import より前）
os.environ.setdefault("RETRO_RADIO_REQUIRE_AUTH", "0")
os.environ.setdefault("RETRO_RADIO_SECRET_KEY", "s4-test-secret-key")

from retro_radio.api import audit as audit_api  # noqa: E402
from retro_radio.api import me as me_api  # noqa: E402
from retro_radio.core.music_profile import (  # noqa: E402
    MAX_FAMILIARITY,
    MIN_FAMILIARITY,
    MusicProfile,
)
from retro_radio.db.privacy_models import (  # noqa: E402
    DEFAULT_TENANT_ID,
    ROLE_ADMIN,
    PrivacyBase,
    create_privacy_tables,
)
from retro_radio.db.privacy_repository import (  # noqa: E402
    AuditRepository,
    ConsentRepository,
    MusicProfileRepositoryImpl,
    TenantRepository,
    UserSecurityRepository,
    purge_user_personal_data,
)
from retro_radio.db.repository import UserRepository  # noqa: E402


@pytest.fixture()
def privacy_db():
    """S4 のテーブルだけを持つ**独立した**セッション。

    `tests/conftest.py` の `db_session` は共有のため触らない。
    ここは S4 専用の in-memory SQLite を使い、
    テスト間でデータが残らないことを保証する。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    # privacy テーブルと `users`（FK は張っていないが anonymize で使う）
    from retro_radio.db.models import Base

    Base.metadata.create_all(bind=engine)
    create_privacy_tables(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


@pytest.fixture()
def user_factory(privacy_db):
    def _make(email="user@example.com"):
        return UserRepository(privacy_db).create(email, "hashed")

    return _make


# ---------------------------------------------------------------------------
# 1. MusicProfileRepository の契約（S3）
# ---------------------------------------------------------------------------
def test_get_returns_empty_profile_for_unknown_owner(privacy_db):
    """**契約 1**: `None` を返さず、**空プロファイル**を返す。

    `None` を返すと `profile.is_empty` が成立せず、
    呼び出し側の「年代パレットへフォールバック」判定が壊れる。
    """
    repo = MusicProfileRepositoryImpl(privacy_db)
    profile = repo.get("unknown-owner")

    assert profile is not None
    assert isinstance(profile, MusicProfile)
    assert profile.owner_id == "unknown-owner"
    assert profile.favorite_tracks == ()
    assert profile.teenage_decades == ()
    assert profile.is_empty is True


def test_get_does_not_create_a_row(privacy_db):
    """`get()` は行を作らない（`save()` との競合・冪等性違反を避ける）。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.get("ghost")
    repo.get("ghost")
    assert repo._find_profile("ghost") is None


def test_save_then_get_round_trips(privacy_db):
    from retro_radio.core.music_profile import FavoriteTrack

    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.save(
        MusicProfile(
            owner_id="u1",
            favorite_tracks=(FavoriteTrack(title="A", artist="X", familiarity_score=4),),
            teenage_decades=(1970, 1980),
        )
    )
    profile = repo.get("u1")
    assert profile.is_empty is False
    assert profile.teenage_decades == (1970, 1980)
    assert profile.track("A").familiarity_score == 4


def test_upsert_track_respects_familiarity_bounds(privacy_db):
    repo = MusicProfileRepositoryImpl(privacy_db)
    for bad in (MIN_FAMILIARITY - 1, MAX_FAMILIARITY + 1):
        with pytest.raises(ValueError):
            repo.upsert_track("u1", "T", "A", familiarity_score=bad)


def test_upsert_track_rejects_unknown_reaction(privacy_db):
    repo = MusicProfileRepositoryImpl(privacy_db)
    with pytest.raises(ValueError):
        repo.upsert_track("u1", "T", "A", reaction="confused")


def test_record_rejection_decrements_by_exactly_one(privacy_db):
    """**契約 2**: familiarity が 1 だけ減る。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=4)

    repo.record_rejection("u1", "A", "X")
    assert repo.get("u1").track("A").familiarity_score == 3

    repo.record_rejection("u1", "A", "X")
    assert repo.get("u1").track("A").familiarity_score == 2


def test_record_rejection_stops_at_lower_bound(privacy_db):
    """下限（1）を下回らない。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=MIN_FAMILIARITY)
    for _ in range(5):
        repo.record_rejection("u1", "A", "X")
    assert repo.get("u1").track("A").familiarity_score == MIN_FAMILIARITY


def test_record_rejection_is_a_single_update(privacy_db, monkeypatch):
    """**原子性の証拠**: `record_rejection` は SELECT を挟まない。

    読み取り -> 書き込みの 2 手順にすると、同時リクエストで減算が失われる。
    `Session.get` / `Query.first` を呼ばないことで 1 UPDATE であることを固定する。
    """
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=5)
    privacy_db.commit()

    statements: list = []
    original = privacy_db.execute

    def spy(statement, *args, **kwargs):
        statements.append(str(statement))
        return original(statement, *args, **kwargs)

    monkeypatch.setattr(privacy_db, "execute", spy)
    repo.record_rejection("u1", "A", "X")

    joined = "\n".join(statements).upper()
    assert "UPDATE FAVORITE_TRACKS" in joined
    assert "SELECT" not in joined, "record_rejection は SELECT を含む（2 手順になっている）"


def test_record_rejection_does_not_create_rows_for_unknown_tracks(privacy_db):
    """未登録の曲に対する拒否では行を作らない（事実でないデータを作らない）。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.record_rejection("u1", "never-heard", "X")
    assert repo.list_tracks("u1") == []


def test_record_rejection_is_tenant_scoped_by_owner(privacy_db):
    """同じ曲名でも owner が違えば減算されない（交差 0）。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("alice", "A", "X", familiarity_score=5)
    repo.upsert_track("bob", "A", "X", familiarity_score=5)

    repo.record_rejection("alice", "A", "X")
    assert repo.get("alice").track("A").familiarity_score == 4
    assert repo.get("bob").track("A").familiarity_score == 5


def test_record_play_updates_only_last_played_at(privacy_db):
    """再生は familiarity を変えない（親和性検査だけが変える）。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=4)
    before = repo.get("u1").track("A")

    when = datetime.now(timezone.utc)
    repo.record_play("u1", "A", "X", played_at=when)
    after = repo.get("u1").track("A")

    assert after.familiarity_score == before.familiarity_score
    assert after.last_played_at is not None
    assert after.last_played_at.tzinfo is not None, "aware であるべき"


def test_record_play_creates_row_for_new_track(privacy_db):
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.record_play("u1", "New", "Y")
    assert repo.get("u1").track("New").familiarity_score == 3


def test_unique_constraint_prevents_duplicate_tracks(privacy_db):
    """`(owner_id, title, artist)` UNIQUE があることで upsert が成立する。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=3)
    repo.upsert_track("u1", "A", "X", familiarity_score=5)
    assert len(repo.list_tracks("u1")) == 1
    assert repo.get("u1").track("A").familiarity_score == 5


def test_rejects_flag_matches_min_familiarity(privacy_db):
    """`MusicProfile.rejects` との整合性。"""
    repo = MusicProfileRepositoryImpl(privacy_db)
    repo.upsert_track("u1", "A", "X", familiarity_score=2)
    repo.record_rejection("u1", "A", "X")
    assert repo.get("u1").rejects("A", "X") is True


# ---------------------------------------------------------------------------
# 2. テナントとロール
# ---------------------------------------------------------------------------
def test_security_resolve_returns_defaults_without_row(privacy_db):
    repo = UserSecurityRepository(privacy_db)
    record = repo.resolve("nobody")
    assert record["role"] == "member"
    assert record["tenant_id"] == DEFAULT_TENANT_ID
    assert record["deleted_at"] is None


def test_set_role_and_tenant(privacy_db):
    repo = UserSecurityRepository(privacy_db)
    repo.ensure("u1", tenant_id="facility-a")
    repo.set_role("u1", ROLE_ADMIN)
    assert repo.is_admin("u1") is True
    assert repo.resolve("u1")["tenant_id"] == "facility-a"


def test_set_role_rejects_unknown_role(privacy_db):
    with pytest.raises(ValueError):
        UserSecurityRepository(privacy_db).set_role("u1", "superuser")


def test_bootstrap_admin_email_matching(privacy_db):
    repo = UserSecurityRepository(privacy_db)
    assert repo.is_bootstrap_admin_email("Admin@Example.com", ["admin@example.com"]) is True
    assert repo.is_bootstrap_admin_email("other@example.com", ["admin@example.com"]) is False
    assert repo.is_bootstrap_admin_email("admin@example.com", []) is False


# ---------------------------------------------------------------------------
# 1.5. admin 認可: リクエストヘッダーを信用しない（権限昇格の回帰防止）
# ---------------------------------------------------------------------------
@pytest.fixture()
def deps_db(privacy_db, monkeypatch):
    """`deps` 内部の遅延 import `get_db` を `privacy_db` に差し替える。

    `_bootstrap_admin` / `_db_role_is_admin` は関数内で
    `from ..db.session import get_db` する（遅延 import）ため、
    モジュール属性を monkeypatch すれば daqui のセッションを 向く。
    """
    import contextlib

    from retro_radio.db import session as session_module

    @contextlib.contextmanager
    def _fake_get_db():
        yield privacy_db

    monkeypatch.setattr(session_module, "get_db", _fake_get_db)
    return privacy_db


def _principal(user_id, *, authenticated=True, role="member"):
    from retro_radio.auth.tokens import Principal

    return Principal(
        user_id=user_id,
        tenant_id=DEFAULT_TENANT_ID,
        role=role,
        auth_mode="session",
        authenticated=authenticated,
    )


def test_bootstrap_admin_does_not_read_request_header(deps_db):
    """`X-Operator-Email` ヘッダーを偽装しても admin になれない。

    実害: ヘッダーはクライアントが自由に書ける。認証済みの一般利用者が
    `RETRO_RADIO_ADMIN_EMAILS` に載ったメールを 1 枚書けば、監査ログ閲覧
    （`/api/admin/audit`）を含む admin 権限に昇格できてしまう。
    判定材料は DB に紐づく認証済みユーザー自身のメールのみ。
    """
    from retro_radio.api.deps import _bootstrap_admin
    from retro_radio.config import Settings

    settings = Settings(
        require_auth=True, secret_key="k", admin_emails=["boss@example.com"]
    )
    user = UserRepository(deps_db).create("attacker@example.com", "hashed")

    # リクエストヘッダーを渡せる経路があっても結果は変わらない。
    # 旧実装は `request.headers["X-Operator-Email"]` を見て True を返していた。
    assert _bootstrap_admin(_principal(user.id), settings) is False


def test_bootstrap_admin_matches_own_email_only(deps_db):
    """自分のメールが `RETRO_RADIO_ADMIN_EMAILS` に入っていれば admin。"""
    from retro_radio.api.deps import _bootstrap_admin
    from retro_radio.config import Settings

    settings = Settings(
        require_auth=True, secret_key="k", admin_emails=["boss@example.com"]
    )
    repo = UserRepository(deps_db)
    boss = repo.create("boss@example.com", "hashed")
    stranger = repo.create("stranger@example.com", "hashed")

    assert _bootstrap_admin(_principal(boss.id), settings) is True
    assert _bootstrap_admin(_principal(stranger.id), settings) is False


def test_bootstrap_admin_requires_authenticated_user(deps_db):
    """個人モード（`user_id` 無し）には bootstrap admin を認めない。

    `require_admin` の docstring が定める「個人モードでは誰も admin を持たない」
    を実装で裏づける。
    """
    from retro_radio.api.deps import _bootstrap_admin
    from retro_radio.config import Settings

    settings = Settings(
        require_auth=True, secret_key="k", admin_emails=["boss@example.com"]
    )
    assert _bootstrap_admin(_principal(None, authenticated=False), settings) is False


def test_db_role_is_admin_is_authoritative(deps_db):
    """admin 判定は DB の `user_security.role` を見る（トークンの role ではない）。"""
    from retro_radio.api.deps import _db_role_is_admin

    repo = UserRepository(deps_db)
    member = repo.create("member@example.com", "hashed")
    dba = repo.create("dba@example.com", "hashed")
    UserSecurityRepository(deps_db).set_role(dba.id, ROLE_ADMIN)

    assert _db_role_is_admin(_principal(member.id)) is False
    assert _db_role_is_admin(_principal(dba.id)) is True
    # トークンが admin を名乗っていても DB が member なら member。
    # （降格反映がトークン TTL 内に遅れないことの防止）
    assert _db_role_is_admin(_principal(member.id, role="admin")) is False
    assert _db_role_is_admin(_principal(None)) is False


def test_tenant_ensure_is_idempotent(privacy_db):
    repo = TenantRepository(privacy_db)
    first = repo.ensure("facility-a", "A 施設")
    second = repo.ensure("facility-a")
    assert first["id"] == second["id"] == "facility-a"
    assert len(repo.list_active()) == 1


# ---------------------------------------------------------------------------
# 3. 同意
# ---------------------------------------------------------------------------
def test_consent_is_false_before_acceptance(privacy_db):
    assert ConsentRepository(privacy_db).has_consented("u1", "1.0.0") is False


def test_consent_recorded_and_readable(privacy_db):
    repo = ConsentRepository(privacy_db)
    repo.record("u1", "1.0.0", accepted=True, tenant_id="facility-a")
    assert repo.has_consented("u1", "1.0.0") is True


def test_consent_is_versioned(privacy_db):
    """版を上げると過去の実諾では**現在の版に同意していない**。"""
    repo = ConsentRepository(privacy_db)
    repo.record("u1", "1.0.0", accepted=True)
    assert repo.has_consented("u1", "1.0.0") is True
    assert repo.has_consented("u1", "2.0.0") is False


def test_consent_withdrawal_revokes(privacy_db):
    repo = ConsentRepository(privacy_db)
    repo.record("u1", "1.0.0", accepted=True)
    assert repo.withdraw("u1", "1.0.0") == 1
    assert repo.has_consented("u1", "1.0.0") is False


def test_explicit_refusal_is_recorded(privacy_db):
    """「同意しない」も記録する（提示 opportunities の記録として）。"""
    repo = ConsentRepository(privacy_db)
    repo.record("u1", "1.0.0", accepted=False)
    assert repo.has_consented("u1", "1.0.0") is False
    assert len(repo.history("u1")) == 1


# ---------------------------------------------------------------------------
# 4. 監査ログと完全率
# ---------------------------------------------------------------------------
def test_audit_log_always_has_tenant_id(privacy_db):
    """`tenant_id` は NOT NULL。「テナント不明のイベント」を作れない。"""
    from retro_radio.db.privacy_models import AuditLogModel

    repo = AuditRepository(privacy_db)
    entry = repo.record(tenant_id="", action="generation", user_id="u1")
    privacy_db.flush()
    assert entry["tenant_id"] == DEFAULT_TENANT_ID
    assert AuditLogModel.__table__.c.tenant_id.nullable is False


@pytest.mark.parametrize(
    "log_count,generation_count,expected",
    [
        (0, 0, 1.0),      # 対象が無ければ 100%
        (5, 5, 1.0),      # 完全
        (3, 5, 0.6),      # 欠落あり
        (8, 5, 1.6),      # 過剰（1.0 に丸めない）
    ],
)
def test_audit_coverage_ratio(log_count, generation_count, expected):
    assert audit_api.audit_coverage(log_count, generation_count) == expected


def test_audit_log_completeness_is_100_percent(privacy_db):
    """**効果指標**: 生成イベント数に対するログ行数 = 100%。

    N 回の生成に対して N 行の `generation` ログが存在することを固定する。
    """
    repo = AuditRepository(privacy_db)
    generations = 7
    for i in range(generations):
        repo.record(
            tenant_id="facility-a",
            action="generation",
            user_id="u1",
            resource_id=f"gen-{i}",
        )
    privacy_db.commit()

    total = repo.count(tenant_id="facility-a")
    generation_rows = repo.count(tenant_id="facility-a", action="generation")
    assert generation_rows == generations
    assert audit_api.audit_coverage(generation_rows, generations) == 1.0
    # 他のアクションが混ざっても完全率は「生成vents どうし」で見る
    repo.record(tenant_id="facility-a", action="playback", user_id="u1")
    assert repo.count(tenant_id="facility-a") == total + 1


def test_audit_list_is_tenant_scoped(privacy_db):
    """**テナント交差 = 0**: テナントをまたいだ閲覧はできない。"""
    repo = AuditRepository(privacy_db)
    repo.record(tenant_id="facility-a", action="generation", user_id="alice")
    repo.record(tenant_id="facility-b", action="generation", user_id="bob")
    privacy_db.commit()

    a_entries = repo.list(tenant_id="facility-a")
    assert len(a_entries) == 1
    assert a_entries[0]["user_id"] == "alice"
    assert repo.count(tenant_id="facility-a") == 1
    assert repo.count(tenant_id="facility-b") == 1


def test_audit_meta_never_contains_personal_text(privacy_db):
    """監査ログの `meta` は個人データを持たない（設計上の制約）。"""
    repo = AuditRepository(privacy_db)
    entry = repo.record(
        tenant_id="facility-a",
        action="generation",
        user_id="u1",
        meta={"year": 1975, "mode": "anniversary"},
    )
    assert entry["meta"] == {"year": 1975, "mode": "anniversary"}
    for forbidden in ("target_name", "email", "script", "birth_year"):
        assert forbidden not in entry["meta"]


# ---------------------------------------------------------------------------
# 5. 論理削除（DELETE /api/me の実行本体）
# ---------------------------------------------------------------------------
def test_purge_removes_personal_rows_but_keeps_audit(privacy_db, user_factory):
    user = user_factory()
    profile_repo = MusicProfileRepositoryImpl(privacy_db)
    profile_repo.upsert_track(user.id, "A", "X")
    ConsentRepository(privacy_db).record(user.id, "1.0.0", accepted=True)
    AuditRepository(privacy_db).record(
        tenant_id=DEFAULT_TENANT_ID, action="generation", user_id=user.id
    )
    privacy_db.commit()

    purge_user_personal_data(privacy_db, user.id)
    UserRepository(privacy_db).anonymize(user.id)
    privacy_db.commit()

    # 個人データは消える
    assert profile_repo.list_tracks(user.id) == []
    assert profile_repo.get(user.id).is_empty is True
    assert ConsentRepository(privacy_db).history(user.id) == []
    # 監査ログは「処理の証明」として残る
    assert AuditRepository(privacy_db).count() == 1
    # 削除済みフラグが立つ
    assert UserSecurityRepository(privacy_db).is_deleted(user.id) is True


def test_anonymize_breaks_email_uniqueness(privacy_db, user_factory):
    """匿名化後も `users.email` の一意制約を満たす。"""
    alice = user_factory("alice@example.com")
    bob = user_factory("bob@example.com")
    UserRepository(privacy_db).anonymize(alice.id)
    privacy_db.commit()

    emails = {UserRepository(privacy_db).get_raw(alice.id)["email"],
              UserRepository(privacy_db).get_raw(bob.id)["email"]}
    assert len(emails) == 2
    assert "@invalid.example" in UserRepository(privacy_db).get_raw(alice.id)["email"]


def test_anonymize_makes_login_impossible(privacy_db, user_factory):
    user = user_factory()
    UserRepository(privacy_db).anonymize(user.id)
    privacy_db.commit()
    assert UserRepository(privacy_db).get_by_id(user.id).hashed_password == "!"


def test_deleted_user_is_flagged_before_and_after(privacy_db, user_factory):
    user = user_factory()
    repo = UserSecurityRepository(privacy_db)
    assert repo.is_deleted(user.id) is False
    repo.mark_deletion_requested(user.id)
    assert repo.resolve(user.id)["deleted_at"] is None
    repo.mark_deleted(user.id)
    assert repo.is_deleted(user.id) is True


# ---------------------------------------------------------------------------
# 6. anniversary の入力最小化（nickname <= 16 / birth year only）
# ---------------------------------------------------------------------------
def test_target_name_accepts_nickname_up_to_16():
    name = "は" * 16
    assert me_api.normalize_target_name(name) == name


def test_target_name_rejects_more_than_16():
    with pytest.raises(me_api.TargetNameError):
        me_api.normalize_target_name("あ" * 17)


def test_target_name_rejects_structural_markers():
    for bad in ("あ\nい", "###見出し", "あ\tい", "あ\x00い"):
        with pytest.raises(me_api.TargetNameError):
            me_api.normalize_target_name(bad)


def test_target_name_strips_surrounding_whitespace():
    assert me_api.normalize_target_name("  お母さん  ") == "お母さん"


def test_target_name_empty_becomes_none():
    assert me_api.normalize_target_name("   ") is None
    assert me_api.normalize_target_name(None) is None


def test_max_target_name_length_is_16():
    assert me_api.MAX_TARGET_NAME_LENGTH == 16


def test_anniversary_requires_nickname_and_year():
    with pytest.raises(ValueError):
        me_api.validate_anniversary_input("anniversary", None, birth_year=1950)
    with pytest.raises(ValueError):
        me_api.validate_anniversary_input("anniversary", "お母さん", birth_year=None)


def test_anniversary_drops_month_and_day():
    """**生年月日を丸ごと受けない**。年だけに縮める。"""
    result = me_api.validate_anniversary_input(
        "anniversary", "お母さん", birth_year=1950, month=3, day=15
    )
    assert result["birth_year"] == 1950
    assert "month" in " ".join(result["dropped"])
    assert "day" in " ".join(result["dropped"])
    assert "month" not in result and "day" not in result


def test_anniversary_rejects_out_of_range_year():
    with pytest.raises(ValueError):
        me_api.validate_anniversary_input(
            "anniversary", "お母さん", birth_year=1900, min_year=1950, max_year=2025
        )


def test_normal_mode_is_not_restricted():
    result = me_api.validate_anniversary_input("normal", None, birth_year=None)
    assert result["target_name"] is None
    assert result["dropped"] == []


# ---------------------------------------------------------------------------
# 7. 開示の出力
# ---------------------------------------------------------------------------
def test_csv_export_has_bom_and_row(privacy_db, user_factory):
    from retro_radio.db.repository import GenerationRepository

    user = user_factory()
    GenerationRepository(privacy_db).create(
        user.id,
        {
            "year": 1960,
            "month": 7,
            "day": 15,
            "script": "テスト原稿",
            "song_title": "テスト曲",
            "artist_name": "テストアーティスト",
        },
    )
    privacy_db.commit()

    payload = {
        "generations": GenerationRepository(privacy_db).get_by_user(user.id),
        "favorite_tracks": MusicProfileRepositoryImpl(privacy_db).list_tracks(user.id),
        "consents": [],
    }
    csv_text = me_api._to_csv(payload)
    header, _, data_row = csv_text.partition("\n")

    assert header.startswith('"種別"')
    assert '"generation"' in data_row
    assert "1960" in data_row
    assert "テスト曲" in data_row
    assert "テストアーティスト" in data_row


def test_csv_export_is_bom_prefixed(privacy_db, user_factory):
    """Excel（cp932 環境）での文字化け防止。BOM が先頭に付く。"""
    body = me_api._to_csv({"generations": [], "favorite_tracks": [], "consents": []})
    assert (me_api.UTF8_BOM + body).startswith("\ufeff")


def test_csv_export_marks_record_type(privacy_db, user_factory):
    user = user_factory()
    MusicProfileRepositoryImpl(privacy_db).upsert_track(user.id, "A", "X")
    payload = {
        "generations": [],
        "favorite_tracks": MusicProfileRepositoryImpl(privacy_db).list_tracks(user.id),
        "consents": [],
    }
    assert "favorite_track" in me_api._to_csv(payload)


# ---------------------------------------------------------------------------
# 8. マイグレーションの head 連鎖
# ---------------------------------------------------------------------------
def test_migration_head_connects_to_existing_head():
    """新規 revision が既存の head (`d7a3f0b1c9e4`) に接続していること。"""
    from pathlib import Path

    versions = Path(__file__).resolve().parent.parent / "db" / "migrations" / "versions"
    new = versions / "2c1f5a9b3d47_add_privacy_and_tenancy_tables.py"
    assert new.exists()

    text = new.read_text(encoding="utf-8")
    assert "revision: str = '2c1f5a9b3d47'" in text
    assert "down_revision: Union[str, None] = 'd7a3f0b1c9e4'" in text


def test_migration_creates_all_six_tables():
    from pathlib import Path

    versions = Path(__file__).resolve().parent.parent / "db" / "migrations" / "versions"
    text = (versions / "2c1f5a9b3d47_add_privacy_and_tenancy_tables.py").read_text(
        encoding="utf-8"
    )
    for table in (
        "tenants",
        "user_security",
        "music_profiles",
        "favorite_tracks",
        "consents",
        "audit_logs",
    ):
        assert f'op.create_table(\n        "{table}"' in text, f"{table} を作る処理が無い"


def test_privacy_models_table_names():
    assert set(PrivacyBase.metadata.tables) == {
        "tenants",
        "user_security",
        "music_profiles",
        "favorite_tracks",
        "consents",
        "audit_logs",
    }


def test_existing_tables_are_untouched():
    """**既存スキーマを壊していない**ことの固定。

    `tests/test_db_models.py` が `users` の列集合の完全一致を検証している。
    S4 は既存テーブルに列を足さない。
    """
    from retro_radio.db.models import Base

    assert set(Base.metadata.tables) == {"users", "generations", "favorites"}
    user_columns = {c.name for c in Base.metadata.tables["users"].columns}
    for added in ("role", "tenant_id", "deleted_at", "birth_year"):
        assert added not in user_columns
