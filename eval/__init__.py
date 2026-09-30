"""内容品質の評価ハーネス（提案⑨ / サブエージェント S2）。

このパッケージは**テストではなく測定器**である。目的是
`plans/evidence_based_improvement_proposals.md` 438〜494 行が言うとおり、
「1,000 文字以上」という能動的に逆向きの content メトリクスを捨てて、
**台本が壊れている条件を列挙して検査する**ことにある。

構成:

| モジュール | 責務 |
|---|---|
| `eval.cases` | 層別抽出 24 ケースの定義と生成（`build_cases.py` が正本から生成） |
| `eval.metrics.fact_score` | FactScore 簡易実装（原子的事実 ↔ 事実レジストリの照合） |
| `eval.metrics.checklist` | CheckList 8 項目（Ribeiro et al. 2020 相当） |
| `eval.metrics.length` | 文字数のレンジ制（水増しを罰する） |
| `eval.metrics.songs` | 曲名一致率（S3 へ渡す中立 API） |
| `eval.metrics.preannounce` | 「予告したのに配信しない」検出 |
| `eval.bww` | Best-Worst 人間評価の手順書（人手が必要なため CI には載せない） |

**所有境界**: `retro_radio/core/*`（S1/S3）・`retro_radio/config.py`（S4）・
`retro_radio/server.py`（S5）・`static/*`（S6/S7/S9）・
`scripts/validate_facts.py` と `retro_radio/core/facts/*`（S1） は
**読み取りのみ**。ここからは一切編集しない。

## 直接実行する

すべてのモジュールは ``python -m`` で単体実行できる（``__main__`` 節がある）。

```bash
python -m eval.metrics.length --measure      # 文字数の実測（レンジ決定の根拠）
python -m eval.metrics.length                # レンジだけ表示
python -m eval.cases.build_cases             # case_defs.json を再生成
python -m eval                               # 24 ケースを全部回す
```
"""

from __future__ import annotations

import sys
from pathlib import Path

# ``python -m eval.metrics.length`` や ``python eval/metrics/length.py`` の
# どちらからでもリポジトリ直下を import できるようにする。
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

__all__ = ["REPO_ROOT"]

REPO_ROOT = _REPO_ROOT
