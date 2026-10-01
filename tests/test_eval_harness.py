"""評価ハーネス（``eval/``）自体のテスト（提案⑨ / サブエージェント S2）。

このファイルの役割は **ハーネスの妥当性**を固定することである。
「指標が通った」だけでは意味がない。**何を壊せば落ちるか**を示せることが
このテストの要求である。したがって各テストは次の形を取る:

1. **レジストリ由来の台本**で高得点になる。
2. **同じ構造のまま壊した台本**（他年の番組名・架空の番組名・未来年）で
   低得点になる。
3. CheckList の **8 項目それぞれ**について、1 項目だけ壊した文字列で
   その項目だけ違反になる。
4. 長さが**帯の両端**で落ち、帯の内側では通る。

外部ネットワークは使わない（API キーは conftest で空に固定されている）。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.cases import load_case, load_cases
from eval.cases.build_cases import MODES, SAMPLED_YEARS, build_all, expected_segments, render
from eval.cases.loader import CASE_DEFS_PATH
from eval.metrics import CaseResult, EvalReport, run_all, run_case
from eval.metrics.checklist import (
    CHECKLIST_ITEMS,
    check_era_words,
    check_heading_order,
    check_japanese_ratio,
    check_preannounce,
    check_prompt_leftover,
    check_required_segments,
    check_song_duplication,
    check_song_match,
    extract_headings,
    japanese_char_ratio,
    run_checklist,
)
from eval.metrics.fact_score import (
    KIND_FABRICATED,
    KIND_PROGRAM,
    extract_atomic_claims,
    fact_score,
    is_song_reference,
    split_sentences,
)
from eval.metrics.length import MEASURED, check_length, length_bounds, percentile
from eval.metrics.preannounce import detect_unfulfilled_preannounce
from eval.metrics.songs import extract_song_mentions, song_match_rate
from retro_radio.core.facts import load_facts, programs_for_year
from retro_radio.core.fallback import (
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
)
from scripts.validate_facts import build_fact_table

# ---------------------------------------------------------------------------
# 共通データ（ハードコードは「正本から機械的に引くもの」だけに限定する）
# ---------------------------------------------------------------------------
#: 1975 年に実在する番組（レジストリから機械的に取る）
VALID_1975_TITLE = programs_for_year(1975)[0]["title"]

#: 1975 年に**放送されていない**が、レジストリには載っている番組
INVALID_1975_TITLE = next(
    record["title"]
    for record in load_facts()
    if record["title"] not in {r["title"] for r in programs_for_year(1975)}
)

#: 静的マスターに載っている曲（曲名一致率のテストに使う）
KNOWN_SONG_TITLE = "上を向いて歩こう"

#: 構造が正しく、違反 0 件であるべき基準台本
WELL_FORMED = (
    "### オープニング\n"
    "皆様、こんにちは。1975年9月24日の放送です。\n"
    "### トーク1\n"
    f"1970年代には、{VALID_1975_TITLE}という番組がありました。\n"
    "### エンディング\n"
    "本日の放送はお開きでございます。\n"
)


# ---------------------------------------------------------------------------
# 1. ケース定義（24 ケース・正本から生成されていること）
# ---------------------------------------------------------------------------
def test_case_definitions_match_the_committed_generated_artifact():
    """``case_defs.json`` が正本と乖離していないこと（``--check`` と同等）"""
    committed = Path(CASE_DEFS_PATH).read_text(encoding="utf-8")
    assert render(build_all()) == committed, (
        "eval/cases/case_defs.json が正本と乖離しています。"
        "`python -m eval.cases.build_cases` を実行してください。"
    )


def test_case_definitions_cover_24_stratified_cases():
    """層別抽出 24 ケース（8 年 × 3 モード）であること"""
    cases = load_cases()
    assert len(cases) == 24
    assert sorted({c["year"] for c in cases}) == sorted(SAMPLED_YEARS)
    assert sorted({c["mode"] for c in cases}) == sorted(MODES)
    ids = [c["id"] for c in cases]
    assert len(set(ids)) == 24, ids


def test_expected_facts_come_from_the_registry_not_from_hardcoded_data():
    """``expected_facts`` が ``build_fact_table(year)`` と一致すること

    ハードコードされた正解が 1 つでも混ざると「その年の事実ではないのに
    正解として扱われる」ため、ここは厳密に一致を要求する。
    """
    for case in load_cases():
        expected = [record["id"] for record in build_fact_table(case["year"])]
        assert case["expected_facts"] == expected, case["id"]


def test_expected_segments_are_derived_from_the_prompt():
    """``expected_segments`` が ``_build_segmented_prompt`` と一致すること"""
    case = load_case("care_recreation-1975")
    assert case["expected_segments"] == expected_segments(1975, "care_recreation")
    assert case["expected_segments"][0] == "オープニング"
    assert case["expected_segments"][-1] == "エンディング"


def test_load_case_rejects_an_unknown_id():
    with pytest.raises(KeyError):
        load_case("no-such-case-1234")


# ---------------------------------------------------------------------------
# 2. fact score: レジストリ由来 = 高得点 / 壊したもの = 低得点
# ---------------------------------------------------------------------------
def test_fact_score_is_high_for_a_registry_backed_script():
    """レジストリ由来の介護台本は高得点になる"""
    result = fact_score(generate_care_script(1975, 5, 15), 1975)
    assert result.score == 100.0, result.describe()
    assert not result.failures
    assert result.coverage > 0.0, "介護原稿はその年の番組をいくつか言及している"


def test_fact_score_drops_when_a_registry_program_is_out_of_period():
    """他年の番組を断定するとスコアが下がること（壊した文字列で検証）"""
    good = fact_score(WELL_FORMED, 1975)
    broken = fact_score(WELL_FORMED.replace(VALID_1975_TITLE, INVALID_1975_TITLE), 1975)

    assert good.score == 100.0, good.describe()
    assert broken.score < good.score, broken.describe()
    assert any(claim.kind == KIND_PROGRAM for claim in broken.warnings)
    # warn はゲートを落とさない（S1 の線引き）。
    assert broken.failures == ()
    assert broken.is_gate_ok(threshold=0.0)


def test_fact_score_detects_a_fabricated_program_name():
    """正本に無い番組名の断定は **fail**（ゲートを落とす）"""
    fabricated = WELL_FORMED + "また、「架空の歌謡番組」という未知の番組もありました。\n"
    result = fact_score(fabricated, 1975)
    assert any(c.kind == KIND_FABRICATED for c in result.failures), result.describe()
    assert not result.is_gate_ok(), "捏造はゲートを落とすはず"


def test_fact_score_detects_a_future_year_as_a_warning():
    """対象年より後の年の言及は warn（S1 と同じ線引き・ゲートは落とさない）"""
    future = fact_score(WELL_FORMED + "2020年代には新しいテレビ番組が始まりました。\n", 1975)
    assert future.warnings, future.describe()
    assert not future.failures
    assert future.is_gate_ok(threshold=0.0), "warn だけでゲートは落とさない"


def test_fact_score_is_zero_when_nothing_verifiable_is_claimed():
    """検証可能な主張が 1 件も無い原稿は 0 点（何も語っていない）"""
    padded = "夕暮れの空の色が美しかった。" * 40
    result = fact_score(padded, 1975)
    assert result.checkable == 0
    assert result.score == 0.0


def test_fact_score_ignores_atmosphere_in_the_denominator():
    """情景描写は分母に入らない（水増しを fact score で罰しない）"""
    short = "1975年という年でした。\n"
    long_atmosphere = short + ("夕暮れの空の色が美しかった。\n" * 200)
    assert fact_score(short, 1975).score == fact_score(long_atmosphere, 1975).score


def test_split_sentences_drops_headings_and_empty_lines():
    assert split_sentences("### オープニング\n本文です。\n\n以上。\n") == ["本文です", "以上"]


def test_extract_atomic_claims_accepts_an_explicit_source():
    """``source_titles`` を渡すと、その集合を正解として判定する（S3 と同じ契約）"""
    claims = extract_atomic_claims(WELL_FORMED, 1975, source_titles={"別の番組"})
    program_claims = [c for c in claims if c.kind == KIND_PROGRAM]
    assert program_claims
    assert all(c.level == "warn" for c in program_claims)


# ---------------------------------------------------------------------------
# 3. CheckList: 8 項目それぞれを「壊した文字列」で検証
# ---------------------------------------------------------------------------
def test_checklist_has_the_eight_declared_items():
    """最低 7 項目（実際は 8: a〜g に加えて S2 が h を追加した）"""
    assert len(CHECKLIST_ITEMS) >= 7
    assert set(CHECKLIST_ITEMS) == {
        "heading_order",
        "required_segments",
        "song_match",
        "era_words",
        "prompt_leftover",
        "japanese_ratio",
        "song_duplication",
        "unfulfilled_preannounce",
    }


def test_well_formed_script_has_no_checklist_violation():
    """基準となる正しい台本は違反 0 件であること"""
    result = run_checklist(WELL_FORMED, 1975)
    assert result.passed, [str(v) for v in result.violations]


def test_item_a_heading_order_catches_a_swapped_order():
    """(a) 見出し順の反転を捕まえる"""
    swapped = (
        WELL_FORMED.replace("### オープニング", "### 仮")
        .replace("### エンディング", "### オープニング")
        .replace("### 仮", "### エンディング")
    )
    problems = check_heading_order(swapped)
    assert problems, swapped
    assert not check_heading_order(WELL_FORMED)


def test_item_a_heading_order_catches_missing_headings():
    """(a) 見出しが 1 つも無い場合も違反にする"""
    assert check_heading_order("本文だけの原稿です。\n") == ["見出し（### ）が 1 つもありません"]

    # 見出しがあっても必須セグメントが無ければ違反になる。
    problems = check_heading_order("### トーク1\n本文\n")
    assert any("### オープニング がありません" in p for p in problems), problems
    assert any("### エンディング がありません" in p for p in problems), problems


def test_item_a_heading_order_catches_a_middle_segment():
    """(a) エンディングが末尾でない場合も捕まえる"""
    broken = WELL_FORMED + "### 書き足し\nおわり\n"
    problems = check_heading_order(broken)
    assert any("末尾ではありません" in p for p in problems), problems


def test_item_b_required_segments_catches_a_missing_one():
    """(b) 必須セグメントの欠落を捕まえる"""
    assert not check_required_segments(WELL_FORMED)
    assert check_required_segments("### オープニング\n本文\n") == [
        "必須セグメントがありません: ### エンディング"
    ]


def test_item_c_song_match_catches_a_song_outside_the_allowed_set():
    """(c) 選曲リストにない曲名を捕まえる（S3 へ渡す API の検証）"""
    script = "### オープニング\n懐かしい名曲「存在しない曲」（架空のアーティスト）をお届けします。\n"
    result, problems = check_song_match(script, 1975, allowed_titles={"実在する曲"})
    assert problems, result.describe()
    assert result.rate == 0.0
    assert result.unmatched[0].title == "存在しない曲"

    # 照合先に含めれば通る（片側に依存しないことの検証）
    ok, no_problems = check_song_match(script, 1975, allowed_titles={"存在しない曲"})
    assert not no_problems
    assert ok.rate == 1.0


def test_item_c_song_match_does_not_fire_on_quiz_text():
    """(c) クイズのヒントを曲名と誤検出しない（誤検出の固定）

    ここを緩めると ``care_recreation`` の全 8 ケースが偽陽性になり、
    正しい原稿を「不一致」と言い張ってしまう。
    """
    script = (
        "### オープニング\n"
        "あの頃のヒット曲を思い出すヒントをひとつ。「1970年代に放送局の擬似体験番組が始まりました。」\n"
    )
    result, problems = check_song_match(script, 1975)
    assert not problems, [m.title for m in result.unmatched]
    assert result.total == 0


def test_item_c_song_match_is_neutral_about_the_year():
    """(c) ``year`` は照合に使わない（片側に依存しない API の約束）"""
    script = f"### オープニング\n懐かしい名曲「{KNOWN_SONG_TITLE}」（坂本九）をお届けします。\n"
    a = song_match_rate(script, 1975)
    b = song_match_rate(script, 2025)
    assert a.rate == b.rate == 1.0
    assert a.source == b.source == "static-master"


def test_item_d_era_words_catches_a_future_year_and_a_future_decade():
    """(d) 4 桁西暦と「○年代」の両方を捕まえる（S1 の関数を再利用）"""
    assert check_era_words("1975年の放送です。\n", 1975) == []
    assert any("2020年" in p for p in check_era_words("2020年に始まった番組。\n", 1975))
    assert any("2020年代" in p for p in check_era_words("2020年代の流行。\n", 1975))


@pytest.mark.parametrize(
    "line",
    [
        "以下のセグメント構成で原稿を書いてください",
        "あなたは昭和・平成のレトロなラジオパーソナリティです。",
        "Lag: 1200 tokens",
        "レスポンス: Optimizer",
        "条件:",
        "<system>",
    ],
)
def test_item_e_prompt_leftover_catches_api_response_labels(line):
    """(e) プロンプト残骸を捕まえる"""
    problems = check_prompt_leftover(WELL_FORMED + line + "\n")
    assert problems, line


def test_item_e_does_not_fire_on_a_clean_script():
    """(e) 正しい台本では検出しない"""
    assert not check_prompt_leftover(WELL_FORMED)


def test_item_e_does_not_break_text_cleaner():
    """(e) 検出は ``clean_script_for_tts`` の努力を壊さない（補完関係である）"""
    from retro_radio.utils.text_cleaner import clean_script_for_tts

    polluted = WELL_FORMED + "Lag: 1200\n"
    assert check_prompt_leftover(polluted)   # 検出できる
    assert VALID_1975_TITLE in clean_script_for_tts(polluted)  # cleaner は本文を残す


def test_item_f_japanese_ratio_catches_an_english_script():
    """(f) 英語混じりの原稿を捕まえる"""
    assert japanese_char_ratio("これはAcceleratorのテストです。") < 0.9
    ratio, problems = check_japanese_ratio(
        "This is an entirely English script without any Japanese words."
    )
    assert ratio == 0.0
    assert problems
    assert not check_japanese_ratio(WELL_FORMED)[1]


def test_item_f_ignores_latin_letters_inside_proper_nouns():
    """(f) 固有名の中のラテン文字は「混入」と数えない（誤検出の固定）"""
    script = (
        "### オープニング\n"
        "懐かしい名曲「LA・LA・LA・LA LOVE SONG」（サザンオールスターズ）をお届けします。\n"
    )
    ratio, problems = check_japanese_ratio(script)
    assert not problems, f"ratio={ratio}"
    assert ratio == 1.0


def test_item_g_song_duplication_catches_the_same_song_twice():
    """(g) 同一曲が 2 回出たら捕まえる"""
    line = f"懐かしい名曲「{KNOWN_SONG_TITLE}」（坂本九）をお届けします。\n"
    problems = check_song_duplication(line + line)
    assert problems and KNOWN_SONG_TITLE in problems[0]
    assert not check_song_duplication(line)


def test_item_h_unfulfilled_preannounce_catches_a_promise_without_delivery():
    """(h) 予告したのに配信しない原稿を捕まえる"""
    broken = (
        "### オープニング\n"
        "本章では、1975年のニュースを三つほどご用意しました。\n"
        "### トーク1\n"
        "夕暮れの空の色が美しかった。夕暮れの空の色が美しかった。\n"
        "### エンディング\n"
        "おわりました。\n"
    )
    findings = detect_unfulfilled_preannounce(broken)
    assert findings, broken
    assert any("予告 3 件" in item for item in findings), findings
    assert check_preannounce(broken)


def test_item_h_accepts_a_promise_that_is_delivered():
    """(h) 予告どおり配信されている原稿は違反にしない（誤検出の固定）"""
    fulfilled = (
        "### オープニング\n"
        "本章では、1975年のニュースを三つほどご用意しました。\n"
        "### トーク1\n"
        "ひとつめ、「携带電話」が登場。\n"
        "ふたつめ、「青色申告」の制度が開始。\n"
        "みっつめ、「土曜日イベント」の放送が始まりました。\n"
        "### エンディング\n"
        "おわりました。\n"
    )
    assert detect_unfulfilled_preannounce(fulfilled) == []


def test_item_h_catches_a_standalone_preannounce():
    """(h) 予告文が単独で終端する形も捕まえる"""
    standalone = "### オープニング\n三つほどご用意しました。\n"
    findings = detect_unfulfilled_preannounce(standalone)
    assert findings, standalone


def test_run_checklist_reports_every_violated_item_at_once():
    """複数の違反を 1 回の走査でまとめて報告する"""
    broken = (
        "### エンディング\n"
        "2020年の番組を三点ご用意しました。\n"
        "This is an entirely English sentence with no Japanese at all.\n"
    )
    result = run_checklist(broken, 1975)
    violated = set(result.items_violated)
    assert {"heading_order", "required_segments", "era_words", "japanese_ratio"} <= violated
    assert not result.passed
    assert "CheckList 違反" in result.describe()


def test_extract_headings_returns_titles_in_order():
    assert extract_headings(WELL_FORMED) == ["オープニング", "トーク1", "エンディング"]


def test_is_song_reference_distinguishes_cue_lines_from_keywords():
    from eval.metrics.fact_score import _QUOTED

    cue = f"懐かしい名曲「{KNOWN_SONG_TITLE}」（歌手）をお届けします。"
    keyword = "あの頃のヒット曲を思い出すヒントをひとつ。「1970年代に放送が始まりました。」"
    assert is_song_reference(cue, _QUOTED.search(cue))
    assert not is_song_reference(keyword, _QUOTED.search(keyword))


# ---------------------------------------------------------------------------
# 4. 長さ: 帯の両端で落ちる
# ---------------------------------------------------------------------------
def test_length_band_is_derived_from_settings():
    """帯が設定から導出されていること（ハードコードしていない）"""
    from retro_radio.config import get_settings

    settings = get_settings()
    lower, upper = length_bounds(settings)
    assert lower == settings.target_script_chars - settings.script_char_tolerance
    assert upper == 1500, "実測 p95=1476 を 50 刻みで切り上げた値"
    assert lower < upper


def test_length_result_falls_out_on_both_ends():
    """帯は**両端**で落ちる（壊した文字列で検証）"""
    lower, upper = length_bounds()

    assert not check_length("あ" * (lower - 1)).ok
    assert check_length("あ" * (lower - 1)).direction == "too_short"

    assert not check_length("あ" * (upper + 1)).ok
    assert check_length("あ" * (upper + 1)).direction == "too_long"

    assert check_length("あ" * lower).ok
    assert check_length("あ" * upper).ok
    assert check_length("あ" * ((lower + upper) // 2)).ok


def test_measured_distribution_brackets_the_band():
    """記録した実測値が採用した帯の内側にあること（レンジの根拠を固定）"""
    lower, upper = length_bounds()
    assert MEASURED["min"] >= lower, "下限は全サンプルの最小値以下であるべき"
    assert MEASURED["max"] <= upper, "上限は全サンプルの最大値以上であるべき"
    assert MEASURED["p5"] < MEASURED["p50"] < MEASURED["p95"]
    assert percentile([1, 2, 3, 4, 5], 0.5) == 3


# ---------------------------------------------------------------------------
# 5. run_case / run_all の集約
# ---------------------------------------------------------------------------
def test_run_case_returns_every_metric():
    result = run_case(load_case("care_recreation-1975"))
    assert isinstance(result, CaseResult)
    assert result.case_id == "care_recreation-1975"
    assert result.year == 1975
    assert result.mode == "care_recreation"
    assert result.fact.score > 0
    assert result.length.ok
    assert result.checklist.song_match is not None
    assert isinstance(result.preannounce, tuple)


def test_run_case_accepts_an_injected_script():
    """``script`` を渡せば生成せずに評価できる（LLM 出力の切り分け用）"""
    case = load_case("care_recreation-1975")
    good = run_case(case, script=generate_care_script(1975, 5, 15))
    bad = run_case(case, script=WELL_FORMED)
    assert good.passed, good.failures
    assert not bad.passed, bad.failures
    assert any("length" in problem for problem in bad.failures)


def test_case_result_serialises_to_json():
    payload = run_case(load_case("anniversary-1975")).to_dict()
    assert json.loads(json.dumps(payload, ensure_ascii=False))["case_id"] == "anniversary-1975"


def test_run_all_covers_all_cases_and_the_preannounce_defect_is_fixed():
    """全 24 ケースを回し、未履行予告の欠陥が解消されたことを確認する

    S2 は当初「normal モード 8 件すべてが ``unfulfilled_preannounce`` で落ちる」
    ことを記録していた（``core/fallback.py`` は S1/S3 所有当时的話）。
    S3（提案②）で ``generate_fallback_script`` が予告どおり 3 件を配るように
    なったため、**このテストは期待値を反転させている**。

    残 Peel iency として失败的ケースがあっても、それは本テストの目的
    （未履行予告の解消）ではないので、違反の内訳だけを報告する。
    """
    report = run_all()
    assert isinstance(report, EvalReport)
    assert len(report.results) == 24
    assert report.mean_fact_score > 0

    still_failing = [
        (r.case_id, r.failures)
        for r in report.results
        if any("unfulfilled_preannounce" in p for p in r.failures)
    ]
    assert still_failing == [], still_failing


def test_run_all_summary_round_trips():
    report = run_all(cases=load_cases()[:2])
    payload = report.to_dict()
    assert payload["summary"]["total"] == 2
    assert len(payload["length_bounds"]) == 2


# ---------------------------------------------------------------------------
# 6. ベースラインの記録（現状のままでも通るものと、落ちているもの）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("year", [1964, 1975, 1985, 1995, 2005, 2015, 2025])
def test_current_care_scripts_score_above_the_threshold(year):
    """介護原稿は現状でも fact score の閾値を満たす（ベースライン）"""
    result = fact_score(generate_care_script(year, 5, 15), year)
    assert result.score >= 80.0, result.describe()
    assert not result.failures


def test_care_1950_is_the_known_low_score_case():
    """``care_recreation/1950`` だけ閾値を下回る既知の欠陥を記録する

    1950 バケットの代表曲 2 件が **1951 年リリース**のため、
    1950 年の原稿に 1951 年の曲が出るため warn が 3 件積み上がる。
    ``core/fallback.py`` は S1/S3 所有なので S2 は編集せず、**検出のみ**を行う。
    このテストは「検出が壊れていない」ことを固定する。
    """
    result = fact_score(generate_care_script(1950, 5, 15), 1950)
    assert result.score < 80.0, result.describe()
    assert not result.is_gate_ok(), "現状は閾値を下回る（ゲートは落ちる）"
    assert len(result.warnings) == 3, result.describe()


def test_anniversary_scripts_score_above_the_threshold():
    result = fact_score(generate_anniversary_script(1985, 5, 15, "花子"), 1985)
    assert result.score >= 80.0, result.describe()


def test_normal_scripts_mention_no_program_at_all():
    """normal モードは番組を 1 つも言及しない（モデルカードに書く不利な truths）"""
    for year in (1950, 1975, 2025):
        assert fact_score(generate_fallback_script(year, 5, 15), year).coverage == 0.0, year


def test_extract_song_mentions_preserves_appearance_order():
    line = f"懐かしい名曲「{KNOWN_SONG_TITLE}」（坂本九）をお届けします。\n"
    mentions = extract_song_mentions(line + line)
    assert [m.title for m in mentions] == [KNOWN_SONG_TITLE, KNOWN_SONG_TITLE]
