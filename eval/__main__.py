"""``python -m eval`` のエントリポイント（提案⑨ 評価ハーネスの一括実行）。"""

from __future__ import annotations

import sys

from .metrics import main

if __name__ == "__main__":
    sys.exit(main())
