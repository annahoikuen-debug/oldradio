"""gTTS の 429（レート制限）対応と、無声トークの扱いを固定する回帰テスト。

実測された不具合:
  - 1 回の /api/generate で gTTS を最大 6 回直列に叩いていたため、
    Google 側のレート制限で **全セグメントが 429** になり、
    番組が「原稿だけの無声」になっていた。
  - 個別音声が無いトークは url='' のトラックになり、<audio> が error を
    出して 500ms 後にスキップ → 原稿が丸ごと消えていた。
"""

import re
from pathlib import Path

import pytest

from retro_radio import server as server_module
from retro_radio.config import get_settings

APP_JS = Path(__file__).resolve().parents[1] / "static" / "app.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    return APP_JS.read_text(encoding="utf-8")


# ==============================================================================
# 1. 呼び出し間隔
# ==============================================================================
def test_tts_interval_settings_exist():
    """gTTS 呼び出し間隔と 429 バックオフが設定として存在する"""
    settings = get_settings()
    assert settings.tts_min_interval_seconds > 0
    assert settings.tts_retry_backoff_seconds > 0


def test_throttle_is_applied_before_every_gtts_call():
    """実 gTTS 呼び出しの前に必ず間隔チェックが入る"""
    src = Path(server_module.__file__).read_text(encoding="utf-8")
    body = src[src.index("def _generate_tts_via_gtts"):src.index("def generate_tts_cached")]
    assert "for attempt, tld in enumerate" in body
    # ループの本体先頭で _tts_throttle() を呼ぶ
    assert re.search(r"for attempt, tld in enumerate\([^)]*\)[^:]*:\s*\n\s*_tts_throttle\(\)", body)


def test_throttle_only_applies_to_the_real_gtts_client():
    """テストのスタブ差し替え時は間隔待ちをしない（テストが数十倍遅くなる）"""
    assert "def _tts_is_network_client" in Path(server_module.__file__).read_text(encoding="utf-8")
    src = Path(server_module.__file__).read_text(encoding="utf-8")
    helper = src[src.index("def _tts_is_network_client"):src.index("def _tts_throttle")]
    assert '"gtts"' in helper


def test_throttle_is_skipped_for_mocked_gtts(monkeypatch):
    """差し替えられたスタブでは sleep しない（テストが高速のまま）"""
    class _Stub:
        __module__ = "tests.fake_gtts"

    monkeypatch.setattr(server_module, "gTTS", _Stub)
    assert server_module._tts_is_network_client() is False

    real = type("gTTS", (), {"__module__": "gtts.tts"})
    monkeypatch.setattr(server_module, "gTTS", real)
    assert server_module._tts_is_network_client() is True


def test_rate_limit_is_detected():
    """429 を判定できる"""
    assert server_module._is_rate_limited(RuntimeError("429 (Too Many Requests) from TTS API"))
    assert server_module._is_rate_limited(RuntimeError("Too Many Requests"))
    assert not server_module._is_rate_limited(RuntimeError("connection reset"))


def test_429_backs_off_before_the_generic_domain_retry(monkeypatch, mock_gtts, tmp_path):
    """429 のときは空けてから汎用ドメインへ再試行する"""
    calls = []
    monkeypatch.setattr(server_module.time, "sleep", lambda s: calls.append(s))

    attempts = []

    def _flaky(*args, **kwargs):
        attempts.append(kwargs.get("tld"))
        if kwargs.get("tld") == "com":
            raise RuntimeError("still 429")
        raise RuntimeError("429 (Too Many Requests) from TTS API")

    monkeypatch.setattr(server_module, "gTTS", _flaky)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    with pytest.raises(Exception):
        server_module.generate_tts_cached("テスト用テキスト")

    assert attempts == ["co.jp", "com"], attempts
    assert calls, "429 後にバックオフの sleep が入っていない"


def test_backoff_not_applied_for_non_rate_limit_errors(monkeypatch, mock_gtts, tmp_path):
    """429 以外の失敗では無駄に待たない"""
    calls = []
    monkeypatch.setattr(server_module.time, "sleep", lambda s: calls.append(s))

    def _broken(*args, **kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(server_module, "gTTS", _broken)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    with pytest.raises(Exception):
        server_module.generate_tts_cached("テスト用テキスト")

    assert calls == [], calls


def test_all_attempts_failing_still_raises_tts_error(monkeypatch, mock_gtts, tmp_path):
    """2 段とも失敗したら TTSError（黙って空を返さない）"""
    def _broken(*args, **kwargs):
        raise RuntimeError("all down")

    monkeypatch.setattr(server_module, "gTTS", _broken)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    from retro_radio.utils.errors import TTSError

    with pytest.raises(TTSError):
        server_module.generate_tts_cached("テスト用テキスト")


def test_tts_error_message_does_not_leak_internals(monkeypatch, mock_gtts, tmp_path):
    """TTSError のメッセージに内部例外が混ざらない"""
    def _broken(*args, **kwargs):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(server_module, "gTTS", _broken)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    from retro_radio.utils.errors import TTSError

    with pytest.raises(TTSError) as excinfo:
        server_module.generate_tts_cached("テスト用テキスト")

    assert "secret internal detail" not in excinfo.value.user_message
    assert "音声の合成に失敗しました" in excinfo.value.user_message


# ==============================================================================
# 2. 無声トークを間奏として残す
# ==============================================================================
def test_talk_without_audio_becomes_intermission(app_js):
    """個別音声が無いトークは「空 URL のトラック」ではなく間奏にする"""
    build_pass = app_js[app_js.index("function buildPass"):app_js.index("function buildQueue(data)")]
    assert "kind: audioUrl ? TALK : INTERMISSION" in build_pass
    assert "getSilenceUrl(SILENCE_SLOT_SECONDS * 1.5)" in build_pass


def test_full_script_substitutes_the_first_silent_talk(app_js):
    """本文全体の音声があれば、最初の無音トークへ差し込む（原稿を丸ごと消さない）"""
    build_pass = app_js[app_js.index("function buildPass"):app_js.index("function buildQueue(data)")]
    assert "if (pass[i].beatOnly) {" in build_pass
    assert "pass[i].url = fullScriptUrl;" in build_pass
    assert "番組全文" in build_pass


def test_frontend_counts_silent_talks(app_js):
    """無声トークの数を数えてユーザーに伝える"""
    assert "function countSilentTalks" in app_js
    assert "countSilentTalks(data)" in app_js
    assert "読み上げ音声が生成できませんでした" in app_js


# ==============================================================================
# 3. サーキットブレーカー
# ==============================================================================
def test_circuit_breaker_setting_exists():
    settings = get_settings()
    assert settings.tts_circuit_breaker_seconds > 0


def test_429_opens_the_circuit(monkeypatch, mock_gtts, tmp_path):
    """429 を受けるとブレーカーが開き、以降の実呼び出しを即座に諦める"""
    calls = []
    monkeypatch.setattr(server_module.time, "sleep", lambda s: calls.append(s))
    monkeypatch.setattr(server_module, "_tts_rate_limited_until", 0.0)

    def _blocked(*args, **kwargs):
        raise RuntimeError("429 (Too Many Requests) from TTS API")

    class _RealLookingGTTS:
        """実 gTTS と同じ扱いになるよう __module__ を偽装したスタブ"""
        __module__ = "gtts.tts"

        def __init__(self, **kwargs):
            _blocked(**kwargs)

        def save(self, path):
            _blocked()

    monkeypatch.setattr(server_module, "gTTS", _RealLookingGTTS)

    from retro_radio.utils.errors import TTSError

    # 1 回目: 実際に叩いて 429 を受ける
    with pytest.raises(TTSError):
        server_module.generate_tts_cached("ブレーカー検証1")
    assert server_module._tts_circuit_open() is True

    # 2 回目: ブレーカーが開いているので短時間で即座に TTSError
    calls.clear()
    with pytest.raises(TTSError):
        server_module.generate_tts_cached("ブレーカー検証2")
    assert calls == [], "ブレーカーが開いている間は待たないはず"


def test_breaker_is_closed_for_mocked_gtts(monkeypatch):
    """スタブ差し替え時はブレーカーを開けない"""
    class _Stub:
        __module__ = "tests.fake_gtts"

    monkeypatch.setattr(server_module, "gTTS", _Stub)
    assert server_module._tts_circuit_open() is False


def test_breaker_does_not_block_cache_hits(monkeypatch, mock_gtts, tmp_path):
    """ブレーカーが開いていても、キャッシュ済み音声は返す"""
    from conftest import build_dummy_mp3

    filename = server_module._tts_cache_filename("キャッシュ済み")
    target = tmp_path / "breaker_cache"
    target.mkdir()
    monkeypatch.setattr(server_module, "CACHE_DIR", target)
    (target / filename).write_bytes(build_dummy_mp3())

    def _boom(*args, **kwargs):
        raise AssertionError("実 gTTS を叩いてはいけない")

    monkeypatch.setattr(server_module, "gTTS", _boom)

    assert server_module.generate_tts_cached("キャッシュ済み") == filename
