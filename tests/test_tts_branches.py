"""`retro_radio.core.tts` の品質フォールバックとキャッシュ経路のテスト。

既存の `test_tts_plan_quality.py` は「None ならそれでいい」という弱い検証で、
`text_to_speech` の品質分岐（premium / high / standard）と ElevenLabs の
フォールバックが未実行だった。ここでは課金の前提に直結する
「 paying ユーザーが standard 品質を掴まされない」経路と、
「外部 API が死んでも必ず音声が返る」保証を固定する。
"""

import os
from unittest.mock import MagicMock, patch

import pytest

from retro_radio.core import tts
from retro_radio.core.tts import (
    cleanup_audio_file,
    elevenlabs_tts,
    generate_error_audio,
    gtts_tts,
    text_to_speech,
)


@pytest.fixture
def gtts_calls(monkeypatch):
    """gTTS をネットワーク不要のスタブへ差し替え、生成された一時ファイルを片付ける。"""
    created = []

    class _FakeGTTS:
        def __init__(self, text="", lang=None, tld=None, slow=False, **kwargs):
            self.text = text
            self.lang = lang
            self.tld = tld
            self.slow = slow
            created.append(self)

        def save(self, path):
            from tests.conftest import build_dummy_mp3

            with open(path, "wb") as fh:
                fh.write(build_dummy_mp3())

    monkeypatch.setattr(tts, "gTTS", _FakeGTTS)
    return created


@pytest.fixture(autouse=True)
def _clear_tts_cache():
    """TTS キャッシュはモジュールグローバルなので、テスト間で持ち越さない。"""
    for cached_file in tts.TTS_CACHE_DIR.glob("*.mp3"):
        cached_file.unlink()
    yield
    for cached_file in tts.TTS_CACHE_DIR.glob("*.mp3"):
        cached_file.unlink()


# --- 標準品質（既定 / 未知の品質値） -------------------------------------------
def test_default_quality_uses_standard(gtts_calls, monkeypatch):
    with patch.object(tts, "gtts_tts", return_value="/tmp/std.mp3") as gtts_mock:
        result = text_to_speech("こんにちは")

    assert result == "/tmp/std.mp3"
    gtts_mock.assert_called_once_with("こんにちは")


def test_unknown_quality_falls_back_to_standard(gtts_calls, monkeypatch):
    """未知の品質値は standard 扱い（未知値を premium と誤認しない）。"""
    with patch.object(tts, "gtts_tts", return_value="/tmp/std.mp3") as gtts_mock:
        text_to_speech("こんにちは", force_quality="ultra")

    gtts_mock.assert_called_once_with("こんにちは")


def test_high_quality_uses_configured_voice(gtts_calls, monkeypatch):
    """high は gTTS の lang/tld/slow を設定値から渡す。"""
    with patch.object(tts, "gtts_tts", return_value="/tmp/high.mp3") as gtts_mock:
        text_to_speech("こんにちは", force_quality="high")

    gtts_mock.assert_called_once_with(
        "こんにちは",
        lang=tts.settings.tts_language,
        tld=tts.settings.tts_tld,
        slow=tts.settings.tts_slow,
    )


def test_high_quality_falls_back_to_standard_on_error(monkeypatch):
    """high 合成が失敗したら standard に落とすべき（ユーザーには必ず音声を返す）。"""
    calls = []

    def _fake(text, lang=None, tld=None, slow=None):
        calls.append((text, lang))
        if lang is not None:
            raise RuntimeError("gtts exploded")
        return "/tmp/std.mp3"

    monkeypatch.setattr(tts, "gtts_tts", _fake)

    assert text_to_speech("こんにちは", force_quality="high") == "/tmp/std.mp3"
    assert len(calls) == 2, "high 失敗後に standard で再試行していない"


# --- プレミアム品質（ElevenLabs） -----------------------------------------------
def test_premium_uses_elevenlabs_when_key_present(gtts_calls, monkeypatch):
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "el-key")

    with patch.object(tts, "elevenlabs_tts", return_value="/tmp/premium.mp3") as el_mock:
        result = text_to_speech("こんにちは", force_quality="premium")

    assert result == "/tmp/premium.mp3"
    el_mock.assert_called_once_with("こんにちは")


def test_premium_uses_standard_when_key_absent(gtts_calls, monkeypatch):
    """API キー未設定なら ElevenLabs を呼ばず standard にする（無駄な通信を避ける）。"""
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "")

    with patch.object(tts, "elevenlabs_tts") as el_mock, patch.object(
        tts, "gtts_tts", return_value="/tmp/std.mp3"
    ) as gtts_mock:
        result = text_to_speech("こんにちは", force_quality="premium")

    assert result == "/tmp/std.mp3"
    el_mock.assert_not_called()
    gtts_mock.assert_called_once_with("こんにちは")


def test_premium_falls_back_when_elevenlabs_raises(monkeypatch):
    """ElevenLabs が例外を投げても standard で必ず返すこと。"""
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "el-key")

    def _boom(text):
        raise RuntimeError("elevenlabs down")

    with patch.object(tts, "elevenlabs_tts", side_effect=_boom), patch.object(
        tts, "gtts_tts", return_value="/tmp/std.mp3"
    ) as gtts_mock:
        result = text_to_speech("こんにちは", force_quality="premium")

    assert result == "/tmp/std.mp3"
    gtts_mock.assert_called_once_with("こんにちは")


def test_premium_returns_none_when_everything_fails(monkeypatch):
    """全ての合成が失敗したら None（呼び出し元のフォールバックに委ねる）。"""
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "el-key")

    with patch.object(tts, "elevenlabs_tts", side_effect=RuntimeError("el")), patch.object(
        tts, "gtts_tts", side_effect=RuntimeError("gtts")
    ):
        assert text_to_speech("こんにちは", force_quality="premium") is None


def test_input_text_is_cleaned_before_synthesis(gtts_calls, monkeypatch):
    """原稿の構造記号が TTS に渡らないこと（読み上げaturally不自然になるため）。

    `clean_script_for_tts` は見出し語・アスタリスク・プロンプト指示行を除去するが、
    段落区切りの改行は残す（gTTS が読み上げの breath として使うため）。
    ここでは除去すべきものを明示的に検証する。
    """
    with patch.object(tts, "gtts_tts", return_value="/tmp/std.mp3") as gtts_mock:
        text_to_speech("**ホisted**\nあなたはラジオパーソナリティです。\n番組の始まり")

    passed_text = gtts_mock.call_args[0][0]
    assert "**" not in passed_text
    assert "あなたはラジオパーソナリティです。" not in passed_text
    assert "ホisted" in passed_text
    assert "番組の始まり" in passed_text


# --- gtts_tts とキャッシュ ------------------------------------------------------
def test_gtts_tts_writes_file_and_uses_settings_defaults(gtts_calls, tmp_path):
    result = gtts_tts("こんにちは")

    assert result is not None
    assert os.path.exists(result)
    assert gtts_calls[0].lang == tts.settings.tts_language
    assert gtts_calls[0].tld == tts.settings.tts_tld
    cleanup_audio_file(result)


def test_gtts_tts_caches_result_and_second_call_skips_synthesis(gtts_calls):
    """2回目は合成せずキャッシュから返す（Google への無用な打切りを避ける）。"""
    first = gtts_tts("キャッシュテスト")
    assert len(gtts_calls) == 1

    second = gtts_tts("キャッシュテスト")

    assert len(gtts_calls) == 1, "キャッシュヒットでも合成が走っている"
    assert second != first, "一時ファイルを直接返すと後始末で消されるためコピー必須"
    assert os.path.exists(second)
    cleanup_audio_file(first)
    cleanup_audio_file(second)


def test_cache_hit_returns_independent_copy(gtts_calls):
    """返り値のファイルを消してもキャッシュ本体は残る。"""
    first = gtts_tts("コピー検証")
    cleanup_audio_file(first)

    second = gtts_tts("コピー検証")

    assert os.path.exists(second)
    cleanup_audio_file(second)


def test_different_text_does_not_share_cache(gtts_calls):
    gtts_tts("テキストA")
    gtts_tts("テキストB")

    assert len(gtts_calls) == 2


def test_empty_cache_file_is_ignored(gtts_calls):
    """0バイトのキャッシュは「無効」として再合成する（再生不能なmp3を避ける）。"""
    import hashlib

    key = hashlib.sha256(
        f"空キャッシュ_{tts.settings.tts_language}_{tts.settings.tts_tld}_"
        f"{tts.settings.tts_slow}".encode("utf-8")
    ).hexdigest()
    (tts.TTS_CACHE_DIR / f"{key}.mp3").write_bytes(b"")

    result = gtts_tts("空キャッシュ")

    assert len(gtts_calls) == 1
    cleanup_audio_file(result)


def test_explicit_params_override_settings(gtts_calls):
    gtts_tts("明示指定", lang="en", tld="co.uk", slow=True)

    assert gtts_calls[0].lang == "en"
    assert gtts_calls[0].tld == "co.uk"
    assert gtts_calls[0].slow is True


def test_gtts_tts_returns_none_on_error(monkeypatch):
    """gTTS が壊れても例外を投げず None（上位で扱えるようにする）。"""
    monkeypatch.setattr(tts, "gTTS", MagicMock(side_effect=RuntimeError("boom")))

    assert gtts_tts("失敗する") is None


# --- ElevenLabs -----------------------------------------------------------------
def _fake_response(content=b"fake-mp3", raises=None):
    response = MagicMock(name="Response")
    response.content = content
    response.raise_for_status.side_effect = raises
    return response


def test_elevenlabs_posts_with_api_key(gtts_calls, monkeypatch, tmp_path):
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "el-key")
    monkeypatch.setattr(tts.settings, "elevenlabs_voice_id", "voice-1")
    monkeypatch.setattr(tts.settings, "elevenlabs_model_id", "model-1")

    with patch.object(tts.requests, "post", return_value=_fake_response()) as post:
        result = elevenlabs_tts("こんにちは")

    assert result is not None
    assert os.path.exists(result)
    url = post.call_args[0][0]
    assert url.endswith("/text-to-speech/voice-1")
    assert post.call_args[1]["headers"]["xi-api-key"] == "el-key"
    assert post.call_args[1]["json"]["model_id"] == "model-1"
    cleanup_audio_file(result)


def test_elevenlabs_falls_back_to_gtts_on_http_error(gtts_calls, monkeypatch):
    """HTTP エラー時は standard へフォールバック（ユーザーには必ず音声を返す）。"""
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", "el-key")

    with patch.object(
        tts.requests, "post", return_value=_fake_response(raises=RuntimeError("401"))
    ):
        result = elevenlabs_tts("フォールバック")

    assert result is not None
    assert len(gtts_calls) == 1
    cleanup_audio_file(result)


def test_elevenlabs_without_key_goes_straight_to_gtts(gtts_calls, monkeypatch):
    """キー未設定なら API を呼ばずに standard にする。"""
    monkeypatch.setattr(tts.settings, "elevenlabs_api_key", None)

    with patch.object(tts.requests, "post") as post:
        result = elevenlabs_tts("キーなし")

    post.assert_not_called()
    assert result is not None
    cleanup_audio_file(result)


# --- 一時ファイルの削除 ----------------------------------------------------------
def test_cleanup_removes_existing_file(tmp_path):
    target = tmp_path / "audio.mp3"
    target.write_bytes(b"data")

    cleanup_audio_file(str(target))

    assert not target.exists()


def test_cleanup_is_safe_for_missing_file():
    cleanup_audio_file("/nonexistent/path/audio.mp3")  # 例外を投げない


@pytest.mark.parametrize("value", [None, ""])
def test_cleanup_accepts_empty_path(value):
    """空文字 / None でも例外を投げないこと。"""
    cleanup_audio_file(value)  # 例外が出なければ成功


def test_cleanup_swallows_permission_errors(tmp_path):
    """削除できなくても呼び出し元を落とさない（best-effort 処理）。"""
    target = tmp_path / "locked.mp3"
    target.write_bytes(b"data")

    with patch.object(tts.os, "unlink", side_effect=PermissionError("locked")):
        cleanup_audio_file(str(target))


# --- エラー通知用音声 ------------------------------------------------------------
def test_generate_error_audio_returns_bytes(gtts_calls):
    audio = generate_error_audio("エラーが発生しました")

    assert audio is not None
    assert audio.startswith(b"ID3")


def test_generate_error_audio_returns_none_on_failure(monkeypatch):
    monkeypatch.setattr(tts, "gTTS", MagicMock(side_effect=RuntimeError("boom")))

    assert generate_error_audio("失敗") is None
