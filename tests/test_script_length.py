"""原稿生成の検証。

Gemini API キー未設定時のフォールバック経路を正として検証する
（CI に API キーは無いので、これが常に実行される経路になる）。

**提案⑨（S2）による変更**: 「target_script_chars（1,000 字）以上」を
**文字数の帯**に置き換えた。帯の実測値と根拠は ``eval/metrics/length.py`` の
docstring にある。「以上」は水増しを最適行動にしていたため、
**下限と上限の両方**を罰する。
"""

import pytest

from eval.metrics.length import check_length, length_bounds
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

#: 旧来のモード別下限テーブル。**帯が正**なので-mode ごとの値は持たない。
#: 参照している箇所が残っている場合は削除すること（ハーネス側で診断が出る）。
MIN_SCRIPT_CHARS: dict = {}


def test_length_band_is_derived_from_settings_not_hardcoded():
    """帯が設定から導出されていること（ハードコードしていない）

    ``config.py`` は S4 の所有ファイルなので S2 は**削除も変更もしない**。
    ``target_script_chars`` と ``script_char_tolerance`` は下限・上限に
    **再解釈**されているだけである。設定が動けば帯も動く。
    """
    settings = get_settings()
    lower, upper = length_bounds(settings)
    assert lower == settings.target_script_chars - settings.script_char_tolerance
    # 上限は「設定由来 1400」と「実測 p95 の切上げ 1500」の大きいほう。
    assert upper >= settings.target_script_chars + settings.script_char_tolerance * 2
    assert upper == 1500, "実測 p95=1476 を 50 刻みで切り上げた値"
    assert lower < upper


def test_api_key_is_not_configured_in_tests():
    """CI/ローカルともに API キーに依存しない（キーがあればこのテストは落ちる）"""
    assert get_settings().gemini_api_key == ""


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
def test_script_length_within_budget(mode):
    """フォールバック原稿が**帯**に入ること（短すぎても長すぎても落ちる）"""
    result = check_length(SCRIPTS[mode](1980, 5, 15))
    assert result.ok, result.describe()
    # 「300 字未満の原稿は台本ではない」という旧ガードも残す。
    assert result.length >= 300, f"台本として短すぎる: {result.length}"


def test_all_fallback_scripts_stay_inside_the_length_band():
    """全フォールバック原稿が**帯**に入ること（「以上」から「帯内」へ）"""
    for mode, builder in SCRIPTS.items():
        result = check_length(builder(1980, 5, 15))
        assert result.ok, f"{mode}: {result.describe()}"


@pytest.mark.parametrize("mode", ["normal", "care_recreation", "anniversary"])
@pytest.mark.parametrize("year", [1955, 1975, 1995, 2015, 2025])
def test_fallback_scripts_stay_inside_the_band_for_every_decade(mode, year):
    """年代ごとに代表曲・歴史番組・クイズが変わるため、どの年代でも帯内であること"""
    result = check_length(SCRIPTS[mode](year, 5, 15))
    assert result.ok, f"{mode}/{year}: {result.describe()}"


def test_script_length_of_generate_radio_script():
    """generate_radio_script（キー未設定 → フォールバック）の長さ"""
    result = check_length(generate_radio_script(1980, 5, 15))
    assert result.ok, result.describe()


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
