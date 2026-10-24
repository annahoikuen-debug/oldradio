"""外部 HTTP タイムアウトの回帰テスト（H-3）。

実測された不具合
----------------
gTTS 2.5.4 の `timeout` 既定は ``None``（= 無限待ち）、`google-genai` の既定
クライアントは ``httpx.Timeout(None)``（同様に無限）。どちらも呼び出し元は
``server._run_job`` の ``finally``（同時実行スロットと入場枠チケットの解放）や
tenacity の ``time.sleep``（キャンセルチェックの発火地点）を**前に**ブロック
してしまい、1 回のハングが「プロセス再起動まで 503」「原稿生成が固まる」に
発展する。tenacity の ``@retry`` は試行**回数**しか制限しないので、ハングを
待たせていてはリトライにもならない。

ここでは「有限であること」を**実際の呼び出し引数として観測**して固定する。
ソースの文字列 grep だけでは、設定が呼ばれずに渡らないことを見逃す。
"""

from types import SimpleNamespace

import pytest

from retro_radio.config import Settings, get_settings


# ==============================================================================
# 1. 設定として存在し、有限であること
# ==============================================================================
def test_timeout_settings_are_finite_and_bounded():
    s = get_settings()
    assert 0 < s.tts_connect_timeout_seconds <= 120
    assert 0 < s.tts_read_timeout_seconds <= 300
    assert 0 < s.gemini_timeout_seconds <= 600


def test_timeout_settings_reject_non_positive_values():
    """0 / 負数は「無限待ち」と同義なので弾く（`ge` が無いと形だけの設定になる）。"""
    for kwargs in (
        {"tts_connect_timeout_seconds": 0},
        {"tts_read_timeout_seconds": 0},
        {"gemini_timeout_seconds": 0},
        {"tts_connect_timeout_seconds": -1},
    ):
        with pytest.raises(Exception):
            Settings(**kwargs)


# ==============================================================================
# 2. gTTS 呼び出しが timeout を受け取っていること
# ==============================================================================
def test_server_gtts_call_passes_timeout(mock_gtts, monkeypatch):
    """`server._generate_tts_via_gtts` が接続/読み取りタイムアウトを渡すこと。"""
    import retro_radio.server as server_module
    from retro_radio.core import tts_engines

    # 既定 `auto` は edge-tts を先に試すため、gTTS 経路へ明示的に固定する。
    monkeypatch.setattr(server_module, "_active_tts_engine", lambda: tts_engines.ENGINE_GTTS)

    settings = get_settings()
    server_module.generate_tts_cached("タイムアウト検証用の原稿")

    assert mock_gtts.calls, "gTTS が一度も呼ばれていない"
    kwargs = mock_gtts.calls[-1].kwargs
    assert kwargs.get("timeout") == (
        settings.tts_connect_timeout_seconds,
        settings.tts_read_timeout_seconds,
    ), f"gTTS に timeout が渡っていない: {kwargs}"


def test_core_gtts_call_passes_timeout(monkeypatch):
    """`core.tts.gtts_tts`（音声合成の本体）が timeout を渡すこと。"""
    import retro_radio.core.tts as core_tts

    seen: list = []

    class _Recorder:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def save(self, path):
            from pathlib import Path

            Path(path).write_bytes(b"\x00\x00")

    monkeypatch.setattr(core_tts, "gTTS", _Recorder)
    settings = get_settings()

    core_tts.gtts_tts("タイムアウト検証", lang=settings.tts_language)

    assert seen, "core.tts の gTTS が呼ばれていない"
    assert seen[-1].get("timeout") == (
        settings.tts_connect_timeout_seconds,
        settings.tts_read_timeout_seconds,
    ), seen[-1]


def test_error_audio_passes_timeout(monkeypatch):
    """`generate_error_audio` が無限待ちのまま戻れなくなる経路であること。"""
    import retro_radio.core.tts as core_tts

    seen: list = []

    class _Recorder:
        def __init__(self, **kwargs):
            seen.append(kwargs)

        def save(self, path):
            from pathlib import Path

            Path(path).write_bytes(b"\x00\x00")

    monkeypatch.setattr(core_tts, "gTTS", _Recorder)
    settings = get_settings()

    assert core_tts.generate_error_audio("エラーです") is not None
    assert seen, "generate_error_audio の gTTS が呼ばれていない"
    assert seen[-1].get("timeout") == (
        settings.tts_connect_timeout_seconds,
        settings.tts_read_timeout_seconds,
    ), seen[-1]


# ==============================================================================
# 3. Gemini クライアントがタイムアウトを受け取っていること
# ==============================================================================
def test_call_gemini_passes_http_timeout(monkeypatch):
    """`_call_gemini` が `HttpOptions.timeout`（**ミリ秒**）を渡すこと。"""
    from google import genai as google_genai

    import retro_radio.core.script_generator as sg

    captured: dict = {}

    class _FakeClient:
        def __init__(self, **kwargs):
            captured["init"] = kwargs
            self.models = self

        def generate_content(self, **kwargs):
            captured["generate"] = kwargs
            return SimpleNamespace(text="  原稿テキスト  ")

    monkeypatch.setattr(google_genai, "Client", _FakeClient)
    # 空キーは即座に例外になるので、検証対象の経路を通すためだけに設定する。
    monkeypatch.setattr(sg.settings, "gemini_api_key", "AIza" + "A" * 35)

    assert sg._call_gemini("プロンプト") == "原稿テキスト"

    init = captured["init"]
    assert "http_options" in init, f"HttpOptions が渡っていない: {init}"
    timeout_ms = init["http_options"].timeout
    assert isinstance(timeout_ms, int), "HttpOptions.timeout はミリ秒の int である"
    assert timeout_ms == int(get_settings().gemini_timeout_seconds * 1000), timeout_ms
    assert 0 < timeout_ms <= 600_000, "有限であること（無限 = None は禁止）"


def test_call_gemini_reads_the_configured_timeout():
    """設定の秒数がミリ秒へ正しく変換されていること（境界の取り違え防止）。"""
    s = get_settings()
    assert int(s.gemini_timeout_seconds * 1000) == int(s.gemini_timeout_seconds) * 1000
