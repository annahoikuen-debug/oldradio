"""曲カタログの CI ゲートと iTunes 照合。

2 つの役割を持つ。

1. ``validate_all()`` … **ネットワーク不要**の静的検査。CI で必ず走る。
   ``python scripts/validate_songs.py`` で fail があれば終了コード 1。
2. ``--verify`` … iTunes Search API を叩いて曲名・アーティストを
   実データと照合する。**ネットワークが要るので既定では走らない**。
   ``python scripts/validate_songs.py --verify --year 1975`` のように
   1 年ずつ直していく運用を想定している。

静的検査の fail / warn
---------------------
fail（1 件でも出たら終了コード 1）

* ``missing-field``      必須フィールドの欠落
* ``bad-confidence``    許可値以外の ``confidence``
* ``duplicate-id``      レコード id の重複
* ``duplicate-song``    正規化後に同じ「曲名 + アーティスト」が重複
* ``missing-source``    ``source`` 欠落（URL の捏造をしないため必須）
* ``out-of-range-year`` ``min_year``〜``max_year`` の範囲外
* ``tsv-drift``         ``songs.tsv`` と ``songs.json`` が同期していない

warn（報告するが終了コードは 0）

* ``unverified``        一次文献を照合していない（現状ほぼ全部）
* ``thin-year``         1 年の曲数が目標未満
* ``missing-year``      1 曲も無い年
* ``itunes-miss``       iTunes に一致する音源が無い（``--verify`` のみ）
* ``itunes-wrong``      iTunes に「近いが別」の結果しか無い（``--verify`` のみ）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts._console import force_utf8_stdio  # noqa: E402
from retro_radio.core.songs import (  # noqa: E402
    REGISTRY_FILES,
    REQUIRED_FIELDS,
    SONGS_DIR,
    TARGET_SONGS_PER_YEAR,
    normalize_song_text,
    song_key,
)

ALLOWED_CONFIDENCE = ("verified", "unverified")
DEFAULT_MIN_YEAR = 1950
DEFAULT_MAX_YEAR = 2025


def _finding(code: str, message: str, **extra: Any) -> Dict[str, Any]:
    finding: Dict[str, Any] = {"code": code, "message": message}
    finding.update(extra)
    return finding


# --------------------------------------------------------------------------- #
# 静的検査
# --------------------------------------------------------------------------- #
def load_records() -> List[Dict[str, Any]]:
    """正本 JSON からレコードを読み込む（生。ローダの検証を通さない）。"""
    records: List[Dict[str, Any]] = []
    for name in REGISTRY_FILES:
        path = SONGS_DIR / name
        payload = json.loads(path.read_text(encoding="utf-8"))
        raw = payload.get("records") if isinstance(payload, dict) else payload
        if not isinstance(raw, list):
            raise ValueError(f"曲カタログの形式が不正です: {path}")
        records.extend(raw)
    return records


def check_structure(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """必須フィールド・型・``confidence``・``source`` を検査する。"""
    findings: List[Dict[str, Any]] = []
    for record in records:
        ident = record.get("id") if isinstance(record, dict) else None

        if not isinstance(record, dict):
            findings.append(_finding("missing-field", "レコードがオブジェクトでない", id=ident))
            continue

        for field in REQUIRED_FIELDS:
            if record.get(field) in (None, ""):
                code = "missing-source" if field == "source" else "missing-field"
                findings.append(
                    _finding(code, f"必須フィールド {field} が無い", id=ident, field=field)
                )

        if record.get("confidence") not in ALLOWED_CONFIDENCE:
            findings.append(
                _finding(
                    "bad-confidence",
                    f"confidence が不正: {record.get('confidence')!r}",
                    id=ident,
                )
            )

        for field in ("release_year", "rank"):
            if not isinstance(record.get(field), int):
                findings.append(
                    _finding(
                        "missing-field",
                        f"{field} が整数でない: {record.get(field)!r}",
                        id=ident,
                    )
                )
    return findings


def check_duplicates(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """id の重複と、正規化後に同じ曲の重複を検出する。"""
    findings: List[Dict[str, Any]] = []
    by_id: Dict[str, int] = {}
    by_song: Dict[str, str] = {}

    for record in records:
        if not isinstance(record, dict):
            continue
        ident = str(record.get("id", ""))
        if ident in by_id:
            findings.append(
                _finding("duplicate-id", f"id が重複: {ident}", id=ident, first_line=by_id[ident])
            )
        else:
            by_id[ident] = 0

        key = song_key(str(record.get("title", "")), str(record.get("artist", "")))
        if key in by_song:
            findings.append(
                _finding(
                    "duplicate-song",
                    "同じ「曲名 + アーティスト」が重複: "
                    f"{record.get('title')}（{record.get('artist')}）",
                    id=ident,
                    other_id=by_song[key],
                )
            )
        else:
            by_song[key] = ident
    return findings


def check_year_range(
    records: List[Dict[str, Any]], min_year: int, max_year: int
) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for record in records:
        year = record.get("release_year")
        if not isinstance(year, int):
            continue
        if year < min_year or year > max_year:
            findings.append(
                _finding(
                    "out-of-range-year",
                    f"release_year {year} が範囲外（{min_year}-{max_year}）",
                    id=record.get("id"),
                    year=year,
                )
            )
    return findings


def check_coverage(
    records: List[Dict[str, Any]],
    min_year: int,
    max_year: int,
    target: int,
) -> List[Dict[str, Any]]:
    """年の過不足を **warn** で報告する（fail にはしない）。

    曲数が足りないことは「アプリが壊れる」ことではない。ローテーションの
    周期が短くなるだけで、選曲ロジックの正しさは保たれる。
    """
    counts: Dict[int, int] = {}
    for record in records:
        year = record.get("release_year")
        if isinstance(year, int):
            counts[year] = counts.get(year, 0) + 1

    findings: List[Dict[str, Any]] = []
    for year in range(min_year, max_year + 1):
        count = counts.get(year, 0)
        if count == 0:
            findings.append(
                _finding("missing-year", f"{year} 年の曲が無い", year=year, count=0)
            )
        elif count < target:
            findings.append(
                _finding(
                    "thin-year",
                    f"{year} 年は {count} 曲（目標 {target} 曲）",
                    year=year,
                    count=count,
                    target=target,
                )
            )
    return findings


def check_unverified(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    findings: List[Dict[str, Any]] = []
    for record in records:
        if record.get("confidence") != "verified":
            findings.append(
                _finding(
                    "unverified",
                    f"一次文献未照合: {record.get('title')}（{record.get('artist')}）",
                    id=record.get("id"),
                )
            )
    return findings


def check_tsv_sync() -> List[Dict[str, Any]]:
    """``songs.tsv`` と ``songs.json`` の同期を見る。

    JSON は TSV から生成されるため、TSV だけ直して再生成し忘れると
    編集内容がアプリに届かない。CI で落とす。
    """
    script = REPO_ROOT / "scripts" / "build_song_catalog.py"
    if not script.exists():
        return []
    from scripts.build_song_catalog import (  # noqa: PLC0415

        SOURCE_TSV,
        parse_tsv,
        render_json,
    )

    if not SOURCE_TSV.exists():
        return [_finding("tsv-drift", f"編集用 TSV が無い: {SOURCE_TSV}")]
    try:
        rendered = render_json(parse_tsv(SOURCE_TSV))
    except ValueError as exc:
        return [_finding("tsv-drift", f"TSV を解釈できません: {exc}")]

    current = (REPO_ROOT / "retro_radio" / "core" / "songs" / "songs.json").read_text(
        encoding="utf-8"
    )
    if current != rendered:
        return [
            _finding(
                "tsv-drift",
                "songs.json が songs.tsv と同期していません。"
                "`python scripts/build_song_catalog.py` を実行してください。",
            )
        ]
    return []


# --------------------------------------------------------------------------- #
# iTunes 照合（--verify）
# --------------------------------------------------------------------------- #
def verify_against_itunes(
    records: List[Dict[str, Any]],
    years: Optional[List[int]] = None,
    delay: float = 0.35,
) -> List[Dict[str, Any]]:
    """iTunes で曲名・アーティストが実在するかを確認する。

    iTunes の ``releaseDate`` は配信日であり発表年ではないため、
    **年の照合はしない**（年の一致は正本の責務）。
    照合するのは「その曲名とアーティストの組合せが実在するか」だけ。

    ``--verify`` を付けずに実行された場合は何もしない（ネットワーク不要）。
    """
    if not years:
        return []

    from retro_radio.core.preview_resolver import _pick_matching, _search_itunes

    findings: List[Dict[str, Any]] = []
    import time  # noqa: PLC0415

    for record in records:
        if record.get("release_year") not in years:
            continue
        title = str(record.get("title", ""))
        artist = str(record.get("artist", ""))
        results = _search_itunes(f"{title} {artist}")
        matched = _pick_matching(results, title, artist)

        if matched:
            continue

        # 曲名だけでも一致する結果があれば「アーティストが違う」可能性が高い。
        wanted = normalize_song_text(title)
        near = [
            item
            for item in results
            if normalize_song_text(str(item.get("trackName", ""))) == wanted
        ]
        if near:
            found = "、".join(
                f"{item.get('trackName')}（{item.get('artistName')}）" for item in near[:3]
            )
            findings.append(
                _finding(
                    "itunes-wrong",
                    f"曲名は合うがアーティストが違う: 「{title}」/ iTunes: {found}",
                    id=record.get("id"),
                    year=record.get("release_year"),
                )
            )
        else:
            findings.append(
                _finding(
                    "itunes-miss",
                    f"iTunes に音源が無い: 「{title}」（{artist}）",
                    id=record.get("id"),
                    year=record.get("release_year"),
                )
            )
        time.sleep(delay)
    return findings


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #
def validate_all(
    min_year: int = DEFAULT_MIN_YEAR,
    max_year: int = DEFAULT_MAX_YEAR,
    target: int = TARGET_SONGS_PER_YEAR,
    check_tsv: bool = True,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """静的検査を実行して ``(fail, warn)`` を返す。"""
    records = load_records()

    fails: List[Dict[str, Any]] = []
    fails += check_structure(records)
    fails += check_duplicates(records)
    fails += check_year_range(records, min_year, max_year)
    if check_tsv:
        fails += check_tsv_sync()

    warns: List[Dict[str, Any]] = []
    warns += check_unverified(records)
    warns += check_coverage(records, min_year, max_year, target)
    return fails, warns


def _print_grouped(findings: List[Dict[str, Any]], stream: Any) -> None:
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for finding in findings:
        grouped.setdefault(finding["code"], []).append(finding)
    for code in sorted(grouped):
        items = grouped[code]
        print(f"\n[{code}] {len(items)} 件", file=stream)
        limit = 40 if code not in ("unverified", "thin-year", "missing-year") else 200
        for finding in items[:limit]:
            print(f"  - {finding['message']}", file=stream)
        if len(items) > limit:
            print(f"  ... 他 {len(items) - limit} 件", file=stream)


def main(argv: Optional[List[str]] = None) -> int:
    force_utf8_stdio()
    parser = argparse.ArgumentParser(description="曲カタログを検証する")
    parser.add_argument("--json", action="store_true", help="機械向け JSON で出力する")
    parser.add_argument("--quiet", action="store_true", help="warn の内訳だけ表示する")
    parser.add_argument("--min-year", type=int, default=DEFAULT_MIN_YEAR)
    parser.add_argument("--max-year", type=int, default=DEFAULT_MAX_YEAR)
    parser.add_argument("--target", type=int, default=TARGET_SONGS_PER_YEAR)
    parser.add_argument(
        "--verify",
        action="store_true",
        help="iTunes を実際に叩いて曲名・アーティストを照合する（低速）",
    )
    parser.add_argument(
        "--year",
        type=int,
        action="append",
        dest="years",
        help="--verify で照合する年（複数指定可）",
    )
    args = parser.parse_args(argv)

    fails, warns = validate_all(
        min_year=args.min_year, max_year=args.max_year, target=args.target
    )

    if args.verify:
        records = load_records()
        years = args.years or list(range(args.min_year, args.max_year + 1))
        warns += verify_against_itunes(records, years=years)

    if args.json:
        print(
            json.dumps(
                {"fail": fails, "warn": warns},
                ensure_ascii=False,
                indent=2,
            )
        )
        return 1 if fails else 0

    print(f"fail: {len(fails)} 件 / warn: {len(warns)} 件")
    if not args.quiet:
        _print_grouped(fails, sys.stdout)
        _print_grouped(warns, sys.stdout)
    else:
        counts: Dict[str, int] = {}
        for finding in warns:
            counts[finding["code"]] = counts.get(finding["code"], 0) + 1
        for code in sorted(counts):
            print(f"  {code}: {counts[code]}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
