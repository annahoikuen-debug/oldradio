"""不足年の候補取得を、必要な曲数だけまとめて回す。

なぜこのスクリプトがあるか
--------------------------
``scripts/fetch_song_catalog.py`` は「1 年あたり target 曲」で年を
1 つずつ処理する。補充したい年が 1 つ増えるたびに ``--min-year`` と
``--max-year`` を調整して起動し直す必要があった。

ここでは正本の実測値から「あと何曲足りないか」を計算し、補充可能な年
だけを対象に必要な本数で取得する。対象年以外は 1 件も取らない。

1950-1966 年は MusicBrainz 側に実データが存在しない（実測: 1963 年は
27 曲、1955 年は 4 曲しか返らない）。対象に入れても 1 曲も増えず時間を
使うだけなので、対象から外す。

使い方
------
    python scripts/run_catalog_topup.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from retro_radio.core.songs import catalog_health  # noqa: E402
from scripts.fetch_song_catalog import FetchUnavailable, build  # noqa: E402

#: 候補が 1 曲でも増える見込みがある年だけを対象にする。
#:
#: MusicBrainz の `date:YYYY AND country:JP AND primarytype:Single` は
#: 1970 年以降で十分な件数を返す。1960 年台は 1 年 20-30 曲で頭打ちに
#: なる（実測）。この範囲を全部含めると、1 曲も増えないのに 30 分
#: ネットワークを使うことになる。
FIRST_FEASIBLE_YEAR = 1970

#: 採用率の下振れに備えて、不足分より多めに候補を取る。
#:
#: iTunes 照合では `previewUrl` が無いと採用しないため、候補の 6 割しか
#: 正本に入らない（実測の傾向）。足りなければ `--resume` で取り直す。
CANDIDATE_OVERSHOOT = 2


def plan() -> list:
    """``(年, 不足曲数)`` の並びを返す。"""
    health = catalog_health()
    coverage = health["coverage"]
    target = int(health["target"])
    known = sorted(coverage)
    if not known:
        return []
    rows = []
    for year in known:
        if year < FIRST_FEASIBLE_YEAR:
            continue
        missing = target - int(coverage[year])
        if missing > 0:
            rows.append((year, missing))
    return rows


def main() -> int:
    rows = plan()
    total_missing = sum(missing for _year, missing in rows)
    print(f"補充対象 {len(rows)} 年 / 不足 {total_missing} 曲", file=sys.stderr)
    for year, missing in rows:
        print(f"  {year}: {missing} 曲不足", file=sys.stderr)

    out_path = REPO_ROOT / "scripts" / "song_source" / "candidates_topup.tsv"
    failed: list = []
    for year, missing in rows:
        print(f"--- {year} を {missing} 曲不足分だけ取得 ---", file=sys.stderr)
        try:
            build(
                min_year=year,
                max_year=year,
                target=missing * CANDIDATE_OVERSHOOT,
                max_pages=5,
                out_path=out_path,
                resume=True,
            )
        except FetchUnavailable as exc:
            print(f"  FAIL {year}: {exc}", file=sys.stderr)
            failed.append(year)
    print(f"候補 -> {out_path}", file=sys.stderr)
    return 2 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
