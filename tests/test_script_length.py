"""原稿生成の検証。

Gemini API キー未設定時のフォールバック経路を正として検証する
（CI に API キーは無いので、これが常に実行される経路になる）。
"""

import pytest

from retro_radio.config import get_settings
from retro_radio.core.fallback import (
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
)
from retro_radio.core.script_generator import generate_radio_script, parse_script_segments


SCRIPTS = {
    "normal": lambda y, m, d: generate_fallback_script(y, m, d),
    "care_recreation": lambda y, m, d: generate_care_script(y, m, d),
    "anniversary": lambda y, m, d: generate_anniversary_script(y, m, d, "花子"),
}

# フォールバック原稿の実測下限。全モードとも target_script_chars(1000) に届く。
MIN_SCRIPT_CHARS = {
    "normal": 800,
    "care_recreation": 800,
    "anniversary": 800,
}


def test_api_key_is_not_configured_in_tests():
    """CI/ローカルともに API キーに依存しない（キーがあればこのテストは落ちる）"""
    assert get_settings().gemini_api_key == ""


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_script_length_within_budget(mode):
    """フォールバック原稿が下限を満たすこと（空虚な原稿を返さない）"""
    script = SCRIPTS[mode](1980, 5, 15)
    length = len(script)

    assert length >= MIN_SCRIPT_CHARS[mode], f"短すぎる: {length}"
    assert length >= 300, f"台本として短すぎる: {length}"


def test_all_fallback_scripts_reach_target_length():
    """全フォールバック原稿が target_script_chars に届くこと"""
    settings = get_settings()
    for mode, builder in SCRIPTS.items():
        assert len(builder(1980, 5, 15)) >= settings.target_script_chars, mode


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
@pytest.mark.parametrize("year", [1955, 1975, 1995, 2015, 2025])
def test_fallback_scripts_reach_target_length_for_every_decade(mode, year):
    """年代ごとに代表曲・歴史番組・クイズが変わるため、どの年代でも字数が足りること"""
    settings = get_settings()
    script = SCRIPTS[mode](year, 5, 15)
    assert len(script) >= settings.target_script_chars, (mode, year, len(script))


def test_script_length_of_generate_radio_script():
    """generate_radio_script（キー未設定 → フォールバック）の長さ"""
    script = generate_radio_script(1980, 5, 15)
    assert 800 <= len(script) <= 3000, f"文字数: {len(script)}"


def test_script_contains_target_date():
    script = generate_radio_script(1980, 5, 15)
    assert "1980年5月15日" in script


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_script_structure(mode):
    """主要セクションと曲振りが含まれる"""
    script = SCRIPTS[mode](1980, 5, 15)
    assert "### オープニング" in script
    assert "### エンディング" in script
    assert "### " in script
    assert "曲" in script


def test_fallback_script_segments():
    """フォールバック原稿は 4 セグメント以上にパースできる"""
    segments = parse_script_segments(generate_fallback_script(1980, 5, 15))
    assert len(segments) >= 4

    titles = [s.title for s in segments]
    assert any("オープニング" in t for t in titles)
    assert any("エンディング" in t for t in titles)


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_fallback_script_segment_titles_match_prompt(mode):
    """見出し名が `_build_segmented_prompt` が要求する名前と一致すること（playlist の前提）"""
    from retro_radio.core.script_generator import _build_segmented_prompt

    prompt = _build_segmented_prompt(1980, 5, 15, mode=mode)
    expected = [
        line[4:].strip()
        for line in prompt.splitlines()
        if line.startswith("### ")
    ]
    actual = [s.title for s in parse_script_segments(SCRIPTS[mode](1980, 5, 15))]

    assert actual == expected, mode


def test_anniversary_script_contains_target_name():
    script = generate_anniversary_script(1980, 5, 15, "花子")
    assert "花子" in script


def test_generate_radio_script_without_api_key_uses_fallback(monkeypatch):
    """API キー未設定なら外部 API を呼ばずフォールバックを返す"""
    import retro_radio.core.script_generator as sg

    monkeypatch.setattr(sg.settings, "gemini_api_key", "", raising=False)
    script = sg.generate_radio_script(1975, 9, 24)
    assert "1975年9月24日" in script
    assert script == generate_fallback_script(1975, 9, 24)


def test_generate_radio_script_falls_back_when_gemini_fails(monkeypatch):
    """Gemini 呼び出しが失敗してもフォールバック原稿を返す（例外を出さない）"""
    import retro_radio.core.script_generator as sg

    monkeypatch.setattr(sg.settings, "gemini_api_key", "AIza" + "A" * 35, raising=False)
    monkeypatch.setattr(
        sg, "_call_gemini", lambda prompt: (_ for _ in ()).throw(RuntimeError("api down"))
    )
    script = sg.generate_radio_script(1975, 9, 24)
    assert "1975年9月24日" in script
