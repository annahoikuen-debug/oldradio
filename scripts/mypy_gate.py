"""mypy ゲート（既存違反ベースライン方式）。

mypy はこのコードベースに 150 件を超える既存の型違反を持ったまま
段階的に改善していく方針（`.github/flake8-app-baseline.ini` の
flake8 と同じ考え方）。全件をその場で直すのは本タスクの範囲外なので、

- `.github/mypy-baseline.json` に**現在の違反**を許容リストとして持つ
- ゲートは「ベースラインに無い**新規違反**」だけで失敗する
- ベースラインから消えた項目（＝修正された違反）は警告のみ
  （修正を失敗させない。行番号ではなく file|code|message で照合する
   ので、ファイル内の行移動でもベースラインが暴走しない）

使い方::

    python scripts/mypy_gate.py            # ゲート実行（CI はこれ）
    python scripts/mypy_gate.py --update   # ベースラインを再生成

mypy のバージョンがメッセージ文言を変えると新規違反として検出される。
その場合は `requirements-dev.txt` の mypy を固定したうえで
`--update` し、差分をレビューする。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Set

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_PATH = REPO_ROOT / ".github" / "mypy-baseline.json"

#: CI と同一の対象・フラグ。`--explicit-package-bases` は
#: `scripts/validate_facts.py` が「パス直接指定」と `from scripts.validate_facts`
#: の両方で見つかりモジュール名が二重になるのを防ぐ。
MYPY_ARGS = [
    "mypy",
    "--explicit-package-bases",
    "--ignore-missing-imports",
    "--no-error-summary",
    "--output=json",
    "retro_radio",
    "eval",
    "scripts",
    "utils",
    "db",
]


def _key(entry: Dict[str, object]) -> str:
    """照合キー。パス区切りを正規化し、行番号は含めない。"""
    file = str(entry.get("file", "")).replace("\\", "/")
    return f"{file}|{entry.get('code', '')}|{entry.get('message', '')}"


def run_mypy() -> List[Dict[str, object]]:
    proc = subprocess.run(
        MYPY_ARGS,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    # mypy の終了コード: 0=エラー無し, 1=型エラー, 2+=内部エラーなど。
    # 型エラーの有無はベースライン側で判定するため、2 以上だけ実行失敗。
    if proc.returncode >= 2:
        sys.stderr.write(proc.stderr)
        raise SystemExit(f"mypy が異常終了しました (exit {proc.returncode})")
    entries: List[Dict[str, object]] = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        entry = json.loads(line)
        if entry.get("severity") == "error":
            entries.append(entry)
    return entries


def load_baseline() -> Set[str]:
    if not BASELINE_PATH.exists():
        return set()
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return {str(item) for item in data}


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update",
        action="store_true",
        help="現在の違反でベースラインを上書きする",
    )
    args = parser.parse_args(argv)

    entries = run_mypy()
    keys = sorted({_key(entry) for entry in entries})

    if args.update:
        BASELINE_PATH.write_text(
            json.dumps(keys, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )
        print(f"ベースラインを更新しました: {len(keys)} 件 -> {BASELINE_PATH}")
        return 0

    baseline = load_baseline()
    current = set(keys)
    new_errors = sorted(current - baseline)
    stale = sorted(baseline - current)

    if stale:
        print(
            f"注意: ベースラインに残るが現在は消えている項目が {len(stale)} 件"
            "あります（修正済みなら --update で掃除してください）:"
        )
        for item in stale:
            print(f"  STALE {item}")

    if new_errors:
        print(f"新規の型エラーが {len(new_errors)} 件あります:")
        for item in new_errors:
            print(f"  NEW {item}")
        print(
            "既存違反を直すか、意図的な場合は `python scripts/mypy_gate.py --update` "
            "でベースラインに追加してください。"
        )
        return 1

    print(f"mypy ゲート OK（新規違反 0 / 現在 {len(current)} 件はベースライン内）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
