"""コアコンテンツの回帰テスト（D4）。

ここで固定しているのは「介護施設・デイサービス回想法で読み上げる原稿が
事実として壊れないこと」。以下はすべて修正前に再現する失敗であり、
修正後は通る。

**提案⑨（S2）による変更**: 「1,000 文字以上」を**文字数の帯**に置き換えた。
内容の指標（fact score / CheckList）は ``eval/`` のハーネスが持ち、
本ファイルは pytest に載る回帰テストだけを置く。ハーネス自体のテストは
``tests/test_eval_harness.py``。

1. ``get_reminiscence_quiz`` が 2011〜2025 を黙って 2000 バケットへ
   マッピングしていた（東日本大震災-router の人に 2000年の悉尼五輪のクイズ）。
2. ``RADIO_PROGRAMS_BY_DECADE`` / ``FALLBACK_SONGS`` の事実誤認
   （勝手にしやがれ=1981年、世界に一つだけの花=モーニング娘。など）。
3. ``get_program_guide`` が未来の年を出し、同じタイトルを2回出していた。
4. ``clean_script_for_tts`` が曲振り台詞を削除していた。
5. ``select_songs`` に重複除去がなかった。

外部ネットワークは使わない（iTunes が必要なテストは必ず ``mock_itunes`` を使う）。
"""

import re

import pytest

from eval.metrics.length import check_length, length_bounds
from eval.metrics.preannounce import detect_unfulfilled_preannounce
from retro_radio.config import get_settings
from retro_radio.core.songs import load_songs
from retro_radio.core.fallback import (
    FALLBACK_SONGS,
    FALLBACK_SONGS_PER_BUCKET,
    FALLBACK_SONG_YEARS,
    REMINISCENCE_DATA,
    HistoricalRadioPrograms,
    generate_anniversary_script,
    generate_care_script,
    generate_fallback_script,
    get_fallback_song,
    get_fallback_songs,
    get_reminiscence_quiz,
)
from retro_radio.core.music_search import select_songs
from retro_radio.utils.text_cleaner import clean_script_for_tts

ALL_YEARS = list(range(1950, 2026))
# タイトル・説明に出現する 4 桁の西暦
YEAR_IN_TEXT = re.compile(r"(1[5-9]\d{2}|20\d{2})")


def _bucket_of(year: int) -> int:
    """年属于自己的年代バケット"""
    return (year // 10) * 10


def _script_builders():
    return {
        "normal": generate_fallback_script,
        "care_recreation": generate_care_script,
        "anniversary": lambda y, m, d: generate_anniversary_script(y, m, d, "花子"),
    }


# --- 1. 回想法クイズの年別バケット正確性（最重要・介護用途） -------------------------
@pytest.mark.parametrize("year", ALL_YEARS)
def test_reminiscence_quiz_belongs_to_the_requested_decade(year):
    """返されるクイズの question が「その年の年代」を名乗っていること"""
    decade = _bucket_of(year)
    quiz = get_reminiscence_quiz(year)
    assert quiz, year
    for item in quiz:
        assert item["question"], (year, item)
        assert f"{decade}年代" in item["question"], (year, item["question"])


@pytest.mark.parametrize("year", range(2011, 2026))
def test_reminiscence_quiz_never_falls_back_to_the_2000s(year):
    """2011〜2025 が 2000 バケットへ黙って落ちるのを防ぐ（東日本大震災の回）"""
    label = f"{_bucket_of(year)}年代"
    questions = [item["question"] for item in get_reminiscence_quiz(year)]
    assert all(label in q for q in questions), (year, questions)
    assert all("2000年代" not in q for q in questions), (year, questions)


def test_reminiscence_quiz_covers_every_decade_up_to_the_present():
    """1950〜2020 のバケットが揃っている（2025 が無い問題の回帰）"""
    expected = [1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020]
    assert sorted(REMINISCENCE_DATA) == expected
    for decade, items in REMINISCENCE_DATA.items():
        assert len(items) >= 3, decade
        for item in items:
            assert set(("question", "answer", "hint")) <= set(item), decade
            assert all(item[k] for k in ("question", "answer", "hint")), (decade, item)


@pytest.mark.parametrize(
    "year,keyword",
    [
        (2011, "東日本大震災"),
        (2011, "iPhone"),
        (2012, "東京スカイツリー"),
        (2020, "コロナ"),
        (2020, "テレワーク"),
        (2020, "キャッシュレス"),
        (2025, "万博"),
    ],
)
def test_reminiscence_quiz_2010s_and_2020s_mention_the_right_events(year, keyword):
    """2010/2020 バケットがその年代の話題を実際に持っていること"""
    questions = " ".join(
        f"{item['question']} {item['answer']} {item['hint']}"
        for item in get_reminiscence_quiz(year)
    )
    assert keyword in questions, (year, keyword)


def test_reminiscence_quiz_2000s_stays_in_the_2000s():
    """2000年代は 2000 バケットのもののまま（既存の保証を維持）"""
    questions = " ".join(item["question"] for item in get_reminiscence_quiz(2005))
    assert "2000年代" in questions
    assert "2010年代" not in questions


def test_reminiscence_quiz_rejects_out_of_range_years():
    """範囲外の年は既定年に寄せる（get_fallback_song と統一）"""
    settings = get_settings()
    default_quiz = get_reminiscence_quiz(settings.default_year)
    assert get_reminiscence_quiz(1800) == default_quiz
    assert get_reminiscence_quiz(3000) == default_quiz


def test_care_script_embeds_quiz_of_the_requested_year():
    """介護原稿に埋め込まれるクイズが対象年の年代に一致すること"""
    for year in (1955, 1975, 1995, 2011, 2015, 2025):
        script = generate_care_script(year, 5, 15)
        assert f"{_bucket_of(year)}年代" in script, year


# --- 2. 番組ガイド: 未来の年を出さない -------------------------------------------
@pytest.mark.parametrize("year", ALL_YEARS)
def test_program_guide_never_mentions_a_future_year(year):
    """タイトル・説明に `year` より後の年を含まないこと"""
    guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
    assert guide.schedules, year
    for schedule in guide.schedules:
        blob = f"{schedule.title} {schedule.description or ''}"
        for token in YEAR_IN_TEXT.findall(blob):
            assert int(token) <= year, (year, schedule.title, blob)


@pytest.mark.parametrize("year", ALL_YEARS)
def test_program_guide_has_no_duplicate_titles(year):
    """同一タイトルが2回現れないこと"""
    guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
    titles = [s.title for s in guide.schedules]
    assert len(set(titles)) == len(titles), (year, titles)


@pytest.mark.parametrize("year", ALL_YEARS)
def test_program_guide_keeps_one_historical_and_one_modern_program(year):
    """歴史番組1本＋現代枠を保ちつつ、提示できる节目が尽きないこと"""
    guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
    historical = [s for s in guide.schedules if s.is_historical]
    modern = [s for s in guide.schedules if not s.is_historical]
    assert len(historical) == 1, (year, [s.title for s in guide.schedules])
    assert modern, year
    for schedule in guide.schedules:
        assert schedule.duration > 0, (year, schedule.title)
        assert schedule.title, year


@pytest.mark.parametrize("year", [1950, 1970, 1982, 1990, 2002, 2014, 2025])
def test_program_guide_reflects_the_requested_year_exactly(year):
    """年依存のタイトルは要求された年を正確に反映する"""
    guide = HistoricalRadioPrograms.get_program_guide(year, 5, 15)
    special = [s for s in guide.schedules if s.title.startswith("特集:")]
    assert special, year
    assert special[0].title == f"特集: {year}年の回想", year


# --- 3. 決定性 -------------------------------------------------------------------
@pytest.mark.parametrize("year", [1950, 1975, 1984, 2011, 2025])
def test_program_guide_is_deterministic(year):
    """同じ入力なら常に同じ出力（冪等・決定性）"""
    first = HistoricalRadioPrograms.get_program_guide(year, 2, 29).to_dict()
    for _ in range(5):
        assert HistoricalRadioPrograms.get_program_guide(year, 2, 29).to_dict() == first


def test_program_guide_handles_impossible_date_without_future_years():
    """実在しない日付でも例外を投げずに「不明」を返す"""
    guide = HistoricalRadioPrograms.get_program_guide(2020, 2, 30)
    assert guide.weekday == "不明"
    assert guide.schedules


# --- 4. FALLBACK_SONGS の事実誤認防止 --------------------------------------------
def test_every_fallback_song_declares_its_release_year():
    """全曲にリリース年のメタデータがある（検証を可能にする）"""
    declared = {s for songs in FALLBACK_SONGS.values() for s in songs}
    assert declared == set(FALLBACK_SONG_YEARS), (
        "メタデータ不足",
        sorted(declared - set(FALLBACK_SONG_YEARS)),
        "メタデータ過多",
        sorted(set(FALLBACK_SONG_YEARS) - declared),
    )


def test_fallback_song_release_year_falls_inside_its_bucket():
    """各曲のリリース年が所属バケットの年代に収まること

    専用キーを持つ年（2025）は「その年付近の曲」を置くバケットなので、
    直前1年分から max_year までを許容する。
    """
    max_year = get_settings().max_year
    for bucket, songs in FALLBACK_SONGS.items():
        for song in songs:
            released = FALLBACK_SONG_YEARS[song]
            if bucket % 10 == 0:
                assert bucket <= released <= bucket + 9, (bucket, song, released)
            else:
                assert bucket - 1 <= released <= max_year, (bucket, song, released)


def test_fallback_song_buckets_have_a_uniform_size():
    """バケットサイズを統一し、曲不足が起きないようにする"""
    sizes = {bucket: len(songs) for bucket, songs in FALLBACK_SONGS.items()}
    assert set(sizes.values()) == {FALLBACK_SONGS_PER_BUCKET}, sizes


def test_fallback_song_master_covers_1950_to_2025():
    """1950年代から2025年までのバケットが揃っている"""
    for decade in (1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020, 2025):
        assert FALLBACK_SONGS.get(decade), decade


def test_kantishiyagare_is_not_in_the_1970s_bucket():
    """「勝手にしやがれ」（沢田研二・1981年）が1970バケットに残っていないこと"""
    from retro_radio.core.fallback import FALLBACK_SONGS as songs_map

    assert ("勝手にしやがれ", "沢田研二") not in songs_map[1970]
    assert ("勝手にしやがれ", "沢田研二") in songs_map[1980]
    assert FALLBACK_SONG_YEARS[("勝手にしやがれ", "沢田研二")] == 1981


def test_sekaiichi_no_hana_is_attributed_to_morning_musume():
    """「世界に一つだけの花」の歌手が誤りではなくないこと"""
    assert ("世界に一つだけの花", "モーニング娘。") in FALLBACK_SONGS[2000]
    assert FALLBACK_SONG_YEARS[("世界に一つだけの花", "モーニング娘。")] == 2001


@pytest.mark.parametrize("year", ALL_YEARS)
def test_fallback_song_comes_from_the_matching_bucket(year):
    """提示される代表曲がその年に対応するバケット由来であること"""
    if year == 2025:
        bucket = 2025
    else:
        bucket = _bucket_of(year)
    title, artist = get_fallback_song(year)
    assert (title, artist) in FALLBACK_SONGS[bucket], (year, bucket, title, artist)
    assert FALLBACK_SONG_YEARS[(title, artist)] <= get_settings().max_year


@pytest.mark.parametrize("year", [1960, 1980, 2000, 2025])
def test_fallback_songs_are_unique_and_sufficient(year):
    """補完が要求数を満たし、重複しないこと"""
    songs = get_fallback_songs(year, count=5)
    assert len(songs) == 5, (year, songs)
    assert len(set(songs)) == 5, (year, songs)


# --- 5. clean_script_for_tts は曲振り台詞を残す ---------------------------------
@pytest.mark.parametrize(
    "line",
    [
        "それでは、オープニングの曲をお届けします。",
        "それでは、この年のヒット曲をお届けします。",
        "続いて、また懐かしい一曲をお届けします。",
        "そして最後に、懐かしい名曲「卒業写真」（荒井由実）をお届けいたします。",
        "エンディングの曲でお別れしましょう。",
    ],
)
def test_clean_script_for_tts_keeps_song_cue_lines(line):
    """曲振り台詞は原稿の一部なので消さない"""
    assert line in clean_script_for_tts(line)


@pytest.mark.parametrize(
    "line",
    [
        "条件:",
        "口調: 丁寧で温かみのある語り口",
        "対象年: 1980年5月15日",
        "重要度: 10",
        "あなたは昭和・平成のレトロなラジオパーソナリティです。",
        "以下のセグメント構成で原稿を書いてください。",
        "最後は「それでは、この年のヒット曲をお届けします」で締めくくる",
        "1. 社会 - 日本の重要な出来事 (重要度: 10)",
    ],
)
def test_clean_script_for_tts_removes_prompt_instructions(line):
    """プロンプトの指示語だけを削除する"""
    assert clean_script_for_tts(line) == ""


@pytest.mark.parametrize(
    "line",
    [
        "条件が整ったほどの静かな時間でした。",
        "構成について少しお話しします。",
        "日本の夏はとても長かった。",
    ],
)
def test_clean_script_for_tts_keeps_lines_that_merely_start_like_a_label(line):
    """見出し語で始まる散文を誤って落とさない"""
    assert line in clean_script_for_tts(line)


def test_clean_script_for_tts_keeps_a_full_script_body():
    """実原稿の主要行が残り、見出しだけ落ちる"""
    script = generate_fallback_script(1975, 9, 24)
    cleaned = clean_script_for_tts(script)
    # 曲の前置き（Round 1 で「〜をお届けします。を…」の文法破綻を
    # 「ヒット曲、{曲名}をお届けいたします。」へ直した）。
    assert "それでは、この年のヒット曲、" in cleaned
    assert "懐かしい一曲、" in cleaned
    assert "### " not in cleaned
    assert len(cleaned) > 800


def test_clean_script_for_tts_handles_empty_input():
    """空入力が正しく処理されること"""
    assert clean_script_for_tts("") == ""
    assert clean_script_for_tts(None) == ""


# --- 6. select_songs の重複除去 -------------------------------------------------
def test_select_songs_removes_duplicates_from_itunes_results():
    """同じ iTunes レコードが複数回返っても1番組内で1回だけ流す"""
    songs = [
        {"trackName": "A", "artistName": "X", "previewUrl": "http://a.mp3"},
        {"trackName": "A", "artistName": "X", "previewUrl": "http://a.mp3"},
        {"trackName": "B", "artistName": "Y", "previewUrl": "http://b.mp3"},
        {"trackName": "B", "artistName": "Y", "previewUrl": "http://b.mp3"},
        {"trackName": "C", "artistName": "Z", "previewUrl": None},
    ]
    result = select_songs(1980, songs, count=3)

    keys = [(s["trackName"], s["artistName"]) for s in result]
    assert len(keys) == 3, keys
    assert len(set(keys)) == 3, keys


def test_select_songs_dedupes_across_preview_and_static_fallback(monkeypatch):
    """プレビューあり／静的フォールバックの両方にまたがって重複させない

    iTunes 候補と静的フォールバックが同じ曲を返す状況を確実につくる。
    """
    import retro_radio.core.music_search as music_search

    same = ("卒業写真", "荒井由実")
    monkeypatch.setattr(
        music_search, "get_fallback_songs", lambda year, count=3: [same] * count
    )
    songs = [{"trackName": same[0], "artistName": same[1], "previewUrl": "http://x.mp3"}]

    result = music_search.select_songs(1975, songs, count=3)
    keys = [(s["trackName"], s["artistName"]) for s in result]
    assert len(set(keys)) == len(keys), keys


def test_select_songs_prefers_records_with_preview():
    """プレビュー付きを優先しつつ重複しない"""
    songs = [
        {"trackName": f"曲{i}", "artistName": "歌手", "previewUrl": f"http://x/{i}.mp3"}
        for i in range(5)
    ]
    result = select_songs(1980, songs, count=3)
    assert len(result) == 3
    assert all(s.get("previewUrl") for s in result)


def test_select_songs_never_fabricates_records():
    """iTunes が空なら正本（静的マスター / 曲カタログ）の曲だけを選ぶ

    静的マスターは 1 バケット 4 曲で対象年の曲になりきれないため、
    補充は正本カタログ（``core/songs/songs.json``）の**対象年の曲**へ
    広がる（``fallback._catalog_in_era_songs``）。捏造が許されるのは
    「正本に無い曲」であって「音源が無い音源 URL」ではない。
    """
    result = select_songs(1980, [], count=3)
    known = {(t, a) for songs in FALLBACK_SONGS.values() for t, a in songs}
    known |= {
        (str(item.get("title")), str(item.get("artist")))
        for item in (load_songs() or [])
    }
    assert len(result) == 3
    for song in result:
        assert song["previewUrl"] is None
        assert (song["trackName"], song["artistName"]) in known, song


def test_select_songs_never_returns_a_future_year_song():
    """補充曲目も「対象年より後」の曲を含まない（事実誤認の防止）"""
    from retro_radio.core.songs import songs_for_year

    year = 1950
    in_era = {
        (str(item.get("title")), str(item.get("artist")))
        for item in songs_for_year(year, tolerance=0)
    }
    result = select_songs(year, [], count=4)
    picked = [(song["trackName"], song["artistName"]) for song in result]
    later = [
        pair
        for pair in picked
        if pair not in in_era
        and FALLBACK_SONG_YEARS.get(pair, year) > year
    ]
    assert not later, later


def test_no_script_ever_announces_a_song_from_a_later_year():
    """全 76 年 × 3 モードで「対象年より後の曲」を紹介しない（事実誤認の防止）

    正本カタログには**対象年の曲が無い年**がある（1953・1954 は 0 件、
    1955 は 1 件。`core.songs.thin_years` が 29 年を報告）。その年を
    静的マスターだけで埋めると、`partition_by_release_year` が
    1960 年の「上を向いて歩こう」で埋めてしまう。対象年より前の年で
    埋められる間は後年の曲を出してはいけない。
    """
    from eval.metrics import selection_window_titles
    from eval.metrics.fact_score import fact_score
    from retro_radio.core.fallback import (
        generate_anniversary_script,
        generate_care_script,
        generate_fallback_script,
    )

    builders = {
        "normal": lambda y: generate_fallback_script(y, 5, 15),
        "care_recreation": lambda y: generate_care_script(y, 5, 15),
        "anniversary": lambda y: generate_anniversary_script(y, 5, 15, "花子"),
    }
    offenders = []
    for name, build in builders.items():
        for year in ALL_YEARS:
            result = fact_score(
                build(year), year, allowed_song_titles=selection_window_titles(year)
            )
            for failure in result.failures:
                offenders.append((name, year, failure.detail))
    assert not offenders, offenders[:5]


# --- 7. 全年代 × 全モードのループ ------------------------------------------------
def test_all_years_and_modes_generate_without_exception():
    """1950〜2025 × 3モードで例外が1件も出ないこと"""
    errors = []
    total = 0
    for name, builder in _script_builders().items():
        for year in ALL_YEARS:
            total += 1
            try:
                builder(year, 5, 15)
            except Exception as exc:  # pragma: no cover - 失敗時の証拠を残す
                errors.append((name, year, repr(exc)))
    assert total == len(ALL_YEARS) * 3
    assert errors == []


def test_all_years_and_modes_stay_inside_the_script_length_band():
    """全年代 × 全モードが**文字数の帯**に入ること（「以上」から「帯内」へ）

    提案⑨ で「1,000 文字以上」を捨てた理由:

    - 「以上」は**短すぎる原稿だけを罰する**ため、情景描写を反復して
      1,000 字を埋めること（＝水増し）が最適行動になっていた。
    - ``generate_fallback_script``（``core/fallback.py:341``）が
      「三つほどご用意しました」と予告しながら中身は空気だけ、という
      欠陥が維持されていたのもこの目標のためである。
    - 下限は「生成できているか」、上限は「水増ししていないか」を見る。

    帯の実測値と根拠は ``eval/metrics/length.py`` の docstring
    （``python -m eval.metrics.length --measure`` で再現できる）。

    **落ちたときは「誰・何年・なぜ」を列挙する**。「外れたのが 0 件だから通った」とは
    書かない（通ることが自明でも、失敗時の可読性を優先する）。
    """
    lower, upper = length_bounds()
    too_short = []
    too_long = []
    for name, builder in _script_builders().items():
        for year in ALL_YEARS:
            length = len(builder(year, 5, 15))
            if length < lower:
                too_short.append((name, year, length))
            elif length > upper:
                too_long.append((name, year, length))

    report = [
        f"許容帯 = {lower}〜{upper} 字 "
        f"(target_script_chars={get_settings().target_script_chars}, "
        f"script_char_tolerance={get_settings().script_char_tolerance})",
        f"全 {len(ALL_YEARS) * 3} サンプル中 帯外 {len(too_short) + len(too_long)} 件",
    ]
    if too_short:
        report.append(f"--- 短すぎる（下限 {lower} 未満）{len(too_short)} 件 ---")
        report += [f"  {mode}/{year}: {length} 字（不足 {lower - length}）" for mode, year, length in too_short]
    if too_long:
        report.append(f"--- 長すぎる（上限 {upper} 超過）{len(too_long)} 件 ---")
        report += [f"  {mode}/{year}: {length} 字（超過 {length - upper}）" for mode, year, length in too_long]

    assert not too_short and not too_long, "\n".join(report)


def test_script_length_band_punishes_both_ends_not_just_the_lower_one():
    """帯は**両端**を罰する（旧「1,000 字以上」構造の復活を防ぐ）

    旧テスト（``>= 1000``）では 1,000 字ちょうどと 5,000 字が同じ扱いだった。
    ここで「上限を超えた長大稿が落ちる」ことを固定し、旧目標構造の復活を防ぐ。
    """
    lower, upper = length_bounds()
    target = get_settings().target_script_chars

    # 下限側の規格値（1,000 字）は帯内に入る。ただし空虚な原稿は落ちる。
    padded = "。" * target
    assert check_length(padded).ok, f"{target} 字の原稿は帯内に入るべき（帯の低端側）"

    # 下限を割る空虚な原稿は落ちる。
    assert not check_length("短い。").ok
    assert check_length("短い。").direction == "too_short"

    # **上限を超える長大稿は落ちる**（これが旧構造の反転）。
    bloated = "。" * (upper + 1)
    assert not check_length(bloated).ok
    assert check_length(bloated).direction == "too_long"

    # 帯の両端そのものは通る（境界を含む）。
    assert check_length("あ" * lower).ok
    assert check_length("あ" * upper).ok


@pytest.mark.parametrize("year", [1950, 1964, 1975, 1985, 1995, 2005, 2015, 2025])
def test_no_unfulfilled_preannounce_in_fallback_script(year):
    """予告した 3 件を**実際に配る**ようになったこと（S3 で期待値を反転）

    旧実装は「三つほどご用意しました」と予告しながら、トーク1〜トーク3 に
    具体的な項目を 1 件も置いていなかった（S2 の ``eval`` が 8 ケース検出）。
    S3（提案②）で ``generate_fallback_script`` が和暦と**実際の曲名**を
    入れるようにしたので、予告が履行されるようになった。

    したがって本テストは「検出できる」ことの固定ではなく、
    **「検出 0 件」が正しい状態である**ことの固定に変わる。
    """
    findings = detect_unfulfilled_preannounce(generate_fallback_script(year, 5, 15))
    assert findings == [], (year, findings)


def test_no_known_preannounce_defect_in_care_and_anniversary_scripts():
    """介護・記念日モードには未履行予告がないこと（正常側の固定）

    介護モードは「三つのタネ」と予告して実際に 3 つ出すため、
    検出器が正常な原稿を誤検出しないことの固定でもある。
    """
    for year in (1950, 1975, 2015, 2025):
        assert detect_unfulfilled_preannounce(generate_care_script(year, 5, 15)) == [], year
        assert (
            detect_unfulfilled_preannounce(generate_anniversary_script(year, 5, 15, "花子")) == []
        ), year


def test_all_care_scripts_stay_segmented_and_named():
    """介護原稿の構造（見出し・回想タネ・歌手名）が崩れにくいこと"""
    for year in ALL_YEARS:
        script = generate_care_script(year, 5, 15)
        assert "### オープニング" in script, year
        assert "### エンディング" in script, year
        assert "思い出" in script, year
        assert f"{year}年" in script, year


# ---------------------------------------------------------------------------
# 定型原稿の文法（テンプレート置換の残骸が混ざっていないこと）
# ---------------------------------------------------------------------------
#
# 以前は `core/fallback.py` の通常モード原稿に
# 「…この年のヒット曲をお届けします。**を**「神田川」（南こうせつとかぐや姫）。」
# という**テンプレート置換の残骸**が 3 箇所あった（`generate_fallback_script(1975,9,24)`
# で実測）。`clean_script_for_tts` の除去規則にも一致しないためそのまま残り、
# **TTS が「〜をお届けします。を「神田川」（…）。」と読み上げていた**。
# 介護用途の原稿として文として成立していない。


@pytest.mark.parametrize("year", [1950, 1964, 1975, 1985, 1995, 2005, 2015, 2025])
def test_fallback_script_contains_no_broken_sentence_fragment(year):
    """`。を`（句点直後の助詞「を」）のような文法破綻が残らないこと。"""
    import re

    from retro_radio.core.fallback import generate_fallback_script

    script = generate_fallback_script(year, 9, 24)
    assert not re.search(r"。を", script), (
        f"{year}年の原稿に「。を」を含む文法破綻があります: "
        f"{re.findall(r'[^。\n]{0,30}。を[^。\n]{0,40}', script)}"
    )


@pytest.mark.parametrize("year", [1950, 1975, 2015])
def test_fallback_script_song_phrases_read_as_sentences(year):
    """`_song_phrase` の直前は句点ではなく、前置きか読点であること。

    「〜をお届けします。を「曲名」」のような並びを許さない。
    """
    import re

    from retro_radio.core.fallback import generate_fallback_script

    script = generate_fallback_script(year, 9, 24)
    # `」（歌手）。` で終わる文は許すが、直前に `。` が来る並びは弾く。
    assert not re.search(r"。を[「『]", script), year
    # 曲名が文中に現れること（置換が生き残っていることの確認）。
    assert re.search(r"[「『][^」』]{1,40}[」』]", script), year
