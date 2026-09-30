from retro_radio.utils.text_cleaner import clean_script_for_tts


def test_clean_script_removes_asterisks():
    """アスタリスクが除去されること"""
    text = "**太字** と *斜体* と ***太字斜体***"
    result = clean_script_for_tts(text)
    assert "*" not in result
    assert "太字" in result
    assert "斜体" in result


def test_clean_script_removes_numbered_sections():
    """番号付きセクションヘッダーが除去されること"""
    text = """1. オープニング挨拶
本文です。
2. その日の主要ニュース
ニュースです。"""
    result = clean_script_for_tts(text)
    assert "1. オープニング挨拶" not in result
    assert "2. その日の主要ニュース" not in result
    assert "本文です。" in result
    assert "ニュースです。" in result


def test_clean_script_removes_structure_keywords():
    """構造キーワードを含む行が除去されること"""
    text = """皆様、こんばんは。
その日の主要な出来事についてお伝えします。
本日は良い天気でした。
オープニング挨拶を申し上げます。
それでは、ヒット曲をお届けします。"""
    result = clean_script_for_tts(text)
    assert "その日の主要な出来事" not in result
    assert "オープニング挨拶" not in result
    assert "ヒット曲をお届けします" not in result
    assert "皆様、こんばんは。" in result
    assert "本日は良い天気でした。" in result


def test_clean_script_preserves_main_content():
    """主要な読み上げコンテンツが保持されること"""
    text = """皆様、いかがお過ごしでしょうか。
昭和55年、街には活気があふれておりました。
当時の物価は今とは大きく異なります。
それでは、この年のヒット曲をお届けします。"""
    result = clean_script_for_tts(text)
    assert "皆様、いかがお過ごしでしょうか。" in result
    assert "昭和55年、街には活気があふれておりました。" in result
    assert "当時の物価は今とは大きく異なります。" in result


def test_clean_script_handles_empty_input():
    """空入力が正しく処理されること"""
    assert clean_script_for_tts("") == ""
    assert clean_script_for_tts(None) == ""
