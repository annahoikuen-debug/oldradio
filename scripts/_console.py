"""保守スクリプト共通のコンソール設定。

なぜ要るか
----------
Windows の既定コンソールエンコーディングは **cp932**（日本語 Windows）だが、
正本データには cp932 で表現できない文字が含まれる:

- 曲名・アーティスト名（`Der Legionär`、`Marie Laforêt`、`½の神話` など
  — 正本カタログ 2944 件中 **45 件**が cp932 で表現できない）


そのため `print()` を素に呼ぶと
``UnicodeEncodeError: 'cp932' codec can't encode character`` で
**スクリプトがクラッシュし、終了コード 1 になる**。
``fail: 0`` でもそうなるので、CI ゲートを壊す。

このモジュールは stdout / stderr を **UTF-8（_errors="replace"）** に再設定し、
検証スクリプトが「検証結果を最後まで報告して終了できる」ようにする。

.. warning::
   呼び出しは **検証より前**（``main()`` の先頭）で行う。
   出力は検証の途中で現れることがあるため、
   再設定が遅いと意味がない。
"""

from __future__ import annotations

import sys
from typing import Any


def force_utf8_stdio(streams: Any = None) -> None:
    """stdout / stderr を UTF-8 に再設定する（Windows cp932 対策）。

    Parameters
    ----------
    streams:
        対象のストリーム。省略時は ``(sys.stdout, sys.stderr)``。

    Notes
    -----
    既に UTF-8 なら何もしない（冪等）。
    ``reconfigure`` を持たないストリーム（``io.StringIO``、pytest の
    キャプチャなど）は静かにスキップする。
    """
    for stream in (streams if streams is not None else (sys.stdout, sys.stderr)):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            encoding = (getattr(stream, "encoding", "") or "").lower().replace("-", "")
        except Exception:  # pragma: no cover - ENCODING を持たないストリーム
            encoding = ""
        if encoding in ("utf8", "utf8sig"):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):  # pragma: no cover
            pass


__all__ = ["force_utf8_stdio"]
