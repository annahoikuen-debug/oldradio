"""TTS / キャッシュの堅牢化リグレッション防止テスト。

固定している実測済みの不具合:

- セッション内オーディオキャッシュが無制限（上限を超えると古い順に捨てる）。
- `core.legacy_tts.save()` が保存先を検証せずパストラバーサルを許す。

削除済み（死蔵コード — コードレビュー D-7）:
`core.tts`（TTS キャッシュ掃除・一時ファイル後始末）、
`services.cache_service`（clear_all_cache）、
`services.export_service`（CSV の脚本切り詰め）の各テストは、
対応する実装とともに削除した。
"""

import pytest

from retro_radio.core import legacy_tts
from retro_radio.utils import session as session_module


# ==============================================================================
# 1. セッション内オーディオキャッシュの上限とコピー semantics
# ==============================================================================
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
    """`get_audio_cache()` はコピーを返す（呼び出し側の clear が実キャッシュを消さない）。"""
    session_module.clear_audio_cache()
    session_module.set_audio_cache("k", b"v")
    copy = session_module.get_audio_cache()
    copy.clear()
    assert session_module.get_audio_cache() == {"k": b"v"}
    session_module.clear_audio_cache()


# ==============================================================================
# 2. legacy_tts.save() のパストラバーサル防御
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
