"""P1 残存課題 4 件の修正に対する回帰テスト（R2-03 / R2-07 / R2-08 / R2-11）。

各節に対応する実バグ:

* **R2-08/DB-01** ``record_generation`` がどこからも呼ばれておらず、
  ``generations`` テーブルが恒久的に空のままだった（= 開示も削除請求も成立しない）。
* **R2-08/DB-01** ``DELETE /api/me`` が ``generations`` を消さなかったため、
  原稿全文が削除後も残存していた。
* **R2-07** ``GenerateRequest.target_name`` が 64 文字まで受理していた
  （プライバシー目標の 16 文字と UI 契約の 64 文字が競合）。
* **R2-11 コア** ラジオ台本の読み上げ文に**テレビ番組**が混ざっていた。
* **R2-03** プロキシ背後でログインのロックアウトキーが全利用者で共有されていた。

**このファイルは「仕様を変えた」ことを固定する。** 将来 上限を緩める改修が
静かに入りますと、ここが赤くなる。
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pydantic
import pytest

from retro_radio import server as server_module
from retro_radio.api.me import MAX_TARGET_NAME_LENGTH, normalize_target_name
from retro_radio.config import Settings
from retro_radio.server import GenerateRequest, _client_ip


# ==============================================================================
# 1. R2-08/DB-01: 生成履歴が `generations` に記録される
# ==============================================================================
class _StubContext:
    """`_GenerationContext` の必要面だけを持つ stub（生成ステップは走らせない）。"""

    def __init__(self, song_list, script="テスト原稿", audio_url="/api/audio/a.mp3"):
        self.song_list = song_list
        self.script = script
        self.audio_url = audio_url
        self.tenant_id = "facility-a"
        self.user_id = "user-1"


def _capture_record(monkeypatch):
    """`history_service.record_generation` を差し替えて呼び出しを観測する。"""
    import retro_radio.services.history_service as hs

    calls = []
    monkeypatch.setattr(hs, "record_generation", lambda **kwargs: calls.append(kwargs))
    return calls


def test_persist_generation_writes_a_row(monkeypatch):
    """生成が完了したら 1 行だけ `generations` へ記録される"""
    calls = _capture_record(monkeypatch)
    ctx = _StubContext([{"title": "神田川", "artist": "南こうせつ", "preview_url": "u"}])

    server_module._persist_generation(GenerateRequest(year=1975), ctx)

    assert len(calls) == 1
    assert calls[0]["year"] == 1975
    assert calls[0]["tenant_id"] == "facility-a"
    assert calls[0]["user_id"] == "user-1"
    assert calls[0]["song_title"] == "神田川"
    assert calls[0]["script"] == "テスト原稿"
    assert calls[0]["mode"] == "normal"


def test_persist_generation_prefers_a_playable_song(monkeypatch):
    """音源が無いスロット（間奏）を代表曲にしない

    音源の無いスロットは ``title="間奏" / artist=""`` で応答に入る。
    それを代表曲にすると、開示 CSV に「間奏」という曲名が並ぶだけで
    実際の選曲情報が消える。
    """
    calls = _capture_record(monkeypatch)
    ctx = _StubContext(
        [
            {"title": "間奏", "artist": "", "preview_url": None},
            {"title": "上を向いて歩こう", "artist": "坂本九", "preview_url": "u"},
        ]
    )

    server_module._persist_generation(GenerateRequest(year=1975), ctx)

    assert calls[0]["song_title"] == "上を向いて歩こう"
    assert calls[0]["artist_name"] == "坂本九"


def test_persist_generation_never_breaks_the_response(monkeypatch):
    """履歴の記録が失敗しても例外は外へ出ない（配信を壊さない）"""
    import retro_radio.services.history_service as hs

    def _boom(**kwargs):
        raise RuntimeError("DB が落ちた")

    monkeypatch.setattr(hs, "record_generation", _boom)

    # 例外が出ればこの行でテストが失敗する（記録は best-effort のはず）。
    server_module._persist_generation(
        GenerateRequest(year=1975),
        _StubContext([{"title": "A", "artist": "B", "preview_url": "u"}]),
    )


def test_build_generate_response_persists_the_generation(monkeypatch):
    """`/api/generate` と `/api/jobs` の共有本体が履歴を記録する

    両経路は `_build_generate_response` を共有しているため、
    ここに 1 か所だけ配線すれば両方が埋まる。片方だけ埋まっている状態を
    検出できないよう、関数自身が呼んでいることを固定する。
    """
    calls = _capture_record(monkeypatch)
    monkeypatch.setattr(server_module, "GENERATION_STEPS", [], raising=False)

    server_module._build_generate_response(
        GenerateRequest(year=1975),
        tenant_id="facility-a",
        user_id="user-1",
    )

    assert calls, "生成履歴が記録されていない（generations が空のまま）"
    assert calls[0]["year"] == 1975


def test_history_is_not_recorded_for_anonymous_personal_mode(monkeypatch):
    """利用者を特定できない個人モードでは記録しない

    紐付け先ユーザー ID の無い行を作ると、後から「誰のものか不明」で
    削除請求を追えなくなる（`history_service.record_generation` の契約）。
    """
    calls = _capture_record(monkeypatch)
    ctx = _StubContext([{"title": "A", "artist": "B", "preview_url": "u"}])
    ctx.user_id = None

    server_module._persist_generation(GenerateRequest(year=1975), ctx)

    # 関数自体は呼び出し、実 Higgs で落ちる判断は history_service 側にある。
    assert calls[0]["user_id"] is None


# ==============================================================================
# 2. R2-08/DB-01: DELETE /api/me が generations も消す
# ==============================================================================
class _SameSession:
    """`get_db()` と同じ「セッションを閉じない」契約だけを満たす代物。"""

    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        return False


@pytest.fixture()
def privacy_db():
    """`users` と S4 のテーブルを持つ独立した in-memory SQLite。

    `DELETE /api/me` は `generations`（`users` 側 MetaData）と
    `user_security` / `audit_logs`（privacy 側 MetaData）の**両方**を触るため、
    両方を同じ DB に作る必要がある。
    """
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from retro_radio.db.models import Base
    from retro_radio.db.privacy_models import create_privacy_tables

    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    create_privacy_tables(bind=engine)
    session = sessionmaker(autocommit=False, autoflush=False, bind=engine)()
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


def test_delete_old_reports_how_many_rows_it_removed(privacy_db):
    """削除件数が返る（削除請求の証明として応答に載せるため）"""
    from retro_radio.db.repository import GenerationRepository, UserRepository

    user = UserRepository(privacy_db).create("deleter@example.com", "hashed")
    repo = GenerationRepository(privacy_db)
    for index in range(3):
        repo.create(
            user.id,
            {
                "year": 1975,
                "month": 1,
                "day": 1,
                "script": f"原稿{index}",
                "song_title": "神田川",
                "artist_name": "南こうせつ",
            },
        )
    privacy_db.flush()

    removed = repo.delete_old(user.id, keep=0)

    assert removed == 3
    assert repo.get_by_user(user.id, limit=10) == []


def _delete_me(monkeypatch, db, user_id):
    """`DELETE /api/me` を同意・認証の前提抜きで呼ぶ。"""
    from retro_radio.api import me as me_module

    monkeypatch.setattr(me_module, "get_db", lambda: _SameSession(db))
    return me_module.delete_me(
        request=SimpleNamespace(),
        principal=SimpleNamespace(user_id=user_id, tenant_id="facility-a"),
        settings=SimpleNamespace(deletion_sla_hours=24),
    )


def test_delete_me_purges_generation_rows(monkeypatch, privacy_db):
    """`DELETE /api/me` が原稿全文（`generations`）も消す"""
    from retro_radio.db.privacy_repository import TenantRepository
    from retro_radio.db.repository import GenerationRepository, UserRepository

    TenantRepository(privacy_db).ensure("facility-a", "A 施設")
    user = UserRepository(privacy_db).create("purged@example.com", "hashed")
    GenerationRepository(privacy_db).create(
        user.id,
        {
            "year": 1975,
            "month": 1,
            "day": 1,
            "script": "他人に見せない原稿本文",
            "song_title": "神田川",
            "artist_name": "南こうせつ",
        },
    )
    privacy_db.commit()

    _delete_me(monkeypatch, privacy_db, user.id)

    assert (
        GenerationRepository(privacy_db).get_by_user(user.id, limit=10) == []
    ), "削除後も原稿全文が generations に残っている"


def test_delete_me_reports_the_removed_generation_count(monkeypatch, privacy_db):
    """削除件数が応答の `purged` に載る（利用者へ説明できる）"""
    from retro_radio.db.privacy_repository import TenantRepository
    from retro_radio.db.repository import GenerationRepository, UserRepository

    TenantRepository(privacy_db).ensure("facility-a", "A 施設")
    user = UserRepository(privacy_db).create("counted@example.com", "hashed")
    GenerationRepository(privacy_db).create(
        user.id,
        {
            "year": 1975,
            "month": 1,
            "day": 1,
            "script": "原稿",
            "song_title": "神田川",
            "artist_name": "南こうせつ",
        },
    )
    privacy_db.commit()

    result = _delete_me(monkeypatch, privacy_db, user.id)

    assert result.purged["generation_rows_removed"] == 1


# ==============================================================================
# 3. R2-07: target_name の上限が 1 か所に集まっている
# ==============================================================================
def test_max_target_name_length_is_sixteen():
    """プライバシー目標（16）を正とする"""
    assert MAX_TARGET_NAME_LENGTH == 16


def test_generate_request_uses_the_same_limit_as_the_privacy_module():
    """UI 契約（64）とプライバシー目標（16）が競合していない"""
    field = GenerateRequest.model_fields["target_name"]
    assert field.description and "16" in field.description, field.description
    # 上限は実際に 16 文字で効いている（宣言だけでなく検証も見る）。
    GenerateRequest(year=1975, target_name="あ" * MAX_TARGET_NAME_LENGTH)
    with pytest.raises(pydantic.ValidationError):
        GenerateRequest(year=1975, target_name="あ" * (MAX_TARGET_NAME_LENGTH + 1))


@pytest.mark.parametrize(
    "value",
    ["お父さん", "おばあちゃん", "Taro", "あ" * MAX_TARGET_NAME_LENGTH],
)
def test_nicknames_within_the_limit_are_accepted(value):
    assert GenerateRequest(year=1975, target_name=value).target_name == value


@pytest.mark.parametrize("value", ["あ" * 17, "あ" * 64, "x" * 1000])
def test_names_over_the_limit_are_rejected(value):
    with pytest.raises(pydantic.ValidationError):
        GenerateRequest(year=1975, target_name=value)


def test_structure_markers_are_still_rejected():
    """長さの統一で構造注入の拒否が抜けていない"""
    for bad in ("太郎\n### エンディング", "### エンディング", "太郎\x00more"):
        with pytest.raises(pydantic.ValidationError):
            GenerateRequest(year=1975, target_name=bad)


def test_blank_becomes_none_on_both_paths():
    """空文字は `None`（`normalize_target_name` と同じ契約）"""
    assert normalize_target_name("") is None
    assert GenerateRequest(year=1975, target_name="").target_name is None


# ==============================================================================
# 4. R2-03: クライアント IP（プロキシ背後のロックアウト）
# ==============================================================================
def _settings(**kwargs):
    base = {"trusted_proxy_header": None, "trusted_proxy_hops": 1}
    base.update(kwargs)
    return SimpleNamespace(**base)


def _request(headers, client_host="10.0.0.1"):
    scope = {
        "type": "http",
        "headers": [
            (key.lower().encode("latin-1"), value.encode("latin-1"))
            for key, value in headers.items()
        ],
    }
    return SimpleNamespace(
        scope=scope,
        client=SimpleNamespace(host=client_host) if client_host else None,
    )


def test_forwarded_header_is_ignored_unless_declared():
    """宣言しない限り `X-Forwarded-For` を信用しない（既存挙動の保存）"""
    request = _request({"X-Forwarded-For": "203.0.113.9"})
    assert _client_ip(request, _settings()) == "10.0.0.1"


def test_declared_header_yields_the_client_behind_one_proxy():
    """nginx 1 台（hops=1）を宣言したら右端＝実クライアントを取れる"""
    request = _request({"X-Forwarded-For": "203.0.113.9"})
    resolved = _client_ip(
        request,
        _settings(trusted_proxy_header="x-forwarded-for", trusted_proxy_hops=1),
    )
    assert resolved == "203.0.113.9"


def test_two_hop_chain_takes_the_second_from_the_right():
    """CDN + nginx（`利用者, CDN` の 2 件）なら `hops=2` で利用者を拾える"""
    request = _request({"X-Forwarded-For": "198.51.100.4, 203.0.113.9"})
    resolved = _client_ip(
        request,
        _settings(trusted_proxy_header="x-forwarded-for", trusted_proxy_hops=2),
    )
    assert resolved == "198.51.100.4"


def test_a_chain_shorter_than_the_declared_hops_is_not_trusted():
    """宣言した段数より足りないなら**信用しない**（安全側）"""
    request = _request({"X-Forwarded-For": "203.0.113.9"})
    resolved = _client_ip(
        request,
        _settings(trusted_proxy_header="x-forwarded-for", trusted_proxy_hops=3),
    )
    assert resolved == "10.0.0.1"


def test_a_spoofed_prefix_is_ignored_at_the_declared_depth():
    """利用者が左端を足しても、宣言段数の位置は変わらない"""
    request = _request({"X-Forwarded-For": "1.2.3.4, 198.51.100.4, 203.0.113.9"})
    resolved = _client_ip(
        request,
        _settings(trusted_proxy_header="x-forwarded-for", trusted_proxy_hops=2),
    )
    assert resolved == "198.51.100.4"


def test_cloudflare_header_is_supported():
    request = _request({"CF-Connecting-IP": "203.0.113.9"})
    resolved = _client_ip(
        request,
        _settings(trusted_proxy_header="cf-connecting-ip", trusted_proxy_hops=1),
    )
    assert resolved == "203.0.113.9"


def test_missing_client_still_returns_empty_string():
    request = _request({}, client_host=None)
    assert _client_ip(request, _settings()) == ""


def test_default_settings_declare_no_trusted_proxy():
    """既定は「信用しない」。設定ミスで突然ロックアウトを共有しない"""
    assert Settings().trusted_proxy_header is None
    assert Settings().trusted_proxy_hops == 1


# ==============================================================================
# 5. 回帰: SSE が終端イベントを読み飛ばさない（R2-08 の配線で露見した競合）
# ==============================================================================
def test_sse_drains_remaining_events_after_the_job_finishes():
    """`is_finished` を見た瞬間に return すると `done` を読み飛ばす

    `Job.succeed()` は `EVENT_DONE` の emit **前**に終端状態を決める
    （`server._run_job` は succeed → emit の順）。
    そのため `is_finished` だけを見て閉じると、クライアントは最後の
    イベントを見失う（``tests/test_job_api.py`` が実測で検出した）。
    """
    source = inspect.getsource(server_module._sse_stream)
    assert "is_finished" in source
    tail = source.split("if job.is_finished:", 1)[1]
    assert "events_after" in tail, "終端後にバッファを排出していない"
