"""`core/` と `models/radio.py` のドメインモデル・生成ロジックの検証。

旧 `tests/test_core_implementation.py` / `test_core_implementation_simple.py` /
`tests/test_script_segments.py` の重複を統合したもの。

Wave 1 の変更追随:
  - `select_news_topics` / `NewsTopic` は `core.fallback` ではなく
    `core.script_generator` に移動した
  - `parse_script_segments` は「見出しなし → None」ではなく「[]」を返す
  - `HistoricalRadioPrograms.get_program_guide` は決定的な选定にした
"""

import pytest

from retro_radio.core.fallback import HistoricalRadioPrograms
from retro_radio.core.script_generator import (
    NEWS_TOPICS_BY_DECADE,
    NewsTopic,
    parse_script_segments,
    select_news_topics,
)
from retro_radio.models.radio import (
    PlaylistItem,
    PlaylistItemType,
    ProgramGuide,
    ProgramSchedule,
    ScriptSegment,
)
from retro_radio.server import parse_script_segments as server_parse_script_segments


SCRIPT = """### オープニング
皆様、こんばんは。开场 greeted 今天的日期是1980年5月15日。

### トーク1_ニュース
1970年代のニュース原稿です。

### トーク2_くらし
くらしの原稿です。

### エンディング
おわりに。
"""


# --- ドメインモデル --------------------------------------------------------------
def test_script_segment_to_dict():
    segment = ScriptSegment(
        id="seg_0",
        title="オープニング",
        content="本文",
        estimated_duration=3.0,
        order=0,
        metadata={"source": "test"},
    )
    payload = segment.to_dict()
    assert payload["id"] == "seg_0"
    assert payload["title"] == "オープニング"
    assert payload["metadata"] == {"source": "test"}
    assert payload["order"] == 0


def test_script_segment_metadata_defaults_to_empty_dict():
    segment = ScriptSegment(id="s", title="t", content="c", estimated_duration=1.0, order=0)
    assert segment.to_dict()["metadata"] == {}


def test_program_schedule_defaults_to_modern():
    schedule = ProgramSchedule(
        id="modern_1",
        title="モーニングニュース",
        start_time="06:00",
        duration=30,
        description="最新のニュース",
    )
    assert schedule.is_historical is False
    assert schedule.source == "modern"
    assert schedule.to_dict()["is_historical"] is False


def test_program_guide_to_dict():
    guide = ProgramGuide(
        date="2025-09-24",
        weekday="水",
        schedules=[
            ProgramSchedule(
                id="modern_1", title="ニュース", start_time="06:00", duration=30, description="d"
            )
        ],
        today_highlight="ハイライト",
    )
    payload = guide.to_dict()
    assert payload["date"] == "2025-09-24"
    assert payload["special_events"] == []
    assert payload["schedules"][0]["id"] == "modern_1"


def test_news_topic_fields():
    topic = NewsTopic(1975, "社会", "見出し", 8)
    assert (topic.year, topic.category, topic.headline, topic.importance) == (
        1975,
        "社会",
        "見出し",
        8,
    )


# --- parse_script_segments -------------------------------------------------------
def test_parse_script_segments_extracts_sections():
    segments = parse_script_segments(SCRIPT)
    assert len(segments) == 4
    assert segments[0].title == "オープニング"
    assert "开场 greeted" in segments[0].content
    assert segments[1].title == "トーク1_ニュース"
    assert segments[-1].title == "エンディング"
    assert [s.order for s in segments] == [0, 1, 2, 3]
    assert all(s.estimated_duration > 0 for s in segments)


def test_parse_script_segments_without_headings_returns_empty_list():
    """見出しなしは None ではなく空リスト（Wave 1 の仕様変更）"""
    assert parse_script_segments("見出しのない原稿です。") == []
    assert parse_script_segments("") == []


def test_parse_script_segments_does_not_raise_on_garbage():
    segments = parse_script_segments("### 見出しだけ\n")
    assert len(segments) == 1
    assert segments[0].content.strip() == ""


def test_server_delegates_to_core_parser():
    """server.parse_script_segments は core 版に委譲している（重複定義を持たない）"""
    assert server_parse_script_segments is parse_script_segments
    assert server_parse_script_segments("no headings") == []


def test_parse_script_segments_requires_markdown_heading_with_space():
    """`###` の後に空白が無い行は見出しとして扱わない"""
    assert parse_script_segments("###見出し") == []


# --- select_news_topics ---------------------------------------------------------
def test_select_news_topics_returns_requested_count():
    topics = select_news_topics(1975, count=2)
    assert len(topics) == 2
    assert all(isinstance(t, NewsTopic) for t in topics)


def test_select_news_topics_sorted_by_importance_desc():
    topics = select_news_topics(1975, count=3)
    importances = [t.importance for t in topics]
    assert importances == sorted(importances, reverse=True)


def test_select_news_topics_stays_within_decade():
    for year in (1955, 1964, 1975, 1985, 1995, 2005, 2015, 2024):
        for topic in select_news_topics(year, count=3):
            assert topic.year == (year // 10) * 10


def test_select_news_topics_clamps_to_available_topics():
    """要求数が実データより多い場合は取り切れるだけ返す"""
    decade = 2010
    available = len(NEWS_TOPICS_BY_DECADE[decade])
    assert len(select_news_topics(2015, count=available + 10)) == available


def test_select_news_topics_for_2025_uses_2025_bucket():
    """2025年は専用の 2025 バケットを使う（`NEWS_TOPICS_BY_DECADE[2025]` を生かす）"""
    topics = select_news_topics(2025, count=1)
    assert len(topics) == 1
    assert topics[0].year == 2025


@pytest.mark.parametrize("year", [1955, 1964, 1975, 1985, 1995, 2005, 2015, 2024])
def test_select_news_topics_uses_decade_bucket_without_exact_year(year):
    """年バケットが無い年は年代バケット（year // 10 * 10）を使う"""
    for topic in select_news_topics(year, count=3):
        assert topic.year == (year // 10) * 10


def test_2025_news_bucket_is_reachable():
    assert select_news_topics(2025, count=1)[0].year == 2025


def test_select_news_topics_caps_at_ten_importance():
    for topic in select_news_topics(1980, count=5):
        assert 1 <= topic.importance <= 10


# --- HistoricalRadioPrograms.get_program_guide ---------------------------------
def test_program_guide_shape():
    guide = HistoricalRadioPrograms.get_program_guide(1975, 9, 24)

    assert isinstance(guide, ProgramGuide)
    assert guide.date == "1975-09-24"
    assert guide.weekday in ["日", "月", "火", "水", "木", "金", "土"]
    assert guide.schedules
    assert any(not s.is_historical for s in guide.schedules)


def test_program_guide_always_contains_a_historical_program():
    """1970年代の歷史番組が必ず1本含まれる（.random() を廃止し決定的に）"""
    for _ in range(20):
        guide = HistoricalRadioPrograms.get_program_guide(1975, 9, 24)
        assert sum(1 for s in guide.schedules if s.is_historical) == 1


def test_program_guide_is_deterministic():
    """同じ (year, month, day) なら常に同じ結果（冪等・決定性）"""
    first = HistoricalRadioPrograms.get_program_guide(1984, 2, 29).to_dict()
    for _ in range(10):
        assert HistoricalRadioPrograms.get_program_guide(1984, 2, 29).to_dict() == first


def test_program_guide_different_dates_differ():
    a = HistoricalRadioPrograms.get_program_guide(1975, 9, 24).to_dict()
    b = HistoricalRadioPrograms.get_program_guide(1976, 9, 24).to_dict()
    assert a["date"] != b["date"]


def test_program_guide_history_varies_by_year_within_decade():
    """同じ年代でも年が変われば歴史番組が変わる（`year % len` の選定が機能している）"""
    picks = {
        HistoricalRadioPrograms.get_program_guide(1970, 1, 1).schedules[0].id,
        HistoricalRadioPrograms.get_program_guide(1971, 1, 1).schedules[0].id,
        HistoricalRadioPrograms.get_program_guide(1972, 1, 1).schedules[0].id,
    }
    assert len(picks) > 1


def test_program_guide_handles_impossible_date():
    """実在しない日付でも例外を投げずに «不明» を返す（API 側では 422 で弾かれる）"""
    guide = HistoricalRadioPrograms.get_program_guide(2020, 2, 30)
    assert guide.weekday == "不明"
    assert guide.schedules


def test_program_guide_to_dict_exposes_frontend_fields():
    payload = HistoricalRadioPrograms.get_program_guide(1975, 9, 24).to_dict()
    for key in ("date", "weekday", "schedules", "today_highlight", "special_events"):
        assert key in payload
    for schedule in payload["schedules"]:
        assert set(("id", "title", "start_time", "duration", "description", "is_historical")) <= set(schedule)
        assert schedule["duration"] > 0
        assert isinstance(schedule["is_historical"], bool)


# --- PlaylistItem 検証（Wave 1 で __post_init__ が追加） ------------------------
def test_playlist_item_valid():
    item = PlaylistItem(id="a", type=PlaylistItemType.SONG, title="曲", artist="歌手")
    assert item.to_dict()["type"] == "song"


def test_playlist_item_normalizes_type():
    """小文字/大文字どちらも PlaylistItemType に正規化される"""
    assert PlaylistItem(id="a", type="song", title="t").type is PlaylistItemType.SONG
    assert PlaylistItem(id="a", type="TALK", title="t", content="c").type is PlaylistItemType.TALK


def test_playlist_item_rejects_unknown_type():
    with pytest.raises(ValueError, match="Invalid playlist item type"):
        PlaylistItem(id="a", type="bogus", title="t")


def test_playlist_item_rejects_empty_id():
    with pytest.raises(ValueError, match="id must not be empty"):
        PlaylistItem(id="", type="song", title="t")


def test_playlist_item_rejects_empty_title():
    with pytest.raises(ValueError, match="title must not be empty"):
        PlaylistItem(id="a", type="song", title="")


def test_playlist_item_talk_requires_content():
    """トーク項目は content が必須（トーク-less な無音再生を防ぐ）"""
    with pytest.raises(ValueError, match="require content"):
        PlaylistItem(id="a", type=PlaylistItemType.TALK, title="t")


def test_playlist_item_to_dict_type_is_lowercase():
    """JSON 上の type は小文字（フロント app.js は小文字前提）"""
    assert PlaylistItem(id="a", type=PlaylistItemType.TALK, title="t", content="c").to_dict()["type"] == "talk"


def test_playlist_item_metadata_defaults_to_empty_dict():
    assert PlaylistItem(id="a", type="song", title="t").to_dict()["metadata"] == {}
