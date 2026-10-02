"""事実レジストリの回帰テスト（提案⑦ / plans/evidence_based_improvement_proposals.md 324-390 行）。

ここで固定しているのは「介護施設・デイサービス回想法で読み上げる原稿が
事実として壊れないこと」。既存の ``tests/test_content_regression.py`` が
2025 年 4 月時点の正本の上に立っているため、本ファイルは**新しい正本**
（``retro_radio/core/facts/*.json``）を導入したものであり、
既存の回帰テストが新しい正本に対して誤りでないことも保証する。

1. 確定的な事実誤認 4 件（ザ・ヒットパレード / ノイタミナA / Sportacent /
   歌謡パレード）と内部矛盾 1 件（8時だョ!全員集合）の是正。
2. ``_mentions_future_year`` の「○年代」穴（現状コードなら落ちる）。
3. 全 76 年 × 3 モードで ``scripts/validate_facts.validate_all()`` が fail 0。
4. 全レコードに ``source`` があること。

外部ネットワークは使わない。
"""

import re

import pytest

from retro_radio.core.facts import (
    ALLOWED_CONFIDENCE,
    ALLOWED_KINDS,
    REQUIRED_FIELDS,
    facts_valid_for,
    future_year_mentions,
    load_facts,
    programs_for_year,
    radio_programs_for_year,
    resolve_program,
)
from retro_radio.core.fallback import (
    RADIO_PROGRAMS_BY_DECADE,
    HistoricalRadioPrograms,
    _mentions_future_year,
    generate_care_script,
)
from retro_radio.models.radio import ProgramSchedule

from scripts.validate_facts import (
    ALL_YEARS,
    END_YEAR,
    START_YEAR,
    Issue,
    build_fact_table,
    issues_by_level,
    validate_all,
)

RECORDS = load_facts()
RECORDS_BY_ID = {r["id"]: r for r in RECORDS}


def _titles_for(year):
    return {r["title"] for r in programs_for_year(year)}


# ---------------------------------------------------------------------------
# 1. 誤認 4 件の是正（+ 内部矛盾 1 件）
# ---------------------------------------------------------------------------
def test_hit_parade_is_the_fuji_1959_program_not_a_1980s_tbs_show():
    """誤認1: ザ・ヒットパレードは 1959〜1970 / フジテレビ / 30 分

    修正前は「1980年代にTBSで始まった」と書かれていたため、1980 年台の
    利用者に「その頃見ていた番組」として提示されていた。
    """
    record = RECORDS_BY_ID["tv_hit_parade_1959"]
    assert record["title"] == "ザ・ヒットパレード"
    assert record["network"] == "フジテレビ"
    assert record["valid_from"] == 1959
    assert record["valid_to"] == 1970
    assert record["duration_min"] == 30
    # 1980 年代には放送されていない。
    assert "ザ・ヒットパレード" not in _titles_for(1985)
    assert "ザ・ヒットパレード" not in _titles_for(1990)


def test_hit_parade_never_appears_in_1980s_and_1990s_scripts():
    """誤認1の回帰防止: 1980/1990 年台の介護原稿に出ないこと"""
    for year in (1980, 1985, 1990, 1995):
        script = generate_care_script(year, 5, 15)
        assert "ザ・ヒットパレード" not in script, year


@pytest.mark.parametrize("year", [1959, 1962, 1965, 1968, 1970])
def test_hit_parade_is_available_in_its_real_span(year):
    """誤認1: 実際に放送されていた年には提示されること"""
    assert "ザ・ヒットパレード" in _titles_for(year), year


def test_noitamina_a_starts_in_2005_not_2001():
    """誤認2: ノイタミナA は 2005 年開始（1990年代末〜2000年代ではない）

    修正前は 2000 バケットの 2 件でローテーションしていたため、
    2001 / 2003 / 2005 / 2007 / 2009 に必ず「存在しない番組」が提示されていた。
    """
    record = RECORDS_BY_ID["noitamina_a_2005"]
    assert record["valid_from"] == 2005
    assert record["valid_to"] == 2014

    for year in (2001, 2002, 2003, 2004):
        assert "ノイタミナA" not in _titles_for(year), year
    for year in (2005, 2009, 2014):
        assert "ノイタミナA" in _titles_for(year), year


def test_noitamina_a_is_absent_from_the_2000s_guide():
    """誤認2: 2000 年台の番組表にノイタミナA が出ないこと"""
    for year in (2000, 2001, 2002, 2003, 2004):
        guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
        titles = [s.title for s in guide.schedules]
        assert "ノイタミナA" not in titles, (year, titles)


def test_sportacent_is_a_single_record_with_one_span():
    """誤認3: Sportacent は単一レコードに集約されている

    修正前は 2010 バケット（"2020年代まで放送が続きました"）と
    2020 バケット（"2020年代も放送されました"）の 2 箇所に互いに矛盾する
    記述があった。
    """
    sportacent = [r for r in RECORDS if r["title"] == "Sportacent"]
    assert len(sportacent) == 1, "Sportacent は 1 事実 1 レコードであること"
    record = sportacent[0]
    assert record["network"] == "NHK"
    assert record["valid_from"] == 2004
    # 終了年が未確定なので null（= 継続中）に 1 箇所で集約する。
    assert record["valid_to"] is None
    # 矛盾する 2 つの記述があちこちに散らばっていないこと。
    blob = " ".join(
        s.description or "" for bucket in RADIO_PROGRAMS_BY_DECADE.values() for s in bucket
    )
    assert "2020年代まで放送が続きました" not in blob
    assert "2020年代も放送されました" not in blob


def test_uta_parade_is_not_labeled_as_a_1980s_program():
    """誤認4: 歌謡パレードは 1977 年開始（放送時刻 19:30 と一致する）

    修正前は「1980年代にNETテレビで始まった」と書かれていたが、
    同じレコードの放送時刻 19:30 が示す 1977 年と矛盾していた。
    """
    record = RECORDS_BY_ID["uta_parade_1977"]
    assert record["valid_from"] == 1977
    assert record["start_time"] == "19:30"
    assert record["network"] == "NETテレビ"

    for schedule in RADIO_PROGRAMS_BY_DECADE[1980]:
        if schedule.title == "歌謡パレード":
            assert "1980年代に" not in (schedule.description or "")


def test_hachiji_dayo_no_longer_contradicts_its_own_start_time():
    """誤認5: 8時だョ!全員集合の「深夜」記述と start_time="20:00" の矛盾を解消

    20:00 は深夜ではない。修正前は同じレコード内で自己矛盾していた。
    """
    record = RECORDS_BY_ID["hachiji_dayo_1968"]
    assert record["start_time"] == "20:00"
    blob = f"{record['claim_ja']} {record['description_ja']}"
    assert "深夜" not in blob
    assert "夜8時" in blob


def test_1950s_bucket_no_longer_claims_a_program_that_started_in_1959():
    """構造の是正: NHK ラジオ第一の開始年を 1925 に直した（1950年代ではない）"""
    record = RECORDS_BY_ID["nhk_radio_first_1925"]
    assert record["valid_from"] == 1925
    assert "1950年代に始まった" not in record["description_ja"]


# ---------------------------------------------------------------------------
# 2. _mentions_future_year の「○年代」穴
# ---------------------------------------------------------------------------
def _schedule(description: str, title: str = "テスト番組") -> ProgramSchedule:
    return ProgramSchedule(
        id="test_1",
        title=title,
        start_time="19:00",
        duration=30,
        description=description,
        is_historical=True,
    )


def test_mentions_future_year_catches_a_future_decade_expression():
    """回帰防止: year=2015 のとき「2020年代」は violation

    旧実装（``_YEAR_IN_TEXT`` だけ）は「2020年代」を 4 桁の西暦として
    拾うため **このケースは通ってしまう**。年代表記を**年代バケットの
    閾値**として独立に評価していることを明示的に固定する。
    """
    schedule = _schedule("2020年代に始まった番組")
    assert _mentions_future_year(schedule, 2015) is True


@pytest.mark.parametrize(
    "year,description,expected",
    [
        # 4 桁の西暦（従来から捕まっていた）
        (2015, "2018年に始まった", True),
        (2018, "2018年に始まった", False),
        # 「○年代」表記（今回の拡張。閾値 = その年代の先頭年）
        (2015, "2020年代の話", True),
        (2019, "2020年代の話", True),
        (2020, "2020年代の話", False),
        (2025, "2020年代の話", False),
        # 過去の年代は違反ではない
        (1975, "1960年代の話", False),
        (1975, "1970年代の話", False),
        # スペースを含む表記
        (2015, "2020 年代の話", True),
        # 対象年そのものは違反ではない
        (1970, "1970年のことです", False),
    ],
)
def test_mentions_future_year_threshold_for_both_forms(year, description, expected):
    """4 桁の西暦と「○年代」の両方で、年代バケットの閾値として比較する"""
    assert _mentions_future_year(_schedule(description), year) is expected


def test_future_year_mentions_reports_both_forms():
    """公開 API: 見つかった言及を文字列で返す（warn の内訳に使う）"""
    mentions = future_year_mentions("1990年代と2020年代、2050年", 2015)
    assert "2020年代" in mentions
    assert "2050年" in mentions
    assert not future_year_mentions("1990年代と2000年", 2015)


def test_guide_never_shows_a_future_decade_expression():
    """回帰防止: 全 76 年の番組表に未来年の年代表記が出ないこと"""
    for year in ALL_YEARS:
        guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
        for schedule in guide.schedules:
            assert not future_year_mentions(
                f"{schedule.title} {schedule.description or ''}", year
            ), (year, schedule.title)


def test_year_in_text_regex_is_still_the_four_digit_pattern():
    """旧来の 4 桁西暦パターンを公開モジュールから参照できることの固定"""
    # ``future_year_mentions`` は 4 桁西暦を依然として検出する。
    assert future_year_mentions("西暦2019年のこと", 2015) == ["2019年"]


# ---------------------------------------------------------------------------
# 3. バケット解決の不整合是正（2025 キー / resolver 統一）
# ---------------------------------------------------------------------------
def test_radio_programs_by_decade_has_a_2025_key():
    """RADIO_PROGRAMS_BY_DECADE に 2025 キーが存在する（以前は欠落）"""
    assert 2025 in RADIO_PROGRAMS_BY_DECADE
    assert RADIO_PROGRAMS_BY_DECADE[2025], "2025 バケットが空では番組表が空になる"


@pytest.mark.parametrize("year", [2020, 2021, 2022, 2023, 2024, 2025])
def test_2020s_years_vary_their_historical_program(year):
    """誤認8: 2020 バケット 1 件で全年に同じ番組を出す状態を解消

    以前は 2020 バケットが 1 件だけなので ``year % 1 == 0`` となり、
    2020〜2025 年すべてに Sportacent が出ていた。
    """
    picks = {
        HistoricalRadioPrograms.get_program_guide(y, 5, 15).schedules[0].id
        for y in (2020, 2021, 2022, 2023, 2024, 2025)
    }
    assert len(picks) > 1, "全年に同じ番組を出す状態が解消されていない"


@pytest.mark.parametrize("year", ALL_YEARS)
def test_resolver_never_returns_a_program_outside_its_span(year):
    """resolver は ``valid_from <= year <= valid_to`` を満たすものだけを返す"""
    record = resolve_program(year)
    assert record is not None, year
    valid_to = record["valid_to"]
    assert record["valid_from"] <= year
    assert valid_to is None or year <= valid_to


@pytest.mark.parametrize("year", ALL_YEARS)
def test_guide_history_is_valid_for_the_requested_year(year):
    """番組表に出る歴史番組は、その年に放送されていた番組だけであること"""
    valid_titles = _titles_for(year)
    guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
    historical = [s for s in guide.schedules if s.is_historical]
    assert historical, year
    for schedule in historical:
        assert schedule.title in valid_titles, (year, schedule.title)


def test_resolver_is_deterministic():
    """同じ年なら常に同じ記録を返す（冪等・決定性）"""
    for year in (1950, 1975, 2005, 2025):
        first = resolve_program(year)
        for _ in range(5):
            assert resolve_program(year)["id"] == first["id"]


def test_resolver_matches_the_historical_pick_contract():
    """``resolve_program`` は ``_historical_pick`` と同じ結果を返す"""
    for year in (1950, 1975, 2005, 2025):
        picked = HistoricalRadioPrograms._historical_pick(year)
        assert picked is not None, year
        assert picked.id == resolve_program(year)["id"], year


# ---------------------------------------------------------------------------
# 4. source / スキーマの必須化
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_every_record_has_a_source(record):
    """``source`` は全レコードに存在する（欠落は CI ゲートで fail）"""
    assert record.get("source"), record["id"]
    assert str(record["source"]).strip(), record["id"]


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_every_record_has_every_required_field(record):
    """必須フィールドがすべて埋まっている（``valid_to`` だけは null 可）"""
    for field in REQUIRED_FIELDS:
        if field == "valid_to":
            assert record.get(field) is None or isinstance(record[field], int), record["id"]
            continue
        assert record.get(field) not in (None, ""), (record["id"], field)


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_every_record_declares_a_valid_kind_and_confidence(record):
    """``kind`` / ``confidence`` は許可値のみ"""
    assert record["kind"] in ALLOWED_KINDS, record["id"]
    assert record["confidence"] in ALLOWED_CONFIDENCE, record["id"]


def test_no_fabricated_source_urls():
    """``source_url`` は本タスクがネットワークアクセスできないため全て null

    URL を推測で書くと「出典があるように見える」が実際は捏造になる。
    本提案の主題がそれなので、正本には URL を置かない。
    """
    for record in RECORDS:
        assert record.get("source_url") is None, record["id"]


def test_unverified_records_declare_a_note():
    """``unverified`` のレコードには必ず ``note_ja``（要確認の申し送り）がある"""
    for record in RECORDS:
        if record["confidence"] == "unverified":
            assert record.get("note_ja"), record["id"]


def test_record_ids_are_unique():
    """1 事実 1 レコード: id は重複しない"""
    ids = [r["id"] for r in RECORDS]
    assert len(ids) == len(set(ids))


def test_registry_is_sorted_by_valid_from():
    """並び順は ``valid_from`` 昇順（決定性の前提条件）"""
    years = [r["valid_from"] for r in RECORDS]
    assert years == sorted(years), years


# ---------------------------------------------------------------------------
# 5. validator: 全 76 年 × 3 モードで fail 0
# ---------------------------------------------------------------------------
def test_validate_all_reports_no_failures():
    """CI ゲート（提案⑦ の効果指標: fact validator の失敗数 = 0）"""
    issues = validate_all()
    failures = [i for i in issues if i.level == "fail"]
    assert failures == [], [str(i) for i in failures]


def test_validate_all_covers_all_76_years_and_3_modes():
    """走査範囲が「全 76 年 × 3 モード」であること"""
    assert len(ALL_YEARS) == 76
    assert (START_YEAR, END_YEAR) == (1950, 2025)

    from scripts.validate_facts import SCRIPT_BUILDERS

    assert set(SCRIPT_BUILDERS) == {"normal", "care_recreation", "anniversary"}


def test_validate_all_warns_about_unverified_records():
    """``confidence: "unverified"`` は warn で可視化される（fail ではない）"""
    issues = validate_all()
    unverified = [i for i in issues if i.code == "unverified"]
    assert len(unverified) == len(RECORDS), "全レコードが未照合として警告される"


def test_validate_all_is_pure():
    """``validate_all()`` は副作用がなく、呼び出しごとに同じ結果"""
    first = validate_all()
    second = validate_all()
    assert [str(i) for i in first] == [str(i) for i in second]


def test_issue_exposes_level_code_and_where():
    """``Issue`` は S2 から機械的に扱える形をしている"""
    issues = validate_all()
    assert issues, "検査結果が空ではバグなのでNG"
    for issue in issues:
        assert isinstance(issue, Issue)
        assert issue.level in ("fail", "warn")
        assert issue.code
        assert issue.message
        assert isinstance(issue.where, str)


def test_issues_by_level_splits_fail_and_warn():
    """``issues_by_level`` は fail / warn に分ける（S2 の fact score 用の API）"""
    grouped = issues_by_level(validate_all())
    assert set(grouped) == {"fail", "warn"}
    assert grouped["fail"] == []
    assert grouped["warn"], "未照合レコードの警告は必ず出る"


def test_build_fact_table_returns_only_valid_records():
    """``build_fact_table(year)`` はその年に有効な事実だけを返す（S2 用の API）"""
    for year in (1950, 1975, 2005, 2025):
        table = build_fact_table(year)
        assert table, year
        for record in table:
            valid_to = record["valid_to"]
            assert record["valid_from"] <= year
            assert valid_to is None or year <= valid_to, (year, record["id"])


def test_facts_valid_for_matches_programs_for_year_shape():
    """``facts_valid_for`` は ``programs_for_year`` の上位集合であること"""
    for year in (1950, 1980, 2015):
        program_ids = {r["id"] for r in programs_for_year(year)}
        all_ids = {r["id"] for r in facts_valid_for(year)}
        assert program_ids <= all_ids


def test_four_digit_year_pattern_is_shared_between_core_and_validator():
    """4 桁西暦の検出は facts モジュールと validator で二重実装しない"""
    # ``scripts/validate_facts`` が使うのは facts の 1 実装そのもの。
    from scripts import validate_facts as vf

    assert vf.future_year_mentions is future_year_mentions


# ---------------------------------------------------------------------------
# 6. 訂正プロトコルの前提: 誤りは最初から載せない
# ---------------------------------------------------------------------------
def test_description_ja_never_contains_a_bare_four_digit_year():
    """読み上げ文言 ``description_ja`` に「単独の」4 桁西暦を書かない

    ``description_ja`` は「1950年代に…」のような**年代バケット表記**を
    使うのは正しい（対象年を超えないため）。問題になるのは
    「1975年に始まった」のような**単独の 4 桁西暦**で、これは
    ``_mentions_future_year`` が除外する原因になり、対象年によっては
    番組全体が番組表から消える。
    """
    bare_year = re.compile(r"(1[5-9]\d{2}|20\d{2})(?!\s*年代)")
    for record in RECORDS:
        description = record.get("description_ja") or ""
        assert not bare_year.search(description), (record["id"], description)


@pytest.mark.parametrize("record", RECORDS, ids=lambda r: r["id"])
def test_description_ja_never_names_a_future_decade_of_its_own_span(record):
    """``description_ja`` の年代表記は、そのレコードの放送期間の外に出ない

    例: 1977 年開始の番組の ``description_ja`` に「1980年代」と書くのは、
    1977〜1979 年の鑑賞者には存在しない番組を語ることになる。
    """
    valid_from = record["valid_from"]
    valid_to = record["valid_to"]
    hi = valid_to if isinstance(valid_to, int) else END_YEAR

    for mention in future_year_mentions(record.get("description_ja") or "", valid_from):
        assert False, (record["id"], mention, valid_from, hi)

    # 期間中のどの年に対しても violation にならないこと。
    for year in range(valid_from, min(hi, END_YEAR) + 1):
        assert not future_year_mentions(record.get("description_ja") or "", year), (
            record["id"],
            year,
        )


def test_every_covered_year_has_at_least_one_valid_program():
    """全 76 年に 1 本以上の有効な歴史番組がある（空の番組表を出さない）"""
    for year in ALL_YEARS:
        assert programs_for_year(year), year


# ---------------------------------------------------------------------------
# 7. R2-11 コア: ラジオ台本に**テレビ番組**が出ない
# ---------------------------------------------------------------------------
def test_radio_programs_for_year_returns_radio_kind_only():
    """``radio_programs_for_year`` は ``radio_program`` だけを返す"""
    for year in ALL_YEARS:
        kinds = {r["kind"] for r in radio_programs_for_year(year)}
        assert kinds <= {"radio_program"}, (year, kinds)


def test_radio_programs_for_year_is_a_subset_of_programs_for_year():
    """ラジオ用は番組表用の部分集合（絞り込みでPromotion してない）"""
    for year in ALL_YEARS:
        radio_ids = {r["id"] for r in radio_programs_for_year(year)}
        program_ids = {r["id"] for r in programs_for_year(year)}
        assert radio_ids <= program_ids, year


@pytest.mark.parametrize(
    "year,tv_title",
    [
        (1975, "料理教室"),          # NHK教育テレビ / tv_program
        (1965, "ザ・ヒットパレード"),  # フジテレビ / tv_program
        (1985, "JAPAN COUNTDOWN"),  # 日本テレビ / tv_program
    ],
)
def test_radio_script_never_names_a_television_program(year, tv_title):
    """ラジオの読み上げ原稿にテレビ番組名が出ない（R2-11 コア）

    ``programs_for_year`` は番組表用に ``tv_program`` も含むため、
    ラジオ台本の埋め込みに使うと「料理教室」（テレビ）が
    ラジオの番組として読み上げられていた。回想の文脈では時代錯誤になる。
    """
    from retro_radio.core import fallback

    sentence = fallback._program_sentence(year, limit=3)
    assert tv_title not in sentence, (year, tv_title, sentence)
