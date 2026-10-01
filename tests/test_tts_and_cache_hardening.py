"""TTS / キャッシュの堅牢化リグレッション防止テスト。

固定している実測済みの不具合:

- `core.tts` の `TTS_CACHE_DIR` に上限が無く `%TEMP%` が際限なく膨らむ。
- `services.cache_service.clear_all_cache()` が `get_audio_cache()`（コピー）の
  `.clear()` を呼ぶだけで**実キャッシュを消せていない**。
- `services.session` のセッション内オーディオキャッシュが無制限。
- `services.export_service` の CSV だけが脚本を 100 文字で黙って切り詰める
  （JSON 版は全文）。開示対象の記録として不整合。
- `core.tts` の `NamedTemporaryFile(delete=False)` が部分失敗で残る。
- `core.legacy_tts.save()` が保存先を検証せずパスolino を許す。
"""

import time
from pathlib import Path

import pytest

from retro_radio.core import legacy_tts, tts
from retro_radio.services import cache_service
from retro_radio.utils import session as session_module


# ==============================================================================
# 1. TTS ファイルキャッシュの上限
# ==============================================================================
@pytest.fixture
def isolated_tts_cache(tmp_path, monkeypatch):
    """`tts.TTS_CACHE_DIR` をテスト専用ディレクトリに差し替える。"""
    cache_dir = tmp_path / "tts_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(tts, "TTS_CACHE_DIR", cache_dir)
    tts.reset_tts_cache_sweep_counter()
    yield cache_dir
    tts.reset_tts_cache_sweep_counter()


def _write_entries(cache_dir: Path, count: int, age_seconds: float = 0.0) -> None:
    now = time.time()
    for index in range(count):
        path = cache_dir / f"{index:04d}.mp3"
        path.write_bytes(b"ID3-audio")
        stamp = now - age_seconds
        import os

        os.utime(path, (stamp, stamp))


def test_sweep_is_skipped_between_intervals(isolated_tts_cache, monkeypatch):
    """既定では N 回に 1 回しか掃除しない（`TenantTtsCache` と同じ間引き）。"""
    monkeypatch.setattr(tts, "TTS_CACHE_SWEEP_INTERVAL", 3)
    _write_entries(isolated_tts_cache, 2)

    assert tts.sweep_tts_cache() == 0
    assert tts.sweep_tts_cache() == 0
    assert tts.sweep_tts_cache() == 0  # 3 回目で実行される


def test_sweep_enforces_max_entries_lru(isolated_tts_cache, monkeypatch):
    """上限を超えたら mtime が古い側（LRU）から削除する。"""
    monkeypatch.setattr(tts, "TTS_CACHE_MAX_ENTRIES", 5)
    # 古い 8 件 / 新しい 2 件
    _write_entries(isolated_tts_cache, 8, age_seconds=10_000)
    _write_entries(isolated_tts_cache, 0)
    for index in range(8, 10):
        path = isolated_tts_cache / f"{index:04d}.mp3"
        path.write_bytes(b"ID3-audio")

    removed = tts.sweep_tts_cache(force=True)

    remaining = sorted(p.name for p in isolated_tts_cache.glob("*.mp3"))
    assert removed == 5
    assert len(remaining) == tts.TTS_CACHE_MAX_ENTRIES
    # 新しい 2 件は必ず残る
    assert remaining[-2:] == ["0008.mp3", "0009.mp3"]


def test_sweep_deletes_ttl_expired_entries(isolated_tts_cache, monkeypatch):
    monkeypatch.setattr(tts, "TTS_CACHE_TTL_DAYS", 1)
    _write_entries(isolated_tts_cache, 2, age_seconds=3 * 86400)

    assert tts.sweep_tts_cache(force=True) == 2
    assert list(isolated_tts_cache.glob("*.mp3")) == []


def test_sweep_actually_runs_on_cache_write(isolated_tts_cache, monkeypatch):
    """保存経路から `sweep_tts_cache()` が実際に呼ばれる（呼ばれているかの回帰防止）。"""
    calls = []
    monkeypatch.setattr(tts, "sweep_tts_cache", lambda *a, **k: calls.append(1))
    calls.append(1)  # 差し替え確認

    class _FakeGTTS:
        def __init__(self, **kwargs):
            pass

        def save(self, path):
            Path(path).write_bytes(b"ID3-audio")

    monkeypatch.setattr(tts, "gTTS", _FakeGTTS)
    out = tts.gtts_tts("ハードニング確認用の原稿")

    assert out and Path(out).is_file()
    assert len(calls) == 2, "保存時に sweep が呼ばれていない"
    tts.cleanup_audio_file(out)


# ==============================================================================
# 2. clear_all_cache() が実キャッシュを消す
# ==============================================================================
def test_clear_all_cache_clears_live_audio_cache():
    """`get_audio_cache()` はコピーなので、`.clear()` だけでは消えない。"""
    session_module.clear_audio_cache()
    session_module.set_audio_cache("a", b"1")
    session_module.set_audio_cache("b", b"2")
    assert session_module.get_audio_cache() == {"a": b"1", "b": b"2"}

    cache_service.clear_all_cache()

    assert session_module.get_audio_cache() == {}


def test_session_audio_cache_is_bounded():
    """セッション内オーディオキャッシュは上限を超えると古い順に捨てる。"""
    session_module.clear_audio_cache()
    limit = session_module.MAX_AUDIO_CACHE_ENTRIES
    for index in range(limit + 5):
        session_module.set_audio_cache(f"k{index}", str(index).encode())

    cache = session_module.get_audio_cache()
    assert len(cache) == limit
    # 最初に入れたものは消え、最新ものは残っている
    assert "k0" not in cache
    assert f"k{limit + 4}" in cache
    session_module.clear_audio_cache()


def test_get_audio_cache_returns_a_copy():
    cache_service.clear_all_cache()
    session_module.set_audio_cache("k", b"v")
    copy = session_module.get_audio_cache()
    copy.clear()
    assert session_module.get_audio_cache() == {"k": b"v"}
    session_module.clear_audio_cache()


# ==============================================================================
# 3. CSV / JSON エクスポートの一致
# ==============================================================================
class _FakeExportService:
    """`ExportService` のエクスポート経路だけを切り出した最小偽物。

    DB を持たず、`gen_repo.get_by_user` だけを差し替えて CSV / JSON の一致を見る。
    """

    def __init__(self, generations):
        from retro_radio.services.export_service import ExportService

        self._service = ExportService.__new__(ExportService)
        self._service._cache = {}
        self._service._cache_timeout = 300
        self._service.tenant_id = "default"
        self._service.user_is_deleted = lambda _uid: False
        self._service.gen_repo = type(
            "_Repo", (), {"get_by_user": lambda _self, _u, limit: generations}
        )()

    def export_generations_csv(self, user_id, limit=1000):
        from retro_radio.services.export_service import ExportService

        return ExportService.export_generations_csv(self._service, user_id, limit=limit)

    def export_generations_json(self, user_id, limit=1000):
        from retro_radio.services.export_service import ExportService

        return ExportService.export_generations_json(self._service, user_id, limit=limit)


def _generation(script: str) -> dict:
    return {
        "created_at": "2024-01-02 03:04:05",
        "year": 1971,
        "month": 3,
        "day": 3,
        "script": script,
        "song_title": "-title",
        "artist_name": "artist",
        "preview_url": None,
    }


def test_csv_export_does_not_truncate_the_script():
    long_script = "あ" * 500
    service = _FakeExportService([_generation(long_script)])

    csv_text = service.export_generations_csv("u1")

    assert long_script in csv_text, "CSV が脚本を切り詰めている（JSON と不整合）"
    assert "..." not in csv_text


def test_csv_and_json_exports_contain_the_same_script():
    script = " célula 内容 " + "x" * 400
    service = _FakeExportService([_generation(script)])

    csv_text = service.export_generations_csv("u1")
    payload = service.export_generations_json("u1")

    assert payload["generations"][0]["script"] == script
    assert script in csv_text


# ==============================================================================
# 4. 一時ファイルの後始末
# ==============================================================================
def test_temp_file_is_removed_when_gtts_save_fails(tmp_path, monkeypatch):
    """部分失敗しても `NamedTemporaryFile(delete=False)` の残骸を残さない。"""
    before = set(Path(tmp_path).glob("*.mp3"))

    class _Boom:
        def __init__(self, **kwargs):
            pass

        def save(self, path):
            # 途中まで書いてから失敗する（残骸が残りやすいケース）
            Path(path).write_bytes(b"ID3-partial")
            raise RuntimeError("synthesis exploded")

    monkeypatch.setattr(tts, "gTTS", _Boom)
    monkeypatch.setattr(tts, "TTS_CACHE_DIR", tmp_path / "no_cache")

    result = tts.gtts_tts("後始末の確認")

    assert result is None
    assert set(Path(tmp_path).glob("*.mp3")) == before


def test_elevenlabs_temp_file_is_removed_on_write_failure(tmp_path, monkeypatch):
    """書き込み失敗で `ElevenLabs` 側の一時ファイルが残らないこと。"""
    tmp_file = tmp_path / "leaked.mp3"
    tmp_file.write_bytes(b"partial")

    class _Resp:
        status_code = 200
        content = b"audio"

        def raise_for_status(self):
            return None

    class _BadFile:
        def __init__(self, delete=False, suffix=""):
            self.name = str(tmp_file)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def write(self, data):
            raise OSError("disk full")

    monkeypatch.setattr(tts.tempfile, "NamedTemporaryFile", _BadFile)
    monkeypatch.setattr(tts.requests, "post", lambda *a, **k: _Resp())
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "test-key", raising=False)
    # フォールバック先（gTTS）はネットワークに出ないように止める
    monkeypatch.setattr(tts, "gtts_tts", lambda *a, **k: None)

    tts.elevenlabs_tts("テスト")

    assert not tmp_file.exists(), "部分書き込みの一時ファイルが残っている"


# ==============================================================================
# 5. legacy_tts.save() のパストラバーサル防御
# ==============================================================================
def _stub_legacy_response(monkeypatch, content=b"\xff\xf3rest"):
    class _Resp:
        status_code = 200

        def __init__(self):
            self.content = content

    monkeypatch.setattr(
        legacy_tts.requests, "get", lambda *a, **k: _Resp()
    )


def test_save_allows_path_inside_allowed_root(tmp_path, monkeypatch):
    monkeypatch.setattr(legacy_tts, "ALLOWED_SAVE_ROOT", tmp_path)
    _stub_legacy_response(monkeypatch)

    target = tmp_path / "out.mp3"
    assert legacy_tts.save("テスト", str(target)) == str(target)
    assert target.read_bytes() == b"\xff\xf3rest"


@pytest.mark.parametrize(
    "name",
    ["../escape.mp3", "..\\escape.mp3", "sub/../../escape.mp3"],
)
def test_save_rejects_traversal(tmp_path, monkeypatch, name):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(legacy_tts, "ALLOWED_SAVE_ROOT", allowed)

    with pytest.raises(legacy_tts.UnsafeSavePathError):
        legacy_tts.save("テスト", str(allowed / name))


def test_save_rejects_absolute_path_outside_root(tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    monkeypatch.setattr(legacy_tts, "ALLOWED_SAVE_ROOT", allowed)
    outside = tmp_path / "outside.mp3"

    with pytest.raises(legacy_tts.UnsafeSavePathError):
        legacy_tts.save("テスト", str(outside))


def test_save_rejects_empty_path(monkeypatch):
    with pytest.raises(legacy_tts.UnsafeSavePathError):
        legacy_tts.save("テスト", "   ")
