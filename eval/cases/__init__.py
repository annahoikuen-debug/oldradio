"""評価ケース（層別抽出 24 ケース）。

## なぜ 24 ケースか

全 76 年 × 3 モード = 228 ケースを毎 CI で回す必要はない。
提案 465 行の指定どおり、**層別抽出**として 8 年 × 3 モード = 24 ケースを
nightly で回す。抽出する 8 年は**各 10 年バケットの代表**かつ
**バケットの端（1950）と最新（2025）**を含む:

    1950 / 1964 / 1975 / 1985 / 1995 / 2005 / 2015 / 2025

- 1950: 対応範囲の始点。バケットの端（1950 バケットが唯一の実年を持つ）。
- 1964: 1960 バケットの中。
- 1975: ``config.default_year``。最も多く使われる既定年。
- 1985 / 1995 / 2005 / 2015: 各バケットの中。
- 2025: バケットが専用（2020 バケットと別キー）。

## 正本から生成する（ハードコードしない）

``case_defs.json`` は**生成物**であり、**正本は
``retro_radio/core/facts/programs.json``（S1 所有）**である。
``build_cases.py`` が ``scripts.validate_facts.build_fact_table(year)`` を使って
``expected_facts`` を生成する。手で書いた ``expected_facts`` は
「その年の事実ではないのに正解として扱われる」危険を含むため置かない。

```bash
python -m eval.cases.build_cases            # case_defs.json を再生成
python -m eval.cases.build_cases --check    # 冪等性（再生成しても差分ゼロか）
```

## ケースのフィールド

| フィールド | 意味 | 生成源 |
|---|---|---|
| ``id`` | ``{mode}-{year}``。一意 | 決定的 |
| ``year`` | 対象年 | 決定的 |
| ``mode`` | ``normal`` / ``care_recreation`` / ``anniversary`` | 決定的 |
| ``expected_facts`` | その年に有効な事実の ``id`` 一覧 | ``build_fact_table(year)`` |
| ``expected_song_count`` | 1 番組で流す曲の下限 | ``config.program_min_song_count`` |
| ``expected_segments`` | 期待される ``###`` 見出し名 | ``_build_segmented_prompt``（読み取りのみ） |

``expected_segments`` は ``retro_radio/core/script_generator.py`` の
``_build_segmented_prompt``（**S1/S3 所有・読み取りのみ**）から導出する。
プロンプトを変えれば自動的に追従するため、S3 がセグメントを増やしても
ケース定義を書き直す必要はない。
"""

from __future__ import annotations

from .loader import CASE_DEFS_PATH, load_cases, load_case

__all__ = ["CASE_DEFS_PATH", "load_cases", "load_case"]
