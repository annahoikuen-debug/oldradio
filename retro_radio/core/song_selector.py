"""再生履歴に基づく選曲（ローテーション）。

「同じ年を選んでも前回放送の曲と被らないように、まんべんなく流す」ための
アルゴリズム。選曲順の規則は 1 つだけ:

    **未再生の曲を先に、再生済みの曲では最も古い順に取る。**

これで次の 2 点が保証される（1 年のプールが N 曲、1 番組が M 曲、N > M）:

* 同じ曲を連続する 2 回の放送で両方取れることはない
* すべての N 曲が 1 回ずつ流れるまで、どの曲も 2 回目に回らない
  （つまり「特定の 1 曲ばかり繰り返し流される」ことがない）

これは「シャッフルして偶発を待つ」より強い。``random`` だけだと
N=50 / M=18 でも 2 回目の放送で 1 曲も重複しない確率は
``(32/50)*(31/49)*…`` = 数千分の 1 程度しかなく、実際に「被った」と
言われる。履歴を持てば決定的になる。

「未再生を先に取る」順序を保証するうえで、**同じ優先度の中で**は
シャッフルする。シャッフルしないと常に rank 順（= 有名な曲から）出て、
毎回同じ曲順になる。
"""

from __future__ import annotations

import logging
import random
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence

from ..core.songs import pool_for_year, song_key

if TYPE_CHECKING:  # pragma: no cover - 型のみ
    # 実行時に import すると循環参照になる（`core.preview_resolver` の
    # TYPE_CHECKING コメントを参照）。引数の型注釈にしか使わない。
    from ..services.song_store import SongHistoryStore

logger = logging.getLogger(__name__)


class SongSelector:
    """履歴を見ながら ``count`` 曲を選ぶ。

    Parameters
    ----------
    history:
        再生履歴のストア。``None`` を渡すと履歴を見ない（＝毎回同じ順）。
        テストや、履歴を保存できない読み取り専用環境用。
    rng:
        シャッフル用の乱数生成器。**注入できる**のは
        「同じ入力なら同じ曲順」をテストできるようにするため。
    """

    def __init__(
        self,
        history: "Optional[SongHistoryStore]" = None,
        rng: Optional[random.Random] = None,
    ) -> None:
        self._history = history
        self._rng = rng or random.Random()

    def order_candidates(
        self, year: int, candidates: Sequence[Dict[str, object]]
    ) -> List[Dict[str, object]]:
        """候補を「次に再生すべき順」に並べ替える（**選択はしない**）。

        Returns
        -------
        list[dict]
            ``candidates`` の要素を並べ替えた新しいリスト。**元の
            ``candidates`` は変更しない**（呼び出し側の再利用のため）。

        Notes
        -----
        並び順は「最後に再生した位置（単調増加カウンタ）の昇順」。
        未再生（履歴に無い）曲の位置は ``0`` として扱うため、
        **未再生が必ず先に来る**。同値（= 両方とも未再生、または同じ
        番組で再生された）はこの中でシャッフルして、
        有名な曲ばかり固定で出るのを避ける。
        """
        if not candidates:
            return []

        last_played: Dict[str, int] = {}
        if self._history is not None:
            last_played = self._history.last_played(int(year))

        decorated: List[tuple] = []
        for record in candidates:
            key = song_key(str(record.get("title", "")), str(record.get("artist", "")))
            # 未再生は 0。履歴の seq は 1 始まりなので必ず未再生より後ろになる。
            decorated.append((last_played.get(key, 0), record))

        # 0 番（同値グループ）のみシャッフルする。1 番以降は「最古 → 最新」の
        # 順序を崩さない（崩すと「次に再生されるのはいつだった曲か」が
        # 分からなくなり、ローテーションの意味が失われる）。
        fresh = [pair for pair in decorated if pair[0] == 0]
        played = [pair for pair in decorated if pair[0] != 0]
        self._rng.shuffle(fresh)
        played.sort(key=lambda pair: (pair[0], int(pair[1].get("rank", 0) or 0)))

        return [record for _seq, record in fresh] + [record for _seq, record in played]

    def select(self, year: int, count: int) -> List[Dict[str, object]]:
        """``year`` 年の曲を ``count`` 曲選び、**再生済みとして記録する**。

        Parameters
        ----------
        year:
            対象年。範囲外は既定年に寄せる。
        count:
            必要な曲数。1 番組（既定 3 周 × 1 パス 6 曲 = 18 曲）を
            重複ゼロで埋めるには、これだけのプールが必要。

        Notes
        -----
        プールが ``count`` に満たないときは同じ 10 年まで広げて足す
        （``core.songs.pool_for_year``）。それでも足りなければ、
        広げた範囲の曲だけを返し、**空は返さない**
        （1 曲も無いと番組の曲スロットが埋まらないため）。
        """
        count = max(1, int(count))
        candidates = pool_for_year(int(year), count)
        ordered = self.order_candidates(int(year), candidates)
        selected = ordered[:count]

        if len(selected) < count:
            logger.warning(
                "選曲プールが足りません: year=%s 要求=%d 採用=%d（%d 年間で広げた結果）",
                year,
                count,
                len(selected),
                (int(year) // 10 + 1) * 10 - int(year) // 10 * 10,
            )

        if self._history is not None:
            self._history.record(
                int(year),
                [
                    song_key(str(record.get("title", "")), str(record.get("artist", "")))
                    for record in selected
                ],
            )
        return selected

    def peek(self, year: int, count: int) -> List[Dict[str, object]]:
        """記録せずに「次に再生される曲」だけを見る（プレビュー・UI 用）。"""
        count = max(1, int(count))
        return self.order_candidates(int(year), pool_for_year(int(year), count))[:count]


__all__ = ["SongSelector"]
