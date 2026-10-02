"""テナントごとの TTS 音声キャッシュ（提案⑧・S4、タスク2）。

## 何を解決するか

現状の `retro_radio/server.py:41` は **OS 共有の一時ディレクトリ 1 つ**に
`CACHE_DIR` を置き、`_tts_cache_filename`（`:335`）が
テキストの SHA-256 から `tts_<hash>.mp3` を命名している。
つまり:

1. **テナントの分隔が無い** — 施設Aの利用者と施設Bの利用者の原稿が同じハッシュ名に
   衝突し、**同じファイルが互いを上書きする**。共有ホスト上で 2 施設を運用すると
   桁違いに交差する。
2. **ファイル名が内容から予測可能** — SHA-256 は本文から機械的に計算できるため、
   認証なしの `GET /api/audio/{filename}` に直接当たり、
   任意の原稿の音声を推測で取得できる。
3. **TTL sweeper が単一ディレクトリ前提** — 削除の単位が「テナント」ではないため、
   「施設Aのキャッシュだけ消したい」が表現できない。

## このモジュールが提供するもの

- `TenantTtsCache`: `CACHE_DIR / tenant_id / tts_<hash>.mp3` 型の
  ディレクトリ構成。**テナント ID ごとに物理的に別のディレクトリ**を持つため、
   OS `ファイルシステム`レベルでも交差が起こらない。
- `sweep()`: **テナント単位**の TTL sweeper。
- `resolve()`: `filename` だけからは配信先を確定できない、
  `テナント ID が明示されたパス` だけを返す。
  呼び出し側は **テナント ID を検証してから**渡すことで、
  他テナントのファイルを配らないことを保証する。

## 不変条件（テスト可能）

> **テナント間のキャッシュ交差 = 0**

これは次の 3 点で担保する。`tests/test_auth_wiring.py` で固定している。

- `tenant_dir(a) != tenant_dir(b)`（`a != b` のとき）
- `resolve(tenant, name)` は `tenant` の配下にしか解決しない
- `sweep(tenant)` は他テナントのファイルを数えない・消さない

## サーバ配線について

`server.py` は **S5 の管轄**であり、S4 は触らない。
そのため本モジュールは「クラス和方法」を提供するだけで、
`_ensure_cache_dir` / `_sweep_tts_cache` / `generate_tts_cached` / `get_audio` の
差し替えは **S5 が行う**。S5 への擬似 diff は最終報告に書いてある。
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
import time
from pathlib import Path
from typing import Iterable, List, Optional, Set

from ..config import get_settings

logger = logging.getLogger(__name__)

#: キャッシュされる拡張子。**判定には使わない**（`notes.mp3.txt` を数えるため）。
#: 実際の判定は [`_is_cache_file`] が「名前の形」で行う。
#: 外部からの互換性のために定数は残す。
CACHE_SUFFIXES = (".mp3", ".tmp")

#: `/api/audio/{filename}` の検証と同じ制約（`server.py:168` と一致させる）。
#: ここで緩めると `/api/audio` の 3 層防御が 1 層だけ崩れる。
AUDIO_FILENAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,127}\.mp3$")

#: テナント ID として受け付ける文字。
#: ディレクトリ名として使うため、`/` `\` `..` などを**構造的に拒否**する。
#: 正規表現で弾くのは多段防御の 1 層目に過ぎないため、
#: 最終的には [`resolve`] の `is_relative_to` でも必ず確認する。
TENANT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

#: テナント ID の最大長（FPS）。過長な ID でパストラバーサル攻撃を受けるのを防ぐ。
MAX_TENANT_ID_LENGTH = 64


def _is_cache_file(path: Path) -> bool:
    """キャッシュファイルか（`tts_*.mp3` / 書き込み中の一時ファイル）。

    拡張子だけで判定すると `notes.mp3.txt` まで数えてしまうため、
    **名前の形**（拡張子が 1 つ + ベース名が `tts_` で始まる）を見る。
    ルート直下に別の用途のファイルがあるディレクトリを
    「テナント」と誤認しないため。
    """
    if not path.is_file():
        return False
    if path.name.startswith(".tts_") and path.suffix == ".tmp":
        return True
    return path.suffix == ".mp3" and path.name.startswith("tts_")


class InvalidTenantIdError(ValueError):
    """テナント ID がディレクトリ名として安全でない。"""


def normalize_tenant_id(tenant_id: Optional[str]) -> str:
    """テナント ID を安全なディレクトリ名へ正規化する。

    - 空 / `None` は `default` に落とす（個人利用モード）。
    - `..` を含む・パス区切りを含む・長すぎるものは **例外**。

    Raises
    ------
    InvalidTenantIdError
        ディレクトリ名として解釈するとディレクトリを脱出しうる値の場合。
    """
    value = (tenant_id or "").strip()
    if not value:
        return "default"
    if len(value) > MAX_TENANT_ID_LENGTH:
        raise InvalidTenantIdError(
            f"テナント ID が長すぎます（最大 {MAX_TENANT_ID_LENGTH} 文字）"
        )
    if not TENANT_ID_PATTERN.match(value) or ".." in value:
        raise InvalidTenantIdError(
            "テナント ID には英数字・`_`・`-` のみ使えます（`..` やパス区切りは不可）"
        )
    return value


class TenantTtsCache:
    """テナント単位の TTS キャッシュ。

    Parameters
    ----------
    root:
        キャッシュのルート（= 既存の `server.CACHE_DIR`）。
        **テナントごとにその配下にディレクトリを作る**。
    ttl_days:
        TTL（日）。`None` なら設定値 `tts_cache_ttl_days` を使う。
    sweep_interval:
        `sweep()` が「N 回呼ばれたら 1 回だけ実行する」制御に使う。
        `None` なら設定値 `tts_cache_sweep_interval` を使う。
    max_files:
        テナント配下のキャッシュファイル数の上限（CACHE-01）。
        `None` なら設定値 `tts_cache_max_files` を使う。超過時は古い順に削除。
    """

    def __init__(
        self,
        root: Path,
        ttl_days: Optional[int] = None,
        sweep_interval: Optional[int] = None,
        max_files: Optional[int] = None,
    ) -> None:
        self.root = Path(root)
        settings = get_settings()
        self.ttl_days = int(ttl_days if ttl_days is not None else settings.tts_cache_ttl_days)
        self.sweep_interval = int(
            sweep_interval if sweep_interval is not None else settings.tts_cache_sweep_interval
        )
        self.max_files = int(
            max_files if max_files is not None else settings.tts_cache_max_files
        )
        self._lock = threading.Lock()
        self._sweep_counters: dict = {}

    # --- ディレクトリ解決 -------------------------------------------------------
    def tenant_dir(self, tenant_id: Optional[str]) -> Path:
        """そのテナント専用のキャッシュディレクトリ（未作成でも返す）。"""
        return self.root / normalize_tenant_id(tenant_id)

    def ensure_tenant_dir(self, tenant_id: Optional[str]) -> Path:
        """そのテナントのディレクトリを作る（冪等）。"""
        path = self.tenant_dir(tenant_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def list_tenants(self) -> List[str]:
        """存在するテナントの ID を列挙する（ディレクトリだけになったものは除く）。"""
        if not self.root.is_dir():
            return []
        out: List[str] = []
        for entry in sorted(self.root.iterdir()):
            if not entry.is_dir():
                continue
            if not TENANT_ID_PATTERN.match(entry.name):
                continue
            if any(_is_cache_file(child) for child in entry.iterdir()):
                out.append(entry.name)
        return out

    # --- ファイル名 -------------------------------------------------------------
    def filename_for(self, text: str, lang: str, tld: str, slow: bool) -> str:
        """テキスト + 音声設定からファイル名を導く（既存の `_tts_cache_filename` と同一）。

        音声設定（lang / tld / slow）をキーに含めないと同じファイル名のまま
        古い言語の音声が返るため、必ず含める。
        """
        cache_key = f"{text}_{lang}_{tld}_{slow}"
        return f"tts_{hashlib.sha256(cache_key.encode('utf-8')).hexdigest()}.mp3"

    def path_for(self, tenant_id: Optional[str], filename: str) -> Path:
        """テナント ID とファイル名から実パスを作る（存在確認はしない）。"""
        return self.tenant_dir(tenant_id) / filename

    def relative_url_for(self, tenant_id: Optional[str], filename: str) -> str:
        """テナント ID を含む **配信 URL** を返す。

        `server.py:844` の `f"/api/audio/{audio_filename}"` を置き換えるためのもの。
        形式は `/api/audio/{tenant_id}/{filename}`。
        **テナント ID が URL に入る**ことで、配信時にセッション照合できる
        （要件「`tenant_id` のセッション照合後にのみ配信する」）。
        """
        return f"/api/audio/{normalize_tenant_id(tenant_id)}/{filename}"

    # --- 配信判定 ---------------------------------------------------------------
    def resolve(self, tenant_id: Optional[str], filename: str) -> Optional[Path]:
        """配信可能な実パスを返す。**テナント外なら `None`**。

        検証 4 層:
        1. ファイル名の形式（`AUDIO_FILENAME_PATTERN`）
        2. テナント ID の形式（`normalize_tenant_id` が例外を投げる）
        3. 実パスがそのテナントディレクトリ配下か（シンボリックリンク対策）
        4. 実ファイルか

        3 の `is_relative_to` は **resolve 後** で判定する。
        resolve 前だと `..` やシンボリックリンクをすり抜ける。
        """
        if not filename or not AUDIO_FILENAME_PATTERN.match(filename):
            return None
        try:
            tenant = normalize_tenant_id(tenant_id)
        except InvalidTenantIdError:
            return None
        target = self.tenant_dir(tenant)
        if not target.is_dir():
            return None
        try:
            resolved = (target / filename).resolve(strict=True)
            root = target.resolve()
        except OSError:
            return None
        if not resolved.is_relative_to(root) or not resolved.is_file():
            return None
        return resolved

    # --- TTL sweeper -----------------------------------------------------------
    def sweep(self, tenant_id: Optional[str] = None, force: bool = False) -> int:
        """TTL より古いファイルを削除する。戻り値は削除件数。

        Parameters
        ----------
        tenant_id:
            **特定テナントだけ**掃除する。`None`（既定）は全テナント。
            施設運用では「テナント単位」で消すため、この引数が本体。
        force:
            間隔カウントを無視して必ず実行する（起動時）。

        Notes
        -----
        削除の単位が 1 ディレクトリ = 1 テナントなので、
        **他テナントのファイルには触らない**（テナント交差 = 0 の根拠）。
        """
        key = normalize_tenant_id(tenant_id) if tenant_id is not None else "*"
        if not force:
            with self._lock:
                count = self._sweep_counters.get(key, 0) + 1
                self._sweep_counters[key] = count
            if count % max(1, self.sweep_interval) != 0:
                return 0

        targets = (
            [self.tenant_dir(tenant_id)]
            if tenant_id is not None
            else [self.root / t for t in self.list_tenants()]
        )
        cutoff = time.time() - self.ttl_days * 86400
        removed = 0
        for directory in targets:
            if not directory.is_dir():
                continue
            try:
                for entry in directory.iterdir():
                    try:
                        if _is_cache_file(entry) and entry.stat().st_mtime < cutoff:
                            entry.unlink()
                            removed += 1
                    except OSError:
                        continue
            except OSError as e:  # pragma: no cover - 環境依存
                logger.warning("TTSキャッシュの整理に失敗しました: %s", e)
        # 個数上限（CACHE-01）: TTL 判定だけでは TTL 内にファイルが無制限に
        # 蓄積するため、上限超過時は**古い順に**削除する。
        removed += self._enforce_max_files(targets)
        if removed:
            logger.info("古いTTSキャッシュを削除しました: %s件（scope=%s）", removed, key)
        return removed

    def _enforce_max_files(self, targets: List[Path]) -> int:
        """テナント配下のキャッシュファイル数を `max_files` に収める（古い順に削除）。"""
        if self.max_files <= 0:
            return 0
        removed = 0
        for directory in targets:
            if not directory.is_dir():
                continue
            try:
                files = [entry for entry in directory.iterdir() if _is_cache_file(entry)]
            except OSError:  # pragma: no cover - 環境依存
                continue
            excess = len(files) - self.max_files
            if excess <= 0:
                continue
            try:
                files.sort(key=lambda entry: entry.stat().st_mtime)
            except OSError:  # pragma: no cover - 環境依存
                continue
            for entry in files[:excess]:
                try:
                    entry.unlink()
                    removed += 1
                except OSError:
                    continue
        return removed

    def sweep_all(self, force: bool = True) -> int:
        """全テナントのキャッシュを掃除する（起動時の `lifespan` 用）。"""
        return self.sweep(tenant_id=None, force=force)

    def reset_sweep_counters(self) -> None:
        """間隔カウンタをリセットする（テスト隔離用）。"""
        with self._lock:
            self._sweep_counters.clear()

    # --- 交差 0 の不変条件（テスト可能な観測点） ---------------------------------
    def files_of(self, tenant_id: Optional[str]) -> Set[str]:
        """そのテナントのファイル名の集合。**他テナントは含まれない**。"""
        directory = self.tenant_dir(tenant_id)
        if not directory.is_dir():
            return set()
        return {
            entry.name for entry in directory.iterdir() if _is_cache_file(entry)
        }

    def purge_tenant(self, tenant_id: Optional[str]) -> int:
        """テナントのキャッシュを**丸ごと**消す（削除請求の実行に相当）。"""
        directory = self.tenant_dir(tenant_id)
        if not directory.is_dir():
            return 0
        removed = 0
        for entry in list(directory.iterdir()):
            try:
                if entry.is_file():
                    entry.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def size_bytes(self, tenant_ids: Optional[Iterable[str]] = None) -> int:
        """指定テナント（無ければ全部）の合計サイズ。運用指標用。"""
        targets = (
            [self.tenant_dir(t) for t in tenant_ids]
            if tenant_ids is not None
            else [self.root / t for t in self.list_tenants()]
        )
        total = 0
        for directory in targets:
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                try:
                    if entry.is_file():
                        total += entry.stat().st_size
                except OSError:
                    continue
        return total


def tenant_cache_from_settings(root: Optional[Path] = None) -> TenantTtsCache:
    """`server.CACHE_DIR` を渡してキャッシュハンドルを組み立てる（``server.py`` 側の接着点）。

    ``root`` が ``None`` の場合は ``CACHE_DIR`` の既定値
    （``%TEMP% / retro_radio_audio_cache``）を使う。S5 はこれで置き換える。
    """
    if root is None:
        import tempfile

        root = Path(tempfile.gettempdir()) / "retro_radio_audio_cache"
    return TenantTtsCache(root)


__all__ = [
    "TenantTtsCache",
    "InvalidTenantIdError",
    "normalize_tenant_id",
    "tenant_cache_from_settings",
    "AUDIO_FILENAME_PATTERN",
    "TENANT_ID_PATTERN",
    "MAX_TENANT_ID_LENGTH",
    "CACHE_SUFFIXES",
]
