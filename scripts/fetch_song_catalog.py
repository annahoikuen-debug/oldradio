"""MusicBrainz から年別のシングル曲カタログを組み立てる。

なぜ MusicBrainz なのか
----------------------
「1 年 50 曲」を満たすには 1950〜2025 年で 3,800 件 필요하다。この規模を
**記憶から書き出すことはできない**。実際、前回の試行では
(i) 年の誤り、(ii) でっち曲名が混入し、(iii) 数量的にも限界があった。

そこで年・曲名・アーティストを**実在データから**取る。

* **MusicBrainz** は ``first-release-date`` を持つ公開音楽データベースで、
  1 リクエスト/秒という-documented なレート制限がある。
  検索は ``date:YYYY AND lang:jpn AND primarytype:Single`` で
  その年にouchingされた日本のシングル 정규に限定できる。
* **iTunes** は検索語に年を入れても年を無視する（実測: 1975 年を索すと
  2019-2024 年の曲しか返らない）。加えて同一 IP からの連打に
  403 Forbidden で拒み、連打すると数分間ブロックされる（実測）。
  年の供給源にはならない。

このスクリプトが作るものは **候補（staging）** であり、正本
（``retro_radio/core/songs/songs.json``）ではない。出力は
``scripts/song_source/musicbrainz.tsv`` にERVICEされ、
``scripts/import_songs.py --candidates`` で iTunes  playable を照合して
から初めて正本に合流する。

曲名の取り出し
--------------
シングル1枚のリリースタイトルには A面/B面の曲名が Slash 区切りで入る::

    あなたの私 / 心の手紙      -> あなたの私, 心の手紙
    年下の男の子                -> 年下の男の子

この Slash で分割して 1 曲 1 レコードにする。

使い方
------
    python scripts/fetch_song_catalog.py                  # 全年を生成
    python scripts/fetch_song_catalog.py --min-year 1970 --max-year 1979
    python scripts/fetch_song_catalog.py --target 30       # 1 年あたり目標
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from retro_radio.core.songs import song_key  # noqa: E402

OUTPUT_TSV = REPO_ROOT / "scripts" / "song_source" / "musicbrainz.tsv"

#: MusicBrainz は 1 リクエスト/秒のレート制限を文書化している。
#: 余裕を持たせて 1.1 秒間隔。
REQUEST_INTERVAL_SECONDS = 1.1

#: 検索 1 ページあたりの件数（MusicBrainz の上限は 100）。
PAGE_SIZE = 100

#: 1 年あたりに読むページ数の上限。
#: MusicBrainz の検索は該当件数が多い年では 1 ページ 100 件で頭打ちになるため、
#: 1 年 300 件（3 ページ）まで読んでから重複を落として採用する。
MAX_PAGES_PER_YEAR = 3

#: 採用しないリリースの status（ブートレグ・海 外版・PSEUDO）。
REJECTED_STATUS = {"Bootleg", "Pseudo-Release"}

USER_AGENT = "retro-radio-catalog/1.0 (https://github.com/; catalog build)"

#: 曲名として長すぎるもの（OST 全集、BOX 等）は候補にしない。
#: ``core.songs.MAX_TITLE_FIELD_LENGTH`` と**同じ上限**。超えた曲名は
#: 正本から読み飛ばされ、選曲しても読み上げ原稿に入らない。
MAX_TITLE_LENGTH = 40

#: 曲名・アーティスト名に現れてはならない並び。
#: ``core.songs._FORBIDDEN_TITLE_SUBSTRINGS`` と同じ規則。
#: 引用括弧は :func:`strip_brackets` で除去するので、ここには入れない。
FORBIDDEN_SUBSTRINGS = ("\r", "\n", "\t", "\x00", "###")

#: DJ ミックス・分裂盤・OST の**タイトル**に現れる形。
#: これらは曲ではないのに ``primarytype:Single`` で拾える（実測:
#: "TV-DJ-Wide Controlmixes: … (J-WIDE CONTROL Vol.10 …)"）。
#: 曲名中の一般語と衝突しない形だけに限定する（"100%ile" や
#: "FLOWER REVOLUTION" を誤って弾かないため）。
_NOT_A_SONG = re.compile(
    r"DJ[\s\-_]|DJ$|\bMIX\b|\bMIXES\b|Selection\s*\(|\bVol\.\s*\d"
    r"|\d巻| piezo| piezo|選曲集|ベスト・アルバム|Best\s+Of\s+\d"
    r"|\d+曲入り|Best\s+Album",
    re.IGNORECASE,
)

#: 分割用の区切り。シングルは A面/B面 を Slash で並べる。
_SLASH_SPLIT = re.compile(r"\s*/\s*|\s*／\s*")


def _fetch(**params: Any) -> Dict[str, Any]:
    """MusicBrainz の JSON API を 1 回叩く。"""
    query = {
        "query": 'date:{year} AND lang:jpn AND primarytype:Single'.format(
            year=params.pop("year")
        ),
        "fmt": "json",
        "limit": PAGE_SIZE,
        "offset": 0,
    }
    query.update(params)
    url = "https://musicbrainz.org/ws/2/release?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _artist_of(release: Dict[str, Any]) -> str:
    """リリースのアーティスト名を連結して返す。"""
    credits = release.get("artist-credit")
    if not isinstance(credits, list):
        return ""
    parts: List[str] = []
    for credit in credits:
        if not isinstance(credit, dict):
            continue
        name = credit.get("artist", {}).get("name") or credit.get("name") or ""
        if name:
            parts.append(str(name))
    return "".join(parts).strip()


#: 曲名中の引用括弧。外すと読み上げ原稿にそのまま載せられる
#: （「C」中山美穂や「もしも明日、僕が死んでも」ナナのように**実在する曲**
#: が括弧付きGcarge PLOY で通報される）。捨てずに外す。
_BRACKET_PAIRS = (("\u300c", "\u300d"), ("\u300e", "\u300f"), ("「", "」"))

#: 曲ではなく**アルバム・OST・ドラマCD・分裂盤**のタイトルに現れる語。
#: ``primarytype:Single`` でも混入する（実測: "TVアニメ『…』エンディングテーマ
#: キャラクターソングシリーズ …"、"…Characters Vol.1 …"、"…& Cover Songs:
#: Complete Best …"）。これらは歌ではないため候補にしない。
_ALBUM_MARKERS = (
    "エンディングテーマ", "オープニングテーマ", "主題歌", "イメージソング",
    "キャラクターソング", "ドラマCD", "ドラマCD", "サウンドトラック",
    "Soundtrack", "soundtrack", "OST", "Best Album", "Complete Best",
    "Cover Songs", "PERSONAL COLLECTION", "Characters Vol", "Characters, Vol",
    "Mega Hits", "TVアニメ", "映画", "邦画", "BEST OF", "Best of",
    "アンコール", "メインテーマ", "タイアップ", "主題歌集", "Sampler",
)


def strip_brackets(text: str) -> str:
    """曲名の引用括弧を取り除く（実在曲なので捨てない）。"""
    for open_ch, close_ch in _BRACKET_PAIRS:
        text = text.replace(open_ch, "").replace(close_ch, "")
    return text.strip()


def _usable_titles(release_title: str) -> List[str]:
    """リリースタイトルを 1 曲ごとの曲名へ分割し、採用可否を判定する。

    弾くもの:
    * 40 文字超（``core.songs.MAX_TITLE_FIELD_LENGTH`` 超過）
    * 制御文字・``###``（読み上げ原稿に載らない）
    * アルバム / OST / ドラマCD / 分裂盤のタイトル（**曲ではない**）

    引用括弧は「実在曲」なので**弾かず外す**:
    ``「C」（中山美穂、1985）は実在のシングルであり、括弧を消せば
    読み上げ原稿に載せられる。
    """
    titles: List[str] = []
    for raw in _SLASH_SPLIT.split(release_title or ""):
        title = strip_brackets(raw)
        if not title or len(title) > MAX_TITLE_LENGTH:
            continue
        if any(bad in title for bad in FORBIDDEN_SUBSTRINGS):
            continue
        if _NOT_A_SONG.search(title):
            continue
        if any(marker in title for marker in _ALBUM_MARKERS):
            continue
        titles.append(title)
    return titles


def iter_year_records(
    year: int,
    max_pages: int = MAX_PAGES_PER_YEAR,
) -> Iterator[Tuple[int, str, str]]:
    """1 年ぶんの ``(year, title, artist)`` を順に返す。

    重複（``song_key`` 単位）はこの関数内で落とす。
    """
    seen: set = set()
    for page in range(max_pages):
        time.sleep(REQUEST_INTERVAL_SECONDS)
        try:
            payload = _fetch(year=year, offset=page * PAGE_SIZE)
        except (urllib.error.URLError, OSError) as exc:
            print(f"  WARN {year} 年 page{page}: {exc}", file=sys.stderr)
            return

        releases = payload.get("releases") or []
        if not releases:
            return

        for release in releases:
            if release.get("status") in REJECTED_STATUS:
                continue
            # 分裂盤・ミックスはトラック数が 10 を超える。曲ではない。
            try:
                if int(release.get("track-count") or 0) > 10:
                    continue
            except (TypeError, ValueError):
                pass
            artist = _artist_of(release)
            if not artist or len(artist.strip()) > 40:
                continue
            for title in _usable_titles(str(release.get("title", ""))):
                key = song_key(title, artist)
                if key in seen:
                    continue
                seen.add(key)
                yield year, title, artist

        if len(releases) < PAGE_SIZE:
            return


def build(
    min_year: int,
    max_year: int,
    target: int,
    max_pages: int,
    out_path: Path,
    resume: bool = False,
) -> List[Dict[str, str]]:
    """全年の候補を ``target`` 件/年で組み立て、**年ごとに逐次書き出す**。

    逐次書き出し的理由: 全体で 76 年 × 2 ページ × 1.1 秒 ≈ 3 分かかる
    ネットワーク処理なので、途中でセッションが中断しても取得済みの年が
    失われない。``resume=True`` なら既存の出力から年を読み取り、
    取得済みの年をスキップする。
    """
    rows: List[Dict[str, str]] = []
    done_years: set = set()

    if resume and out_path.exists():
        for line in out_path.read_text(encoding="utf-8-sig").splitlines():
            parts = line.split("\t")
            if len(parts) < 3 or parts[0].strip() == "release_year":
                continue
            if not parts[0].strip().isdigit():
                continue
            year = int(parts[0].strip())
            done_years.add(year)
            rows.append({
                "release_year": parts[0].strip(),
                "title": parts[1],
                "artist": parts[2],
                "genre": parts[3] if len(parts) > 3 else "",
                "note_ja": parts[4] if len(parts) > 4 else "",
            })
        print(f"  resume: {len(done_years)} 年を読み込み済み", file=sys.stderr)

    if not rows:
        write_tsv(out_path, [], header_only=True)

    for year in range(min_year, max_year + 1):
        if year in done_years:
            print(f"  {year}: skip（取得済み）", file=sys.stderr)
            continue
        picked = 0
        seen: set = set()
        for row_year, title, artist in iter_year_records(year, max_pages=max_pages):
            key = song_key(title, artist)
            if key in seen:
                continue
            seen.add(key)
            row = {
                "release_year": str(row_year),
                "title": title,
                "artist": artist,
                "genre": "",
                "note_ja": "",
            }
            rows.append(row)
            append_tsv(out_path, row)
            picked += 1
            if picked >= target:
                break
        print(f"  {year}: {picked} 曲", file=sys.stderr)
    return rows


def append_tsv(path: Path, row: Dict[str, str]) -> None:
    """候補を 1 行追記する（ヘッダは既に、ある前提）。"""
    with path.open("a", encoding="utf-8") as fh:
        fh.write("\t".join([
            row["release_year"],
            row["title"],
            row["artist"],
            row.get("genre", ""),
            row.get("note_ja", ""),
            "unverified",
            "musicbrainz:release-search:date+lang-jpn+single",
        ]) + "\n")
        fh.flush()


def write_tsv(path: Path, rows: List[Dict[str, str]], header_only: bool = False) -> None:
    """候補 TSV を書き出す（ヘッダつき）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    header = "release_year\ttitle\tartist\tgenre\tnote_ja\tconfidence\tsource"
    lines = [header]
    if header_only:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return
    for row in rows:
        lines.append("\t".join([
            row["release_year"],
            row["title"],
            row["artist"],
            row.get("genre", ""),
            row.get("note_ja", ""),
            "unverified",
            "musicbrainz:release-search:date+lang-jpn+single",
        ]))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="MusicBrainz から年別の曲カタログ候補を生成する"
    )
    parser.add_argument("--min-year", type=int, default=1950)
    parser.add_argument("--max-year", type=int, default=2025)
    parser.add_argument("--target", type=int, default=50, help="1 年あたりの候補数")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES_PER_YEAR)
    parser.add_argument("--out", type=Path, default=OUTPUT_TSV)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="既存の出力から取得済みの年を読み取り、不足分だけ取る",
    )
    args = parser.parse_args(argv)

    print(
        f"{args.min_year}-{args.max_year} を {args.target} 曲/年 で生成します"
        f"（1リクエスト {REQUEST_INTERVAL_SECONDS} 秒間隔）",
        file=sys.stderr,
    )
    rows = build(
        args.min_year, args.max_year, args.target, args.max_pages, args.out,
        resume=args.resume,
    )

    per_year: Dict[int, int] = {}
    for row in rows:
        y = int(row["release_year"])
        per_year[y] = per_year.get(y, 0) + 1
    print(f"合計 {len(rows)} 件 / {len(per_year)} 年 -> {args.out}", file=sys.stderr)
    short = sorted(y for y, c in per_year.items() if c < args.target)
    if short:
        print(f"目標未達の年 ({len(short)}): {short}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
