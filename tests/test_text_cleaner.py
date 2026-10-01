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


# ---------------------------------------------------------------------------
# 本文を消さないこと（Round 1）
# ---------------------------------------------------------------------------
#
# 構造要素の除去は「行頭パターンだけで本文を消す」構造が 4 箇所あり、
# 実際に司会の本文が丸ごと消えていた。以下はその再現と固定。


def test_clean_script_keeps_numbered_body_lines():
    """番号付きの**本文行**（列挙）を消さないこと。

    以前は `^\\s*\\d+[\\.、]\\s*.+$` が「数字＋.／、で始まる任意の行」を
    消していたため、本文が丸ごと消えていた。
    """
    text = "### T1\n1. 1975年、FOMOという流行語が誕生\n2. 石油危機\nbody.\n"
    result = clean_script_for_tts(text)
    assert "1975年、FOMOという流行語が誕生" in result
    assert "石油危機" in result
    assert "body." in result


def test_clean_script_keeps_numbered_lines_that_carry_sentences():
    """文（句点）を含む番号付き行も残ること。"""
    text = "1. 1975年の夏、スタイルが誕生しました。\n2. 今日はいい天気でした。\nおわり。"
    result = clean_script_for_tts(text)
    assert "今日はいい天気でした。" in result
    assert "おわり。" in result


def test_clean_script_does_not_drop_the_line_after_a_bare_hash_heading():
    """単独の `###` 行が**次の行の本文**を消さないこと。

    以前は `^\\s*#{1,6}\\s+.*$` の `\\s+` が改行 `\\n` を吸収し、
    続く `.*$` が次行全体を飲み込んでいた。
    """
    text = "### OPEN\nline1 body.\n###\nline2 IMPORTANT.\n### END\nbye"
    result = clean_script_for_tts(text)
    assert "line1 body." in result
    assert "line2 IMPORTANT." in result
    assert "bye" in result
    # 見出し行自体は消える（`### END` のような見出し行）。
    assert "### END" not in result


def test_clean_script_keeps_a_hash_only_line_as_body():
    """`#` の後ろに語が無い行は本文として扱う（消さない）。"""
    text = "before\n###\nafter"
    result = clean_script_for_tts(text)
    assert "before" in result
    assert "after" in result


def test_clean_script_keeps_bullet_points_that_are_not_stage_directions():
    """Markdown の箇条書きを丸ごと消さないこと。

    以前は `[※*]+` だけで消せてしまい、`* 1975 item one` が
    行ごと消えていた（実測）。
    """
    text = "### T1\nbody line.\n* 1975 item one\n* 1975 item two\n"
    result = clean_script_for_tts(text)
    assert "body line." in result
    assert "1975 item one" in result
    assert "1975 item two" in result


def test_clean_script_still_removes_symbol_only_stage_direction_lines():
    """記号だけの行は演出指示として除去されること（上記と引き換え）。"""
    text = "body line.\n**\nおわり。"
    result = clean_script_for_tts(text)
    assert "body line." in result
    assert "おわり。" in result
    assert "**" not in result


def test_clean_script_still_removes_stage_direction_lines_with_words():
    """演出語を含む行（`※音楽が流れる`）は除去されること。"""
    text = "start（※音楽が流れる）\nbody line.\n* 効果音\nend"
    result = clean_script_for_tts(text)
    assert "※音楽" not in result
    assert "body line." in result
    assert "end" in result


def test_clean_script_does_not_span_lines_with_stage_direction_parens():
    """`※` の括弧除去が改行を跨がないこと（閉じ括弧が無い場合も）。"""
    text = "start（※音楽が流れる\nこれは次の段落です。\nend"
    result = clean_script_for_tts(text)
    assert "start" in result
    assert "end" in result


# ---------------------------------------------------------------------------
# ニュース候補行（重要度つき）の除去
# ---------------------------------------------------------------------------
#
# `script_generator._build_prompt` はニュース候補を
# `1. 社会 - 日本の重要な出来事 (重要度: 10)` という行番号つきの機械的な行で
# 生成する。Gemini がその行をそのまま流用すると原稿の末尾に「重要度」が
# 読み上げられるため、TTS 前に落とす必要がある。
#
# 一方で番号付きの**本文の列挙**は残す（このファイルの 2 テスト目がそれ）。
# この 2 つを区別できるのは「`番号. カテゴリ - 見出し（重要度: N）`」という
# 形だけが重要度の情報を持つため。


def test_clean_script_removes_numbered_news_candidate_lines():
    """重要度つきのニュース候補行を除去すること。"""
    text = (
        "### T1\n"
        "1. 社会 - 日本の重要な出来事 (重要度: 10)\n"
        "2. 経済 - 石油危機の直撃 (重要度: 8)\n"
        "おわり。"
    )
    result = clean_script_for_tts(text)
    assert "重要度" not in result, result
    assert "日本の重要な出来事" not in result
    assert "石油危機の直撃" not in result
    assert "おわり。" in result


def test_clean_script_keeps_a_numbered_line_merely_mentioning_importance():
    """「重要度」が出ても形が違う行は本文として残すこと。

    `1. 1975年の流行、年末の売上げ (重要度: 10)` のように
    番号 + 箇条書きの形まで落入ると、
    Round 1 で直した「本文を丸ごと消す」問題が再発する。
    """
    text = "### T1\n1. 1975年の流行、年末の売上げ (重要度: 10)\nおわり。"
    result = clean_script_for_tts(text)
    assert "1975年の流行" in result, result
    assert "おわり。" in result
