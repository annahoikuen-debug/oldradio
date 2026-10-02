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

重み付け（上位ヒットを優先）
--------------------------
同じ優先度グループの中で全てを等確率で選ぶと、1 年 50 曲のうち
rank 50 付近の曲も rank 1 の曲と同じ確率で選ばれる。

そこで rank の小さい曲ほど高い重みを付けて選ぶ。ただし
**この重み付けが効くのは出典を照合済みの曲（``confidence="verified"``）
だけ**にする。

なぜ照合済みだけなのか
----------------------
正本の rank は原典の順位ではない。現在の正本 3030 件はすべて
``confidence="unverified"`` で、rank には MusicBrainz の検索結果順が
入っている（実測: ``songs.json`` の全レコードが
``unverified:primary-source-pending``）。

その rank で重みを掛けると、ヒット成績ではない並びをヒット成績として
扱うことになり、``docs/song_catalog.md`` の「出典の無い主張をしない」
方針に反する。

したがって:

* ``verified`` の曲 … rank を重みに反映する（上位ほど出やすい）
* ``unverified`` の曲 … 重み 1.0（等確率）。現在の正本はすべてこちら

将来、一次文献と照合した正式な順位が入れば、重み付けは自動的に効き
始める。コードの変更は不要。
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Sequence

from .rng import module_rng

from ..core.songs import pool_for_year, song_key

if TYPE_CHECKING:  # pragma: no cover - 型のみ
    # 実行時に import すると循環参照になる（`core.preview_resolver` の
    # TYPE_CHECKING コメントを参照）。引数の型注釈にしか使わない。
    from ..services.song_store import SongHistoryStore

logger = logging.getLogger(__name__)


def _release_year_of(record: Dict[str, object]) -> Optional[int]:
    """レコードから発表年（``release_year``）を取り出す（無ければ ``None``）。

    R2-05（コア）の対象年保証で使う。カタログ正本のレコードは
    ``release_year`` を必ず持つが、テストや上流の dict では無いことがある。
    """
    value = record.get("release_year")
    try:
        return int(value) if value is not None else None  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


#: ``rank`` が小さい曲に与える重みの強さ。
#:
#: 重みは ``1.0 + RANK_WEIGHT_CEILING / rank`` とする。rank 1 なら
#: ``1 + 50 / 1``、rank 50 なら ``1 + 50 / 50`` になる。
#:
#: 1.0 を足すのは、下位の曲も「絶対に選ばれない」ようにしないため。
#: 重みを 0 にしてしまうと、下位の曲を選曲から外し続けることになる。
RANK_WEIGHT_CEILING = 50


def _hit_weight(record: Dict[str, object]) -> float:
    """重み付け選曲での 1 曲あたりの重みを返す。

    ``confidence="verified"`` の曲だけが ``rank`` を反映し、それ以外は
    等確率の 1.0 に留まる。理由はモジュールの冒頭を参照。

    Parameters
    ----------
    record:
        カタログのレコード 1 件。``confidence`` や ``rank`` が無い /
        壊れていても 1.0 にフォールバックする。選曲が止まらないことを
        優先する。
    """
    if record.get("confidence") != "verified":
        return 1.0
    value = record.get("rank")
    try:
        rank = int(value) if value is not None else 0
    except (TypeError, ValueError):
        return 1.0
    if rank <= 0:
        return 1.0
    return 1.0 + RANK_WEIGHT_CEILING / rank


def _weighted_order(pairs: List[tuple], rng: Any) -> List[tuple]:
    """``(優先度, record)`` の列を重みの大きい順に並べ替える。

    重みが全て等しければ（照合済みの曲が 1 曲も無いとき）通常の
    ``shuffle`` に委ねる。**この場合の結果は重み付け導入前と完全に
    一致する**（既存の決定論を壊さないため）。

    重み付き抽出を「重み付き順」（先頭から順に抽選して決まる並び）として
    実装するのは、``peek`` が先頭 ``count`` 件しか見ないのに対し
    ``select`` は同じ並びの先頭を使うため、両者がずれないようにするため。
    """
    weights = [_hit_weight(record) for _seq, record in pairs]
    if not pairs or all(weight == weights[0] for weight in weights):
        rng.shuffle(pairs)
        return pairs

    remaining = list(pairs)
    remaining_weights = list(weights)
    ordered: List[tuple] = []
    while remaining:
        threshold = rng.random() * sum(remaining_weights)
        cumulative = 0.0
        chosen = len(remaining) - 1
        for index, weight in enumerate(remaining_weights):
            cumulative += weight
            if threshold < cumulative:
                chosen = index
                break
        ordered.append(remaining.pop(chosen))
        remaining_weights.pop(chosen)
    return ordered


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
        rng: Optional[Any] = None,
    ) -> None:
        self._history = history
        # R2-03/04/06 コアの seed 設計: 既定は設定に応じた rng。
        # settings.rng_seed が int のときは決定論的になる。
        self._rng = rng if rng is not None else module_rng()

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
        # R2-05（コア）: fresh を「対象年の曲」と「それ以外（隣接年・10年広げ）」
        # に 2 分割する。pool_for_year が広げた場合、隣接年の曲が対象年の曲を
        # 押し出して先頭に来ることがあった。**対象年の曲を必ず先に**返すことで、
        # 1 番組の前半に確実にその時代の曲が流れる。
        fresh_target = [
            pair for pair in fresh
            if _release_year_of(pair[1]) == int(year)
        ]
        fresh_other = [pair for pair in fresh if pair not in fresh_target]
        # 重みは無かったときの等確率シャッフルと同じ並びを保つ。
        # 「rank が小さいほど先に」という並び順にはしない（そうすると
        # 毎回同じ曲順になり、毎回違う曲順にする効果が消える）。
        fresh_target = _weighted_order(fresh_target, self._rng)
        fresh_other = _weighted_order(fresh_other, self._rng)
        played.sort(key=lambda pair: (pair[0], int(pair[1].get("rank", 0) or 0)))

        return (
            [record for _seq, record in fresh_target]
            + [record for _seq, record in fresh_other]
            + [record for _seq, record in played]
        )

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
