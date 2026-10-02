"""決定論用の rng ヘルパ（R2-03/04/06 コアの seed 設計）。

選曲・話題選択の乱数が 3 箇所（``script_generator.select_news_topics`` /
``song_selector`` の既定 rng / ``fallback.get_fallback_song``）で
グローバル ``random`` を直接使っており、同一入力で異なる結果になっていた。
テストの再現性・監査性が必要な施設運用・eval 用に seed を設定できるようにする。

契約:

* ``settings.rng_seed`` が ``None``（既定）: グローバル ``random`` を返す
  （**現行挙動と完全に同じ**）。既存の呼び出し側は影響を受けない。
* ``settings.rng_seed`` が int: 新しい ``random.Random(seed)`` を返す
  （**同一入力なら同一結果**）。

``module_rng()`` は呼び出しごとに新しい Random を作る。
「1 つの Random インスタンスを共有すると呼び出し順で結果が変わる」ため、
決定論が必要な箇所は「同一入力なら同一結果」であることだけを保証する。
"""

import random
from typing import Union

from ..config import get_settings

#: グローバル ``random`` モジュールも ``sample`` / ``choice`` / ``shuffle`` を
#: 持つため、rng として同じ型に畳む（構造的部分型）。
RandomLike = Union[random.Random, "random"]


def module_rng() -> RandomLike:
    """設定に応じた rng を返す（決定論 or グローバル random）。

    ``settings.rng_seed`` が int のとき ``random.Random(seed)``、
    ``None`` のときグローバル ``random`` モジュールを返す。
    """
    seed = get_settings().rng_seed
    if seed is None:
        return random
    return random.Random(int(seed))


__all__ = ["module_rng", "RandomLike"]
