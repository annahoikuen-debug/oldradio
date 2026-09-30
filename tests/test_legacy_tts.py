"""旧 TTS エンドポイントへのフォールバックを固定する回帰テスト。

実測した障害:
  gTTS 2.5.4 は `POST /_/TranslateWebserverUi/data/batchexecute` を使うが、
  Google の CAPTCHA 対策で 429（"unusual traffic"）が返る。
  一方、旧来の `GET /translate_tts` は 200 を返すので救済経路として使う。
"""

import pytest

from retro_radio import server as server_module
from retro_radio.core import legacy_tts
from retro_radio.utils.errors import TTSError


# ==============================================================================
# 1. クライアント単体の挙動
# ==============================================================================
class _Response:
    def __init__(self, status_code=200, content=b"\xff\xf3\x00"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": "audio/mpeg"}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("HTTP %s" % self.status_code)


def test_chunks_at_google_limit():
    """Google の 1 リクエスト上限 100 文字で分割する"""
    assert legacy_tts._chunk("a" * 250) == ["a" * 100, "a" * 100, "a" * 50]
    assert legacy_tts._chunk("a" * 100) == ["a" * 100]
    assert legacy_tts._chunk("") == []


def test_synthesize_concatenates_mp3_chunks(monkeypatch):
    """分割した各チャンクの MP3 を連結して返す"""
    seen = []

    def _fake_get(url, headers=None, timeout=None):
        seen.append(url)
        return _Response(content=b"\xff\xf3" + bytes([len(seen)]))

    monkeypatch.setattr(legacy_tts.requests, "get", _fake_get)
    out = legacy_tts.synthesize("あ" * 250, lang="ja", tld="co.jp")

    assert len(seen) == 3
    assert all("translate.google.co.jp" in u for u in seen)
    assert all("tl=ja" in u for u in seen)
    assert out == b"\xff\xf3\x01\xff\xf3\x02\xff\xf3\x03"


def test_synthesize_rejects_non_mp3(monkeypatch):
    """MP3 以外の応答は受け入れない（HTML エラーページの混入を防ぐ）"""
    monkeypatch.setattr(
        legacy_tts.requests, "get",
        lambda *a, **k: _Response(content=b"<!DOCTYPE html><html>error</html>"),
    )
    with pytest.raises(legacy_tts.LegacyTTSError):
        legacy_tts.synthesize("テスト")


def test_synthesize_rejects_error_status(monkeypatch):
    monkeypatch.setattr(
        legacy_tts.requests, "get", lambda *a, **k: _Response(status_code=429)
    )
    with pytest.raises(legacy_tts.LegacyTTSError):
        legacy_tts.synthesize("テスト")


def test_synthesize_rejects_empty_text():
    with pytest.raises(legacy_tts.LegacyTTSError):
        legacy_tts.synthesize("   ")


def test_synthesize_reports_connection_failure(monkeypatch):
    def _boom(*args, **kwargs):
        raise legacy_tts.requests.RequestException("dns failure")

    monkeypatch.setattr(legacy_tts.requests, "get", _boom)
    with pytest.raises(legacy_tts.LegacyTTSError):
        legacy_tts.synthesize("テスト")


def test_save_writes_bytes(monkeypatch, tmp_path):
    monkeypatch.setattr(
        legacy_tts.requests, "get",
        lambda *a, **k: _Response(content=b"\xff\xf3rest"),
    )
    target = tmp_path / "out.mp3"
    assert legacy_tts.save("テスト", str(target), lang="ja", tld="com") == str(target)
    assert target.read_bytes() == b"\xff\xf3rest"


# ==============================================================================
# 2. server からのフォールバック
# ==============================================================================
def test_429_falls_back_to_legacy_endpoint(monkeypatch, mock_gtts, tmp_path):
    """gTTS が 429 のとき旧エンドポイントへ切り替えて音声を作る"""
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server_module, "_tts_rate_limited_until", 0.0)

    def _blocked(*args, **kwargs):
        raise RuntimeError("429 (Too Many Requests) from TTS API. Probable cause: Unknown")

    monkeypatch.setattr(server_module, "gTTS", _blocked)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    def _fake_synthesize(text, lang=None, tld=None):
        assert tld == "co.jp", "TLD は co.jp のまま（jp に切り詰めない）"
        return b"\xff\xf3legacy-audio"

    monkeypatch.setattr(server_module.legacy_tts, "synthesize", _fake_synthesize)

    name = server_module.generate_tts_cached("テスト原稿")
    written = (tmp_path / name).read_bytes()
    assert written == b"\xff\xf3legacy-audio"


def test_legacy_path_is_used_when_circuit_is_open(monkeypatch, mock_gtts, tmp_path):
    """ブレーカーが開いているときは gTTS を一切叩かず旧経路へ"""
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("ブレーカー開放中に gTTS を叩いてはいけない")

    monkeypatch.setattr(server_module, "gTTS", _must_not_be_called)
    monkeypatch.setattr(
        server_module, "_tts_is_network_client", lambda: True
    )
    monkeypatch.setattr(
        server_module, "_tts_rate_limited_until", 9e18
    )

    calls = []
    monkeypatch.setattr(
        server_module.legacy_tts, "synthesize",
        lambda text, lang=None, tld=None: calls.append(text) or b"\xff\xf3ok",
    )
    monkeypatch.setattr(server_module.time, "sleep", lambda s: None)

    name = server_module.generate_tts_cached("ブレーカー検証")
    assert calls == ["ブレーカー検証"]
    assert (tmp_path / name).read_bytes() == b"\xff\xf3ok"


def test_both_paths_failing_raises_tts_error(monkeypatch, mock_gtts, tmp_path):
    """両方失敗したら TTSError（空音声を黙って返さない）"""
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server_module, "_tts_rate_limited_until", 0.0)

    def _blocked(*args, **kwargs):
        raise RuntimeError("429 (Too Many Requests) from TTS API")

    monkeypatch.setattr(server_module, "gTTS", _blocked)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    def _legacy_fail(text, lang=None, tld=None):
        raise legacy_tts.LegacyTTSError("also down")

    monkeypatch.setattr(server_module.legacy_tts, "synthesize", _legacy_fail)

    with pytest.raises(TTSError):
        server_module.generate_tts_cached("両方失敗")


def test_gtts_success_does_not_touch_legacy(monkeypatch, mock_gtts, tmp_path):
    """gTTS が成功したら旧経路には一切触らない（通常の経路を壊さない）"""
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path)

    def _must_not_be_called(*args, **kwargs):
        raise AssertionError("gTTS が成功しているのに旧経路を叩いた")

    monkeypatch.setattr(server_module.legacy_tts, "synthesize", _must_not_be_called)

    name = server_module.generate_tts_cached("通常経路")
    assert name.startswith("tts_")
    assert (tmp_path / name).exists()


def test_legacy_failure_leaves_no_partial_file(monkeypatch, mock_gtts, tmp_path):
    """旧経路が失敗しても tmp ファイルを残さない"""
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server_module, "_tts_rate_limited_until", 0.0)

    def _blocked(*args, **kwargs):
        raise RuntimeError("429 (Too Many Requests) from TTS API")

    monkeypatch.setattr(server_module, "gTTS", _blocked)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    def _legacy_fail(text, lang=None, tld=None):
        raise legacy_tts.LegacyTTSError("down")

    monkeypatch.setattr(server_module.legacy_tts, "synthesize", _legacy_fail)

    with pytest.raises(TTSError):
        server_module.generate_tts_cached("残骸なし")

    leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".tts_")]
    assert leftovers == [], leftovers
    assert list(tmp_path.glob("*.mp3")) == []


def test_tts_error_message_does_not_leak_internals(monkeypatch, mock_gtts, tmp_path):
    """ユーザー向けメッセージに内部例外が混ざらない"""
    monkeypatch.setattr(server_module, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(server_module, "_tts_rate_limited_until", 0.0)

    def _blocked(*args, **kwargs):
        raise RuntimeError("secret gTTS internal detail")

    monkeypatch.setattr(server_module, "gTTS", _blocked)
    monkeypatch.setattr(server_module, "_tts_is_network_client", lambda: False)

    def _legacy_fail(text, lang=None, tld=None):
        raise legacy_tts.LegacyTTSError("secret legacy internal detail")

    monkeypatch.setattr(server_module.legacy_tts, "synthesize", _legacy_fail)

    with pytest.raises(TTSError) as excinfo:
        server_module.generate_tts_cached("秘匿")

    message = excinfo.value.user_message
    assert "secret gTTS internal detail" not in message
    assert "secret legacy internal detail" not in message
    assert "音声の合成に失敗しました" in message
