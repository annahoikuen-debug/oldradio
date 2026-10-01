"""候補曲リストを iTunes で照合し、採用分だけを正本 TSV へ合流させる。

なぜこのスクリプトが要るのか
----------------------------
``scripts/validate_songs.py --verify`` は「年 1 つぶんの候補を 1 件ずつ
逐次照合して**報告だけ**する」.verify だ。3800 曲候補をこれで回すと
逐次 HTTP  Because 1 曲 = 2 検索語 = 2 リクエストで数時間かかるうえ、
結果は warn として出るだけで**採用された曲しか正本に入らない**。

このスクリプトは同じ照合を並列で回し、**採用曲と棄却曲でファイルを分ける**:

* 採用（iTunes に ``previewUrl`` のある実在曲） → ``songs.tsv`` へ追記
* 棄却（iTunes に無い = でっち上げの疑い、または音源が無い）
  → ``songs_rejected.tsv`` に理由付きで残す。**捨てるのではなく保留**する。
  年ごとの曲数が目標に届いていない年の穴を後で埋める材料になる。

**iTunes を「年」の供給源にはしない。** 実測で iTunes Search API は
検索語に年を入れても年を無視する（1975 年を搜すと 2019-2024 年の曲しか
返らない）。ここで使うのは「その曲名とアーティストの組合せが実在し、
プレビュー音源があるか」だけ。年の正しさは候補リスト側の責務であり、
iTunes の ``releaseDate`` は配信日なので照合に使いません。

このフィルタは副次的にハルシネーション潰し DATABASE として働きます。
でっち上げた曲名は iTunes に絶対に出ないため、棄却側に落ちます。

使い方
------
    # 候補 TSV を照合して採用/棄却に振り分ける（正本には書き込まない）
    python scripts/import_songs.py --candidates scripts/song_source/candidates.tsv \\
        --out-accepted scripts/song_source/accepted.tsv \\
        --out-rejected scripts/song_source/rejected.tsv

    # 採用分を正本 songs.tsv へ合流して songs.json を再生成する
    python scripts/import_songs.py --merge

    # ネットワークを使わず、振り分け結果だけ確認する
    python scripts/import_songs.py --report
"""

from __future__ import annotations

import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from retro_radio.core.songs import song_key  # noqa: E402

# 一次文献を照合していない曲に付ける出典欄の値（build_song_catalog.py と同値）。
UNVERIFIED_SOURCE = "unverified:primary-source-pending"

#: 照合結果として使う判定。
ACCEPT = "accept"
REJECT_MISS = "itunes-miss"
REJECT_WRONG_ARTIST = "itunes-wrong"

#: 採用側 TSV の列。build_song_catalog.py の parse_tsv と互換。
ACCEPTED_COLUMNS = ("release_year", "title", "artist", "genre", "note_ja", "confidence", "source")

#: 棄却側 TSV の列。理由を残すため 1 つ多い。
REJECTED_COLUMNS = ("release_year", "title", "artist", "genre", "reason", "detail")


# --------------------------------------------------------------------------- #
# 候補の読み書き
# --------------------------------------------------------------------------- #
def read_candidates(path: Path) -> List[Dict[str, str]]:
    """候補 TSV を読む。``#`` 行と空行は無視する。

    列は ``release_year / title / artist / genre / note_ja``。年・曲名・
    アーティストの 3 列は必須。
    """
    if not path.exists():
        raise FileNotFoundError(f"候補 TSV がありません: {path}")

    out: List[Dict[str, str]] = []
    # ``utf-8-sig`` は BOM つきの TSV（Excel や PowerShell の
    # ``Out-File -Encoding utf8`` が書くもの）でもそのまま読める。
    for lineno, raw in enumerate(
        path.read_text(encoding="utf-8-sig").splitlines(), start=1
    ):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        # ヘッダ行（`release_year	title	artist`）があれば読み飛ばす。
        # 生成済みの accepted.tsv / rejected.tsv をそのまま --candidates に
        # 渡せるようにするため。
        if raw.split("\t")[0].strip() == "release_year":
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            raise ValueError(f"{path}:{lineno}: 列が足りません（年/曲名/アーティスト）: {raw!r}")
        parts += [""] * (5 - len(parts))
        year_s, title, artist, genre, note = parts[:5]
        if not year_s.strip().isdigit():
            raise ValueError(f"{path}:{lineno}: release_year が整数でない: {year_s!r}")
        if not title.strip() or not artist.strip():
            raise ValueError(f"{path}:{lineno}: 曲名・アーティストが空です: {raw!r}")
        out.append({
            "release_year": year_s.strip(),
            "title": title.strip(),
            "artist": artist.strip(),
            "genre": genre.strip(),
            "note_ja": note.strip(),
        })
    return out


def _write_rows(path: Path, columns: Tuple[str, ...], rows: List[Dict[str, str]]) -> None:
    """TSV を書き出す（列順は決まっているので dict 順は不要）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["\t".join(columns)]
    for row in rows:
        lines.append("\t".join(str(row.get(col, "")).replace("\t", " ") for col in columns))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# iTunes 照合（並列）
# --------------------------------------------------------------------------- #
def verify_one(candidate: Dict[str, str]) -> Dict[str, str]:
    """1 曲分を iTunes で照合して、判定 Reason 付きの行を返す。

    ``scripts/validate_songs.py --verify`` と同じ判定を使う
    （``_pick_matching`` は「曲名とアーティストが両方正規化後一致する
    候補」だけを返す）。その上で **previewUrl の有無**も見る。

    音源が無い (``previewUrl`` が無い) ものは棄却する。理由: このアプリは
    音源が無い曲を「間奏」として扱うため、1 番組の 6 スロットが埋まらず、
    ログに「可聴な曲だけでは 1 パスの 6 スロットが埋まらないため、
    残りを循環で埋めた」が出る。**鳴らない.oauth曲 Arroyo カタログに
    入れる意味がない**ため。

    Returns
    -------
    dict
        候補の列に ``reason`` と ``detail`` を足した行。
        ``reason`` は :data:`ACCEPT` / :data:`REJECT_MISS` /
        :data:`REJECT_WRONG_ARTIST` のいずれか。
    """
    from retro_radio.core.preview_resolver import (
        PreviewTransportError,
        _fetch_itunes,
        _pick_matching,
        _reset_breaker,
    )
    from retro_radio.core.songs import normalize_song_text

    # 批量作業では 1 曲ごとに「本当に 1 回試す」必要がある。サーキット
    # ブレーカーは実運用で時間予算を守るためのもので、逐次 import では
    # 3 件目以降が HTTP を出さずに「省略」され、全件が
    # itunes-unreachable になる（実測）。よって毎回リセットする。
    _reset_breaker()

    title = candidate["title"]
    artist = candidate["artist"]
    row = dict(candidate)
    row["reason"] = REJECT_MISS
    row["detail"] = ""

    # 検索語は 2 通り。曲名が一般名詞のときはアーティスト名が入った方が上位に来る。
    matched = None
    for term in (f"{title} {artist}", f"{artist} {title}"):
        try:
            results = _fetch_itunes(term)
        except PreviewTransportError as exc:
            # 到達不能は「曲が無い」ではない。ここで棄却すると outages 中に
            # まとめて落ちてしまう。保留として reason を分ける。
            row["reason"] = "itunes-unreachable"
            row["detail"] = str(exc)
            return row
        matched = _pick_matching(results, title, artist)
        if matched:
            break

    if not matched:
        # 曲名だけが合う結果があれば「アーティストが違う」可能性が高い。
        wanted = normalize_song_text(title)
        near = [
            item
            for item in results
            if normalize_song_text(str(item.get("trackName", ""))) == wanted
        ]
        if near:
            row["reason"] = REJECT_WRONG_ARTIST
            found = "、".join(
                f"{item.get('trackName')}（{item.get('artistName')}）" for item in near[:3]
            )
            row["detail"] = f"iTunes: {found}"
        else:
            row["detail"] = "iTunes に音源が無い"
        return row

    if not matched.get("previewUrl"):
        row["reason"] = "itunes-no-preview"
        row["detail"] = "一致はするがプレビュー音源が無い（間奏になるため不採用）"
        return row

    row["reason"] = ACCEPT
    row["detail"] = ""
    return row


def verify_all(
    candidates: List[Dict[str, str]],
    workers: int = 1,
    delay: float = 1.0,
) -> List[Dict[str, str]]:
    """候補を照合して、判定 Reason 付きの行を全件返す。

    Parameters
    ----------
    workers:
        並列数。**既定は 1（逐次）**。iTunes は同一 IP からの連打に
        403 Forbidden で拒む（実測: 並列 5 × 検索語 2 で約 100 リクエストの
        連打 → ブロック開始、45 秒後も 403 が継続）。並列度を上げると
        **実在する曲まで「音源なし」と判定される**ため、既定は安全側の 1。
    delay:
        1 曲あたりの sleep 秒数。逐次なら 1〜2 秒が実用下限。

    Notes
    -----
    途中でレート制限に撃たれても、候補は丢弃しない。`itunes-unreachable` は
    弃却側へ出て、`--report` で切り分けて再実行できる。
    """
    results: List[Dict[str, str]] = []

    if workers <= 1:
        for index, candidate in enumerate(candidates, start=1):
            results.append(verify_one(candidate))
            if index % 10 == 0 or index == len(candidates):
                print(f"  照合 {index}/{len(candidates)}", file=sys.stderr)
            if delay:
                time.sleep(delay)
        return results

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(verify_one, c): c for c in candidates}
        done = 0
        for future in as_completed(futures):
            results.append(future.result())
            done += 1
            if delay:
                time.sleep(delay)
            if done % 10 == 0 or done == len(candidates):
                print(f"  照合 {done}/{len(candidates)}", file=sys.stderr)
    return results


# --------------------------------------------------------------------------- #
# 振り分け
# --------------------------------------------------------------------------- #
def split_results(rows: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], List[Dict[str, str]]]:
    """照合結果を受用側と棄却側に分ける。同(year, title, artist) は先用優先。"""
    accepted: List[Dict[str, str]] = []
    rejected: List[Dict[str, str]] = []
    seen: set = set()

    # 結果を元の候補順に並べる（並列実行の完了順は不定なので）。
    order = {song_key(r["title"], r["artist"]): i for i, r in enumerate(rows)}
    rows = sorted(rows, key=lambda r: order.get(song_key(r["title"], r["artist"]), 0))

    for row in rows:
        key = song_key(row["title"], row["artist"])
        if key in seen:
            row["reason"] = "duplicate"
            row["detail"] = "同一ファイル内で重複"
            rejected.append(row)
            continue
        seen.add(key)
        if row["reason"] == ACCEPT:
            accepted.append(row)
        else:
            rejected.append(row)
    return accepted, rejected


def summarize(rows: List[Dict[str, str]]) -> Dict[str, int]:
    """reason ごとの件数。"""
    counts: Dict[str, int] = {}
    for row in rows:
        counts[row["reason"]] = counts.get(row["reason"], 0) + 1
    return dict(sorted(counts.items()))


# --------------------------------------------------------------------------- #
# 採用分の正本合流
# --------------------------------------------------------------------------- #
def merge_into_source(accepted_path: Path, source_tsv: Path) -> int:
    """採用 TSV を正本 ``songs.tsv`` へ追記する（既存の重複は飛ばす）。

    正本側は「年 + rank」が密でなければならないため、``.tsv`` に
    # 正本側は「年 + rank」が密でなければならないため、``.tsv`` に

    Returns
    -------
    int
        実際に追記した行数。
    """
    if not accepted_path.exists():
        raise FileNotFoundError(f"採用 TSV がありません: {accepted_path}")
    if not source_tsv.exists():
        raise FileNotFoundError(f"正本 TSV がありません: {source_tsv}")

    existing_keys = set()
    for line in source_tsv.read_text(encoding="utf-8-sig").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        if parts[0].strip() == "release_year":
            continue
        existing_keys.add(song_key(parts[1], parts[2]))

    # 年ごとにまとめて追記する（rank は生成側で振り直されるためbegingroup順は不問）。
    accepted = read_candidates(accepted_path)
    accepted.sort(key=lambda c: (int(c["release_year"]), c["title"]))

    added_lines: List[str] = []
    added = 0
    for candidate in accepted:
        key = song_key(candidate["title"], candidate["artist"])
        if key in existing_keys:
            continue
        existing_keys.add(key)
        added_lines.append(
            "\t".join([
                candidate["release_year"],
                candidate["title"],
                candidate["artist"],
                candidate.get("genre", ""),
            ])
        )
        added += 1

    if added_lines:
        with source_tsv.open("a", encoding="utf-8") as fh:
            if not source_tsv.read_text(encoding="utf-8").endswith("\n"):
                fh.write("\n")
            fh.write("\n".join(added_lines) + "\n")
    return added


# --------------------------------------------------------------------------- #
# エントリポイント
# --------------------------------------------------------------------------- #
def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="候補曲を iTunes で照合して正本に合流する")
    parser.add_argument("--candidates", type=Path, help="照合する候補 TSV")
    parser.add_argument("--out-accepted", type=Path, help="採用分の出力先 TSV")
    parser.add_argument("--out-rejected", type=Path, help="棄却分の出力先 TSV")
    parser.add_argument("--workers", type=int, default=8, help="並列数（既定8）")
    parser.add_argument("--delay", type=float, default=0.0, help="1曲あたりの sleep 秒")
    parser.add_argument("--merge", action="store_true", help="採用分を正本 songs.tsv へ合流する")
    parser.add_argument("--report", action="store_true", help="振り分け結果だけ表示する（ネットワーク不要）")
    args = parser.parse_args(argv)

    source_tsv = REPO_ROOT / "scripts" / "song_source" / "songs.tsv"
    accepted_path = args.out_accepted or (REPO_ROOT / "scripts" / "song_source" / "accepted.tsv")
    rejected_path = args.out_rejected or (REPO_ROOT / "scripts" / "song_source" / "rejected.tsv")

    if args.report:
        for path in (accepted_path, rejected_path):
            if not path.exists():
                print(f"{path.name}: まだありません")
                continue
            rows = read_candidates(path) if "accepted" in path.name else _read_rejected(path)
            counts: Dict[str, int] = {}
            for row in rows:
                counts[row.get("reason", "?")] = counts.get(row.get("reason", "?"), 0) + 1
            print(f"{path.name}: {len(rows)} 件 {counts}")
        return 0

    if args.merge:
        added = merge_into_source(accepted_path, source_tsv)
        print(f"正本 songs.tsv へ {added} 行追記しました: {source_tsv}")
        print("続けて `python scripts/build_song_catalog.py` で songs.json を再生成してください")
        return 0

    if not args.candidates:
        parser.error("--candidates / --merge / --report のいずれかが必要")

    try:
        candidates = read_candidates(args.candidates)
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    print(f"候補 {len(candidates)} 曲を iTunes で照合します（並列 {args.workers}）", file=sys.stderr)
    rows = verify_all(candidates, workers=args.workers, delay=args.delay)
    accepted, rejected = split_results(rows)

    _write_rows(accepted_path, ACCEPTED_COLUMNS, accepted)
    _write_rows(rejected_path, REJECTED_COLUMNS, rejected)

    print(f"採用 {len(accepted)} / 棄却 {len(rejected)} / 計 {len(candidates)}")
    print(f"採用理由: {summarize(rows)}")
    print(f"採用 -> {accepted_path}")
    print(f"棄却 -> {rejected_path}")
    return 0


def _read_rejected(path: Path) -> List[Dict[str, str]]:
    """棄却側 TSV を読む（reason/detail 列あり）。"""
    rows: List[Dict[str, str]] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip() or raw.lstrip().startswith("#") or lineno == 1:
            continue
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        parts += [""] * (len(REJECTED_COLUMNS) - len(parts))
        year, title, artist, genre, reason, detail = parts[: len(REJECTED_COLUMNS)]
        rows.append({
            "release_year": year, "title": title, "artist": artist,
            "genre": genre, "reason": reason, "detail": detail,
        })
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
