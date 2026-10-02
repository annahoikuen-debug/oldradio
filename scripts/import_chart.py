"""チャート順位を保つ形で、採用済み候補を正本 ``songs.tsv`` へ合流する。

なぜ専用スクリプトか
--------------------
``scripts/import_songs.py --merge`` は採用分を**末尾へ追記**する。
``build_song_catalog.py`` は TSV の**行順**で rank を振り直すため、
追記すると新しい曲は既存曲より後の rank（= 下位）になる。

しかし 1968 年以前のデータは「年別ヒット曲ランキング」から取得した
**順位付き**データであり、順位を反映させないと同じクラスの曲同士で
並びが崩れる。よって対象年については、

    チャート順（rank 1, 2, 3…）→ 既存の未照合曲

の順に並べ替えてから書き直す。

同じ年・同じ曲名を持つ既存曲Alreadyある場合は**追記しない**
（同じ曲が 2 つの id で正本に入り、「1 番組で 1 曲ずつ」の保証が
壊れるため。``core/songs/__init__.py`` が同じ理由で重複を弾く）。

使い方
------
    python scripts/import_chart.py
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from retro_radio.core.songs import normalize_song_text, song_key  # noqa: E402

SONG_SOURCE = REPO_ROOT / "scripts" / "song_source"
SOURCE_TSV = SONG_SOURCE / "songs.tsv"
CHART_TSV = SONG_SOURCE / "chart_pre1968.tsv"

#: 採用結果（iTunes 照合済み）の TSV。すべて同じ形式。
ACCEPTED_FILES = ("chart_acc.tsv", "chart_acc2.tsv", "chart_acc3.tsv", "chart_acc4.tsv")

#: 正本 TSV の列。
COLUMNS = ("release_year", "title", "artist", "genre", "note_ja", "confidence", "source")


def read_rows(path: Path) -> List[List[str]]:
    """TSV を読み、コメント行とヘッダを除いた列のリストを返す。"""
    rows: List[List[str]] = []
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if not parts[0].strip().isdigit():
            continue
        rows.append(parts)
    return rows

def chart_rows_by_year() -> Dict[int, List[Tuple[int, List[str]]]]:
    """チャート TSV を年ごとの ``(順位, 行)`` にする。"""
    by_year: Dict[int, List[Tuple[int, List[str]]]] = defaultdict(list)
    for parts in read_rows(CHART_TSV):
        rank = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 999
        by_year[int(parts[0])].append((rank, parts))
    for entries in by_year.values():
        entries.sort(key=lambda pair: pair[0])
    return dict(by_year)


def accepted_by_year() -> Dict[Tuple[int, str], List[str]]:
    """採用済み曲 ``(年, 正規化曲名)`` → 正本 TSV の行。"""
    out: Dict[Tuple[int, str], List[str]] = {}
    for name in ACCEPTED_FILES:
        path = SONG_SOURCE / name
        if not path.exists():
            continue
        for parts in read_rows(path):
            year = int(parts[0])
            title = parts[1].strip()
            artist = parts[2].strip()
            out[(year, normalize_song_text(title))] = [
                str(year), title, artist,
                parts[3] if len(parts) > 3 else "",
                parts[4] if len(parts) > 4 else "",
                "unverified",
                "secondary:nendai-ryuukou.com/houyaku-hit-ranking+itunes-preview",
            ]
    return out


def main() -> int:
    chart = chart_rows_by_year()
    accepted = accepted_by_year()

    existing: List[List[str]] = read_rows(SOURCE_TSV)
    existing_keys = {song_key(p[1], p[2]) for p in existing}
    titles_in_year: Dict[int, set] = defaultdict(set)
    for parts in existing:
        titles_in_year[int(parts[0])].add(normalize_song_text(parts[1]))

    added = 0
    skipped = 0
    for year, entries in chart.items():
        for _rank, parts in entries:
            title = parts[2].strip()
            artist = parts[3].strip()
            normalized = normalize_song_text(title)
            row = accepted.get((year, normalized))
            if row is None:
                continue
            if song_key(title, artist) in existing_keys:
                skipped += 1
                continue
            if normalized in titles_in_year[year]:
                # 同年に同じ曲名の，既有表記を正とする。
                skipped += 1
                continue
            existing.append(row)
            existing_keys.add(song_key(title, artist))
            titles_in_year[year].add(normalized)
            added += 1

    by_year: Dict[int, List[List[str]]] = defaultdict(list)
    for parts in existing:
        by_year[int(parts[0])].append(parts)

    # 正本 songs.tsv にヘッダ行は置かない。build_song_catalog.parse_tsv は
    # ヘッダを認識せず「release_year が整数でない」で落ちる（実測）。
    lines: List[str] = []
    for year in sorted(by_year):
        rows = by_year[year]
        # 対象年のみ「チャート順位の曲 → 既存の未照合曲」の順に並べる。
        rank_map = {
            normalize_song_text(entry[1][2]): entry[0]
            for entry in chart.get(year, [])
        }
        ranked: List[Tuple[int, int, List[str]]] = []
        plain: List[Tuple[int, int, List[str]]] = []
        for index, parts in enumerate(rows):
            normalized = normalize_song_text(parts[1])
            if normalized in rank_map:
                ranked.append((int(rank_map[normalized]), index, parts))
            else:
                plain.append((index, index, parts))
        ranked.sort(key=lambda item: (item[0], item[1]))
        plain.sort(key=lambda item: item[0])
        for _rank, _index, parts in ranked + plain:
            padded = list(parts) + [""] * (len(COLUMNS) - len(parts))
            lines.append("\t".join(padded[: len(COLUMNS)]))

    SOURCE_TSV.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"正本 songs.tsv: {added} 行追加 / {skipped} 行は既存と重複のため不採用")
    print(f"対象年 {sorted(chart)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
