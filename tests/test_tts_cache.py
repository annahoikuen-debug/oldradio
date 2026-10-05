"""TTS キャッシュの検証。

Wave 1 で:
  - キャッシュキーが `sha256(f"{text}_{lang}_{tld}_{slow}")` に変更された
    （同じテキストでも tld（言語）が違えば別ファイルになる）
  - `mkstemp` + `os.replace` による atomic install になった
  - import 時の `mkdir` が廃止され、lifespan 起動 or 初回の TTS 生成時に作られる
  - TTL sweep が追加された
"""

import hashlib
import os
import time

import pytest

import retro_radio.server as server_module
from retro_radio.server import _tts_cache_filename, generate_tts_cached


@pytest.fixture
def tts(mock_gtts, monkeypatch):
    """キャッシュディレクトリをテスト専用の一時ディレクトリに差し替える"""
    import tempfile
    from pathlib import Path

    tmp_dir = Path(tempfile.mkdtemp(prefix="retro_radio_tts_test_"))
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_dir)
    mock_gtts.calls = []
    return mock_gtts


def _unique(prefix: str = "テキスト") -> str:
    return f"{prefix}-{os.urandom(8).hex()}"


# --- キャッシュキー -------------------------------------------------------------
def test_cache_filename_shape():
    filename = _tts_cache_filename("こんにちは")
    assert filename.startswith("tts_")
    assert filename.endswith(".mp3")
    hash_part = filename[4:-4]
    assert len(hash_part) == 64
    int(hash_part, 16)


def test_cache_key_is_stable_for_same_input():
    assert _tts_cache_filename("同じ") == _tts_cache_filename("同じ")


def test_cache_key_differs_for_different_text():
    assert _tts_cache_filename("A") != _tts_cache_filename("B")


def test_cache_key_includes_tts_language_and_tld(monkeypatch):
    """tld（言語）を変えるとキャッシュキーが変わる（別言語の音声を返さない）"""
    monkeypatch.setattr(server_module.settings, "tts_language", "ja", raising=False)
    monkeypatch.setattr(server_module.settings, "tts_tld", "co.jp", raising=False)
    monkeypatch.setattr(server_module.settings, "tts_slow", False, raising=False)
    ja = _tts_cache_filename("こんにちは")

    monkeypatch.setattr(server_module.settings, "tts_tld", "com", raising=False)
    com = _tts_cache_filename("こんにちは")

    monkeypatch.setattr(server_module.settings, "tts_language", "en", raising=False)
    en = _tts_cache_filename("こんにちは")

    assert len({ja, com, en}) == 3, "言語設定がキャッシュキーに含まれていない"


def test_cache_key_includes_slow_flag(monkeypatch):
    monkeypatch.setattr(server_module.settings, "tts_slow", False, raising=False)
    normal = _tts_cache_filename("text")
    monkeypatch.setattr(server_module.settings, "tts_slow", True, raising=False)
    assert normal != _tts_cache_filename("text")


def test_cache_key_matches_documented_formula(monkeypatch):
    """ドキュメントどおりの sha256(f"{text}_{engine}_{lang}_{tld}_{slow}") で hash している"""
    monkeypatch.setattr(server_module.settings, "tts_language", "ja", raising=False)
    monkeypatch.setattr(server_module.settings, "tts_tld", "co.jp", raising=False)
    monkeypatch.setattr(server_module.settings, "tts_slow", False, raising=False)

    expected = hashlib.sha256("こんにちは_gtts_ja_co.jp_False".encode("utf-8")).hexdigest()
    assert _tts_cache_filename("こんにちは") == f"tts_{expected}.mp3"


def test_cache_key_includes_edge_voice_and_rate(monkeypatch):
    """edge エンジンでは voice / rate / pitch / volume をキャッシュキーに含める。

    含めないと「音声設定だけ変えても古いファイルが返る」事故になる。
    """
    monkeypatch.setattr(server_module.settings, "tts_engine", "edge", raising=False)
    monkeypatch.setattr(server_module.settings, "tts_edge_voice", "ja-JP-NanamiNeural", raising=False)
    monkeypatch.setattr(server_module.settings, "tts_edge_rate", "+0%", raising=False)
    base = _tts_cache_filename("こんにちは")

    monkeypatch.setattr(server_module.settings, "tts_edge_rate", "-15%", raising=False)
    slower = _tts_cache_filename("こんにちは")

    monkeypatch.setattr(server_module.settings, "tts_edge_voice", "ja-JP-KeitaNeural", raising=False)
    other_voice = _tts_cache_filename("こんにちは")

    assert len({base, slower, other_voice}) == 3


def test_different_tld_does_not_return_stale_audio(tts, monkeypatch):
    """言語設定を変えると以前作ったファイルを再利用しない"""
    text = _unique("言語切替")

    monkeypatch.setattr(server_module.settings, "tts_tld", "co.jp", raising=False)
    first = generate_tts_cached(text)
    first_calls = len(tts.calls)

    monkeypatch.setattr(server_module.settings, "tts_tld", "com", raising=False)
    second = generate_tts_cached(text)

    assert first != second
    assert len(tts.calls) == first_calls + 1, "tld 変更でも再生成されていない"
    assert tts.calls[-1].tld == "com"


# --- 生成と再利用 --------------------------------------------------------------
def test_creates_file_on_first_call(tts):
    filename = generate_tts_cached(_unique())
    path = server_module.CACHE_DIR / filename
    assert path.is_file()
    assert path.stat().st_size > 0


def test_creates_cache_dir_on_demand(tts):
    """import 時ではなく初回生成時にディレクトリが作られる"""
    assert not server_module.CACHE_DIR.exists() or list(server_module.CACHE_DIR.iterdir()) == []
    generate_tts_cached(_unique())
    assert server_module.CACHE_DIR.is_dir()


def test_same_text_regenerates_only_once(tts):
    text = _unique()
    generate_tts_cached(text)
    generate_tts_cached(text)
    assert len(tts.calls) == 1


def test_uses_cached_file_on_second_call(tts):
    text = _unique()
    first = generate_tts_cached(text)
    second = generate_tts_cached(text)
    assert first == second
    assert len(tts.calls) == 1


def test_different_text_creates_different_files(tts):
    assert generate_tts_cached("曲A") != generate_tts_cached("曲B")


def test_no_partial_files_left_behind(tts):
    """atomic install により .tmp が残らない"""
    generate_tts_cached(_unique())
    leftovers = [p.name for p in server_module.CACHE_DIR.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_failed_generation_leaves_no_partial_file(tts, monkeypatch):
    """gTTS が全滅したら temp file を残さず TTSError を投げる"""
    from retro_radio.utils.errors import TTSError

    def boom(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(server_module, "gTTS", boom)
    with pytest.raises(TTSError):
        generate_tts_cached(_unique())

    leftovers = [p.name for p in server_module.CACHE_DIR.iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_tts_error_message_does_not_leak_internals(tts, monkeypatch):
    from retro_radio.utils.errors import TTSError

    def boom(*args, **kwargs):
        raise RuntimeError("connect to /home/deploy/.env failed")

    monkeypatch.setattr(server_module, "gTTS", boom)
    with pytest.raises(TTSError) as exc:
        generate_tts_cached(_unique())

    assert "/home/deploy" not in exc.value.user_message
    assert "connect to" not in exc.value.user_message
    assert "音声の合成に失敗しました" in exc.value.user_message


def test_falls_back_to_generic_tld_on_failure(tts, monkeypatch):
    """最初の tld で失敗しても汎用ドメインで1回だけリトライする"""
    calls = []

    class _Flaky:
        def __init__(self, text="", lang=None, tld=None, slow=False, **kwargs):
            calls.append(tld)
            self.tld = tld
            self.fail = tld == "co.jp"

        def save(self, path):
            if self.fail:
                raise RuntimeError("co.jp unavailable")
            with open(path, "wb") as f:
                f.write(b"ID3")

    monkeypatch.setattr(server_module, "gTTS", _Flaky)
    filename = generate_tts_cached(_unique())

    assert calls == ["co.jp", "com"]
    assert filename.endswith(".mp3")
    assert (server_module.CACHE_DIR / filename).is_file()


def test_retry_failure_raises_tts_error(tts, monkeypatch):
    from retro_radio.utils.errors import TTSError

    def boom(*args, **kwargs):
        raise RuntimeError("all down")

    monkeypatch.setattr(server_module, "gTTS", boom)
    with pytest.raises(TTSError):
        generate_tts_cached(_unique())


# --- TTL sweep -----------------------------------------------------------------
def test_sweep_removes_expired_files(tts, monkeypatch):
    monkeypatch.setattr(server_module.settings, "tts_cache_ttl_days", 1, raising=False)

    old = server_module.CACHE_DIR / "tts_old.mp3"
    old.write_bytes(b"old")
    stale_mtime = time.time() - 3 * 86400
    os.utime(old, (stale_mtime, stale_mtime))

    fresh = server_module.CACHE_DIR / "tts_fresh.mp3"
    fresh.write_bytes(b"fresh")

    removed = server_module._sweep_tts_cache(force=True)

    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_keeps_fresh_files(tts):
    filename = generate_tts_cached(_unique())
    assert server_module._sweep_tts_cache(force=True) == 0
    assert (server_module.CACHE_DIR / filename).exists()


def test_sweep_ignores_non_audio_files(tts):
    other = server_module.CACHE_DIR / "notes.txt"
    other.write_text("keep me")
    server_module._sweep_tts_cache(force=True)
    assert other.exists()


def test_sweep_on_missing_directory_is_noop(tts):
    import shutil

    shutil.rmtree(server_module.CACHE_DIR, ignore_errors=True)
    assert server_module._sweep_tts_cache(force=True) == 0


def test_sweep_is_rate_limited(tts, monkeypatch):
    """毎回の掃除は不要（sweep_interval 回ごとに1度だけ走らせる）"""
    monkeypatch.setattr(server_module.settings, "tts_cache_sweep_interval", 3, raising=False)
    monkeypatch.setattr(server_module.settings, "tts_cache_ttl_days", 1, raising=False)
    monkeypatch.setattr(server_module, "_sweep_counter", 0, raising=False)

    (server_module.CACHE_DIR).mkdir(parents=True, exist_ok=True)
    old = server_module.CACHE_DIR / "tts_old.mp3"
    old.write_bytes(b"old")
    stale = time.time() - 5 * 86400
    os.utime(old, (stale, stale))

    assert server_module._sweep_tts_cache() == 0   # 1回目: スキップ
    assert server_module._sweep_tts_cache() == 0   # 2回目: スキップ
    assert server_module._sweep_tts_cache() == 1   # 3回目: 実行
    assert not old.exists()


# --- セグメント単位の TTS ------------------------------------------------------
def test_generate_tts_for_segments_attaches_audio_url(tts):
    from retro_radio.core.script_generator import parse_script_segments

    segments = parse_script_segments("### オープニング\nこんにちは\n### エンディング\nさようなら")
    result = server_module.generate_tts_for_segments(segments)

    assert len(result) == 2
    for segment in result:
        assert segment.metadata["audio_url"].startswith("/api/audio/tts_")


def test_generate_tts_for_segments_survives_failure(tts, monkeypatch):
    """個別セグメントの TTS 失敗でも全体が例外を投げない（audio_url=None）"""
    from retro_radio.core.script_generator import parse_script_segments

    monkeypatch.setattr(
        server_module, "generate_tts_cached", lambda text: (_ for _ in ()).throw(RuntimeError("x"))
    )
    segments = parse_script_segments("### オープニング\nこんにちは")
    result = server_module.generate_tts_for_segments(segments)

    assert result[0].metadata["audio_url"] is None


def test_empty_content_segment_gets_no_audio(tts):
    from retro_radio.models.radio import ScriptSegment

    segment = ScriptSegment(
        id="s", title="t", content="   ", estimated_duration=0.0, order=0, metadata={}
    )
    result = server_module.generate_tts_for_segments([segment])
    assert "audio_url" not in (result[0].metadata or {})


@pytest.mark.network
def test_real_gtts_is_opt_in():
    """実 gTTS を使うテストは `network` マーカー付き"""
    filename = generate_tts_cached(_unique("実ネットワーク"))
    assert filename.endswith(".mp3")
