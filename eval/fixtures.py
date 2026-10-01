"""台本の供給源（script source）。評価ハーネスを**閉路（hermetic）**にする。

## なぜ必要か

``python -m eval`` は ``retro_radio.core.script_generator.generate_radio_script``
を呼ぶ。その関数は ``GEMINI_API_KEY`` が設定されていれば**実 API を叩く**ため、
同じコマンドでも次の要因で結果が変わる:

1. ネットワークの有無・遅延・障害。
2. モデルのバージョン（``gemini_model`` の既定が変われば全 score が動く）。
3. 実行時刻（生成確率が揺れる）。
4. ローカルの API キー設定の有無。

CI はこれらを排除できないので、**既定を「決定的モード」にする**。既定の
``deterministic`` は ``_deterministic_script``（= フォールバック経路）を直接呼ぶ。
これは LLM を一切経由しないため、**ネットワークに触らず**、
**同じ入力なら常に同じ出力**になる。

## 3 つの供給源

| source | ネットワーク | 決定性 | 用途 |
|---|---|---|---|
| ``deterministic`` | 触らない | あり（完全） | **既定**。CI とローカル再現 |
| ``fixtures`` | 触らない | あり（固定データ） | 特定の原稿を評価したいとき |
| ``gemini`` | 触る | なし | 実サービス品質の評価（人手） |

``fixtures`` は ``{ケースID: 原稿}`` の JSON を読み込む。実運用の原稿を
保存しておけば、ネットワークなしで**その原稿**を評価できる。

## 環境変数

- ``EVAL_SCRIPT_SOURCE`` = ``deterministic`` | ``fixtures`` | ``gemini``
  （CLI オプション ``--script-source`` より**弱い**。明示指定があればそちらが勝つ）
- ``EVAL_FIXTURES`` = fixture JSON のパス（``source=fixtures`` のとき必須）

resolve_source はこの優先順位で決める。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:  # pragma: no cover - 直接実行時の保険
    sys.path.insert(0, str(_REPO_ROOT))

#: 供給源の名前。
SCRIPT_SOURCES: Tuple[str, ...] = ("deterministic", "fixtures", "gemini")

#: **既定**。ネットワークに一切触れない。
DEFAULT_SCRIPT_SOURCE = "deterministic"

#: 供給源を選ぶ環境変数。
SCRIPT_SOURCE_ENV = "EVAL_SCRIPT_SOURCE"

#: fixture のパスを指定する環境変数。
FIXTURES_ENV = "EVAL_FIXTURES"

#: ``gemini`` は非決定的かつネットワークに依存する経路であることの印。
NON_HERMETIC_SOURCE = "gemini"


class ScriptSourceError(RuntimeError):
    """供給源の指定が不正、または fixture が読めない。"""


def resolve_source(explicit: Optional[str] = None) -> str:
    """実際に使う供給源を返す。

    優先順位は **明示引数 > 環境変数 > :data:`DEFAULT_SCRIPT_SOURCE`**。

    Raises
    ------
    ScriptSourceError
        未知の供給源名を指定した場合。
    """
    candidate = explicit or os.environ.get(SCRIPT_SOURCE_ENV) or DEFAULT_SCRIPT_SOURCE
    normalized = str(candidate).strip().lower()
    if normalized not in SCRIPT_SOURCES:
        raise ScriptSourceError(
            f"未知の台本供給源です: {candidate!r}（{', '.join(SCRIPT_SOURCES)} のいずれか）"
        )
    return normalized


def resolve_fixture_path(explicit: Optional[Path] = None) -> Path:
    """fixture のパスを返す（``source=fixtures`` のときに必須）。

    Raises
    ------
    ScriptSourceError
        パスが与えられていない場合。
    """
    raw = explicit if explicit is not None else os.environ.get(FIXTURES_ENV)
    if not raw:
        raise ScriptSourceError(
            "source=fixtures には fixture ファイルが必要です。"
            f"--fixtures または環境変数 {FIXTURES_ENV} を指定してください。"
        )
    return Path(raw)


def load_fixture_scripts(path: Path) -> Dict[str, str]:
    """fixture JSON を ``{ケースID: 原稿}`` として読み込む。

    受け付ける形は 2 つ:

    - ``{"normal-1950": "原稿…", …}``（素のマップ）
    - ``{"cases": {"normal-1950": "原稿…"}}``（``case_defs.json`` と並べた形）

    Raises
    ------
    ScriptSourceError
        JSON が壊れている、または値が文字列でない場合。
    """
    target = Path(path)
    if not target.exists():
        raise ScriptSourceError(f"fixture が見つかりません: {target}")
    try:
        payload: Any = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ScriptSourceError(f"fixture の JSON が壊れています: {target}（{exc}）") from exc

    mapping: Mapping[str, Any] = payload.get("cases", {}) if isinstance(payload, dict) else {}
    if not isinstance(mapping, Mapping):
        raise ScriptSourceError(f"fixture の cases はオブジェクトである必要があります: {target}")

    scripts: Dict[str, str] = {}
    for case_id, text in mapping.items():
        if not isinstance(text, str):
            raise ScriptSourceError(
                f"fixture {target} の {case_id} は文字列である必要があります（実際: {type(text).__name__}）"
            )
        scripts[str(case_id)] = text
    if not scripts:
        raise ScriptSourceError(f"fixture に台本が 1 件もありません: {target}")
    return scripts


def deterministic_script(
    year: int,
    mode: str = "normal",
    *,
    month: int,
    day: int,
    target_name: Optional[str] = None,
) -> str:
    """**ネットワークを一切触れずに**原稿を生成する。

    ``retro_radio.core.script_generator._deterministic_script``（= フォールバック
    経路）を直接呼ぶ。``generate_radio_script`` を経由しないのは、同関数が
    ``GEMINI_API_KEY`` の有無で LLM 経路を選ぶため（＝既定を強制できないため）。
    """
    from retro_radio.core.script_generator import _catalog_allowlist, _deterministic_script

    # songs=None は「正本カタログから導出」を意味する。
    # 以前は validate_song_pairs(None) を使っていたが、同関数は None を返す契約で
    # `or []` により空リストになり、決定論スクリプトが内蔵の古いフォールバック曲名
    # （1951/1955 年の曲）を使っていた。_catalog_allowlist は正本カタログから
    # 対象年と近傍年の実在曲を返す。**release_year フィルタを緩和して
    # 近傍年（最大 10 年幅）の曲を借りるのは select_program_songs の
    # 設計どおり**（曲カタログに該当年の曲が無い年は空にしない方針）ため、
    # ここでさらに絞り込まない（1950 年は正本上 2 曲しか無く、絞り込むと
    # 同一曲の重複言及になる）。
    try:
        from retro_radio.core.songs import PROGRAM_SONGS_PER_BROADCAST
        count = PROGRAM_SONGS_PER_BROADCAST
    except Exception:  # noqa: BLE001 - 曲ストアが読めない場合は内蔵フォールバックへ縮退
        count = 0
    songs: List[Tuple[str, str]] = (
        _catalog_allowlist(year, count=count) if count > 0 else []
    )
    return _deterministic_script(year, month, day, mode, target_name, songs)


def gemini_script(
    year: int,
    mode: str = "normal",
    *,
    month: int,
    day: int,
    target_name: Optional[str] = None,
) -> str:
    """実サービス（Gemini）を経由して原稿を生成する。**非決定的**。

    評価ハーネスの**既定ではない**。この経路を使う場合は、結果がネットワークと
    モデルバージョンに依存することをレポートに記録すること。
    """
    from retro_radio.core.script_generator import generate_radio_script

    return generate_radio_script(year, month, day, mode=mode, target_name=target_name)


def build_script_for_source(
    source: str,
    year: int,
    mode: str = "normal",
    *,
    month: int,
    day: int,
    target_name: Optional[str] = None,
) -> str:
    """供給源名に応じて原稿を生成する。

    Raises
    ------
    ScriptSourceError
        未知の供給源名を指定した場合。
    """
    if source == "deterministic":
        return deterministic_script(year, mode, month=month, day=day, target_name=target_name)
    if source == "gemini":
        return gemini_script(year, mode, month=month, day=day, target_name=target_name)
    raise ScriptSourceError(
        f"この供給源はファイルを必要とします（`build_script_for_source` ではなく"
        f"`scripts` を渡すこと）: {source!r}"
    )


__all__ = [
    "DEFAULT_SCRIPT_SOURCE",
    "FIXTURES_ENV",
    "NON_HERMETIC_SOURCE",
    "SCRIPT_SOURCES",
    "SCRIPT_SOURCE_ENV",
    "ScriptSourceError",
    "build_script_for_source",
    "deterministic_script",
    "gemini_script",
    "load_fixture_scripts",
    "resolve_fixture_path",
    "resolve_source",
]
