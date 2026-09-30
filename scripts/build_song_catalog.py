"""曲カタログの正本 JSON を、編集用の TSV から生成する。

なぜ TSV なのか
--------------
1 年 50 曲 × 76 年 = 3800 レコード。JSON で 1 レコード 6 フィールドを
手で書くと、括弧・カンマ・キー名の typos を検出する手段がないまま
3000 行近いデータを保守することになる。TSV は 1 行 1 曲で
「曲名・アーティストのタイポ」は iTunes 照合（``--verify``）が検出できる。

責務の分担
----------
* ``scripts/song_source/songs.tsv``  … **編集する面**（人が触る）
* ``retro_radio/core/songs/songs.json`` … **正本**（アプリが読む）
* ``tests/test_song_catalog.py`` … 両者が同期していることを検証する

使い方
------
    python scripts/build_song_catalog.py            # TSV -> JSON を生成
    python scripts/build_song_catalog.py --check     # 同期しているか検査のみ

曲名・アーティストの正誤と、iTunes プレビュー音源の有無は
``scripts/validate_songs.py --verify`` が実ネットワークで照合する。
総当たらで数千リクエストになるため時間がかかり、**1 年ぶんの修正を
するときだけ**実行すること。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_TSV = REPO_ROOT / "scripts" / "song_source" / "songs.tsv"
TARGET_JSON = REPO_ROOT / "retro_radio" / "core" / "songs" / "songs.json"

# 一次文献を照合していない曲に付ける出典欄の値。
# URL を推測で書かないため、文献名も書かず「未照合」であることを明示する
# （docs/facts_registry.md 3.4 と同じ方針）。
UNVERIFIED_SOURCE = "unverified:primary-source-pending"

COLUMNS = ("release_year", "title", "artist", "genre", "note_ja", "confidence", "source")


def _norm(value: str) -> str:
    """比較用の正規化（`core.songs.normalize_song_text` と同じ規則）。"""
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    return "".join(text.split())


def parse_tsv(path: Path) -> List[Dict[str, object]]:
    """編集用 TSV を読み込んで正本レコードのリストにする。

    列は ``release_year / title / artist / genre / note_ja / confidence / source``。
    ``confidence`` と ``source`` は省略可（既定は未照合）。

    Raises
    ------
    ValueError
        年が整数でない、曲名・アーティストが空、confidence が許可値でない、
        または同一ファイル内で曲名が重複した場合。
    """
    if not path.exists():
        raise FileNotFoundError(f"編集用 TSV がありません: {path}")

    records: List[Dict[str, object]] = []
    # 曲名だけで重複を弾くと「同名の別曲」（同名異曲）を潰してしまうため、
    # 年 + 曲名 + アーティスト で判定する。
    keys: Dict[Tuple[int, str, str], int] = {}
    ranks: Dict[int, int] = {}

    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            raise ValueError(f"{path}:{lineno}: 列が足りません（年/曲名/アーティスト）: {raw!r}")
        parts += [""] * (len(COLUMNS) - len(parts))
        year_s, title, artist, genre, note, confidence, source = parts[: len(COLUMNS)]

        try:
            year = int(year_s.strip())
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: release_year が整数でない: {year_s!r}") from exc

        title = title.strip()
        artist = artist.strip()
        if not title or not artist:
            raise ValueError(f"{path}:{lineno}: 曲名・アーティストが空です: {raw!r}")

        confidence = (confidence or "unverified").strip()
        if confidence not in ("verified", "unverified"):
            raise ValueError(
                f"{path}:{lineno}: confidence が不正です: {confidence!r}"
                "（verified / unverified のみ）"
            )

        key = (year, _norm(title), _norm(artist))
        if key in keys:
            raise ValueError(
                f"{path}:{lineno}: {year} 年の「{title}」（{artist}）は {keys[key]} 行目と重複"
            )
        keys[key] = lineno

        ranks[year] = ranks.get(year, 0) + 1
        record: Dict[str, object] = {
            "id": f"s{year}-{ranks[year]:03d}",
            "title": title,
            "artist": artist,
            "release_year": year,
            "rank": ranks[year],
            "source": (source or UNVERIFIED_SOURCE).strip() or UNVERIFIED_SOURCE,
            "confidence": confidence,
        }
        if genre.strip():
            record["genre"] = genre.strip()
        if note.strip():
            record["note_ja"] = note.strip()
        records.append(record)

    if not records:
        raise ValueError(f"{path}: レコードが 1 件もありません")
    return records


def render_json(records: List[Dict[str, object]]) -> str:
    """正本 JSON のテキストを返す（末尾改行つき）。"""
    payload = {
        "_comment": [
            "曲カタログ正本。1 曲 1 レコード。並び順は release_year 昇順、同年は rank 昇順。",
            "release_year は原典の発表年。iTunes の releaseDate は配信日なので照合には使えない。",
            "confidence は一次文献照合の有無。照合していないものは unverified を書く。",
            "source に URL を推測で書かないこと（docs/song_catalog.md 4 節）。",
            "データの追加・編集は scripts/song_source/songs.tsv で行い、本ファイルは生成物。",
            "tests/test_song_catalog.py が両者の同期を検証する。",
        ],
        "records": records,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def coverage(records: List[Dict[str, object]]) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for record in records:
        year = int(record["release_year"])
        counts[year] = counts.get(year, 0) + 1
    return dict(sorted(counts.items()))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="曲カタログ正本 JSON を生成する")
    parser.add_argument("--check", action="store_true", help="生成せず、同期しているか検査する")
    parser.add_argument("--target", type=int, default=50, help="1 年あたりの目標曲数（既定50）")
    parser.add_argument("--min-year", type=int, default=1950)
    parser.add_argument("--max-year", type=int, default=2025)
    args = parser.parse_args(argv)

    try:
        records = parse_tsv(SOURCE_TSV)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    counts = coverage(records)
    span = f"{min(counts)}-{max(counts)}" if counts else "-"
    missing = [
        year
        for year in range(args.min_year, args.max_year + 1)
        if year not in counts
    ]
    thin = [(year, count) for year, count in counts.items() if count < args.target]

    print(f"総 {len(records)} レコード / {len(counts)} 年 ({span})")
    if missing:
        print(f"WARN 曲が無い年が {len(missing)} 個: {missing}")
    if thin:
        print(f"WARN 目標 {args.target} 曲未満の年が {len(thin)} 個:")
        for year, count in thin:
            print(f"      {year}: {count} 曲")

    rendered = render_json(records)
    if args.check:
        current = TARGET_JSON.read_text(encoding="utf-8") if TARGET_JSON.exists() else ""
        if current != rendered:
            print("ERROR: songs.json が songs.tsv と同期していません（--check 失敗）")
            return 1
        print("OK: songs.json は songs.tsv と同期している")
        return 0

    TARGET_JSON.parent.mkdir(parents=True, exist_ok=True)
    TARGET_JSON.write_text(rendered, encoding="utf-8")
    print(f"書き込みました: {TARGET_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
