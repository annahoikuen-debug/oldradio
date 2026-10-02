"""選曲ローテーションの永続化と、iTunes プレビュー解決結果のキャッシュ。

なぜ永続化なのか
----------------
「同じ年を選んでも前回放送の曲と被らないように流す」には、**その年の
どの曲をいつ再生したか**が要る。プロセス内の ``random`` だけでは
再起動のたびに同じ曲から始まり、同一番組を 2 つ作っても被る。
実測では旧実装の静的マスターが全 36 曲しか無いため、同一年に数回
放送すると必ず重複していた。

このストアは 2 つの責務を持つ。どちらも小さい SQLite 1 ファイルに
閉じており、外部依存を持たない（標準ライブラリ ``sqlite3`` のみ）。

1. :class:`SongHistoryStore` … 年ごとの「最後に再生した位置」。
   選曲はこの表を見て**未再生 → 最古再生**の順に取る。
2. :class:`PreviewCache` … ``(曲名, アーティスト)`` → iTunes のプレビュー URL。
   ローテーションにより同じ曲の再解決が減るため、HTTP を省ける。

親プロセス之外（テスト・別プロセス）から同時に書きうるため、
接続は**操作ごとに開き閉じる**。``sqlite3`` の接続はスレッドをまたげない
ため、共有するとサーバのスレッドプール（同時生成 2）で壊れる。

ただし「接続を開く」たびに DDL と ``PRAGMA journal_mode=WAL`` を
走らせてはならない。``journal_mode`` の切り替えは **DB ヘッダの書き込み**
で排他ロックを要求し、かつ ``busy_timeout`` を設定する前に実行すると
    待ち時間なしで `database is locked` に落ちる。サーバは要求ごとに
ストアを生成するため、この書き換えが定常的に起きていた。対策は 3 つ:

1. :class:`_SqliteStore` を **解決済みパスごとのプロセス内シングルトン**に
   して、``_initialized`` により DDL と journal 変換を最初の 1 回だけ行う。
2. 接続時は ``busy_timeout`` を先に設定し、``journal_mode`` は **既に WAL
   なら触らない**（読み取りだけなので排他ロックを取らない）。必要なときだけ
   変換を単独のロック下でリトライする。
3. 再生順カウンタ ``played_seq`` は **SQLite 側のカウンタ表**で採番する。
   プロセス内カウンタだと、リクエストごとに新しいストアが生成されるので
   スレッド間で同じ番号が使われ、「未再生 → 最古再生」の並び順が壊れる。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence

logger = logging.getLogger(__name__)

# 履歴は「年 × 曲」1 行で、 更新のたびに上書きする。
# したがって行数はカタログの上限（年 50 曲 × 76 年 = 3800）に自然に
# 収まる。削除処理は不要。
SCHEMA = """
CREATE TABLE IF NOT EXISTS song_playback (
    year       INTEGER NOT NULL,
    song_key   TEXT    NOT NULL,
    played_seq INTEGER NOT NULL,
    played_at  REAL    NOT NULL,
    PRIMARY KEY (year, song_key)
);
CREATE INDEX IF NOT EXISTS ix_song_playback_year_seq
    ON song_playback (year, played_seq);

CREATE TABLE IF NOT EXISTS song_preview (
    song_key       TEXT PRIMARY KEY,
    preview_url    TEXT,
    artwork_url    TEXT,
    -- Apple への送客導線（`trackViewUrl` に `at`/`ct` を付けたもの）。
    -- NULL は「導線が無い」意味で、既存行は移行時に NULL のまま扱う。
    track_view_url TEXT,
    checked_at     REAL NOT NULL
);

-- 再生順カウンタ。プロセス再起動・プロセス跨ぎでも単調増加させる。
CREATE TABLE IF NOT EXISTS song_sequence (
    id    INTEGER PRIMARY KEY CHECK (id = 1),
    value INTEGER NOT NULL
);
"""

# ロック待ちの許容秒数。並列生成（同時 2 スレッド）と、別プロセスからの
# テスト実行を想定して大きめに取り、``busy_timeout`` を**最先**に設定する。
BUSY_TIMEOUT_SECONDS = 10.0

# プロセス内シングルトンレジストリの上限。テストは ``tmp_path`` を変えるため
# パスが増え続けるが、古い項目を捨てれば無制限に膨らまない。
_REGISTRY_LIMIT = 64
_REGISTRY_LOCK = threading.Lock()
_STORES: "Dict[str, _SqliteStore]" = {}

# WAL 変換をプロセス内で 1 本に絞るロック（DB ヘッダの排他書き込み）。
_WAL_LOCK = threading.Lock()


class SongStoreError(RuntimeError):
    """ストアを開けない、または書き込みに失敗したとき。"""


def _resolve_path(path: Optional[str], fallback_dir: Optional[str] = None) -> Path:
    """ストアのファイルパスを決める。

    ``None`` / 空文字なら作業ディレクトリの既定ファイルに置く。
    ``sqlite:///`` のような URL が渡された場合はパス部分だけを取り出す
    （``config.database_url`` と同じファイルに置けるようにするため）。
    """
    raw = (path or "").strip()
    if raw.startswith("sqlite:///"):
        raw = raw[len("sqlite:///"):]
    elif "://" in raw:
        raw = Path(raw).name
    if not raw:
        raw = "retro_radio_song_store.db"
    resolved = Path(raw).expanduser()
    if not resolved.is_absolute() and fallback_dir:
        resolved = Path(fallback_dir) / resolved.name
    return resolved


class _SqliteStore:
    """SQLite の接続管理だけを共有する土台。

    接続は操作ごとに開く。**長時間開きっぱなしにしない**のは、
    サーバが `ThreadPoolExecutor` で同時に 2 件の生成を走らせ、
    ``sqlite3`` の接続をスレッド間で共有すると
    ``ProgrammingError: SQLite objects created in a thread can only be
    used in that same thread`` になるため。

    インスタンスは :func:`_shared_store` が**パスごとに 1 つだけ**返す。
    インスタンスが新しいと ``_initialized`` が毎回リセットされ、
    ``PRAGMA journal_mode=WAL``（DB ヘッダ書き込み）が接続のたびに走って
    `database is locked` を起こすため。
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._init_lock = threading.Lock()
        self._initialized = False

    def _ensure_wal(self, conn: sqlite3.Connection) -> None:
        """必要ならだけ WAL へ変換する（冪等・失敗しても致命的ではない）。

        既に WAL なら何もしない（``PRAGMA journal_mode`` の**読み取り**は
        排他ロックを取らないので、接続のたびに実行しても安全）。
        変換が必要なのは初回だけなので、失敗しても既定のジャーナルで
        動作し続け、下流の操作を殺さない。
        """
        try:
            row = conn.execute("PRAGMA journal_mode;").fetchone()
            if row and str(row[0]).lower() == "wal":
                return
        except sqlite3.Error:  # pragma: no cover - 読み取り失敗は変換を試す
            pass

        with _WAL_LOCK:
            for attempt in range(3):
                try:
                    conn.execute("PRAGMA journal_mode=WAL;")
                    return
                except sqlite3.Error as exc:
                    if attempt == 2:
                        logger.warning(
                            "journal_mode=WAL に変換できません（既定のまま続けます）: %s", exc
                        )
                        return
                    time.sleep(0.05 * (attempt + 1))

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=BUSY_TIMEOUT_SECONDS)
        except (OSError, sqlite3.Error) as exc:
            raise SongStoreError(f"選曲ストアを開けません: {self.path} ({exc})") from exc

        try:
            # 順序が重要: busy_timeout を**先に**設定する。
            # これを持たない接続は journal_mode の切り替えで待たずに即失敗し、
            # `database is locked` になる（WAL 変換は排他ロックを要求する）。
            conn.execute(f"PRAGMA busy_timeout={int(BUSY_TIMEOUT_SECONDS * 1000)};")
            conn.execute("PRAGMA synchronous=NORMAL;")
            self._ensure_wal(conn)
            yield conn
        except sqlite3.Error as exc:
            raise SongStoreError(f"選曲ストアの操作に失敗しました: {self.path} ({exc})") from exc
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """テーブルを作り、必要なら移行する（2 回呼んでも安全）。"""
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            with self.connect() as conn:
                conn.executescript(SCHEMA)
                # カウンタ行の初期値を「既存の最大再生番号」以上にそろえる。
                # カウンタ表が無い既存 DB では 0 から始めると新しい番号が
                # 既存の再生履歴と衝突し、「最古」の順序が壊れる。
                row = conn.execute(
                    "SELECT MAX(played_seq) FROM song_playback"
                ).fetchone()
                current = int(row[0] or 0) if row else 0
                existing = conn.execute(
                    "SELECT value FROM song_sequence WHERE id = 1"
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO song_sequence (id, value) VALUES (1, ?)",
                        (current,),
                    )
                elif int(existing[0]) < current:
                    conn.execute(
                        "UPDATE song_sequence SET value = ? WHERE id = 1", (current,)
                    )
                conn.commit()
            # 移行は**初期化の中だけ**1 回実行する。`PreviewCache.get/put` は
            # 1 曲につき 2 回 `ensure_schema()` を呼ぶため、外で呼ぶと
            # 1 回の番組生成で数十回の接続と PRAGMA が重複して開くことになる。
            # DB が後から別スキーマへ差し替わった場合は、`PreviewCache` 側が
            # `no such column` を検知して `migrate_store_url_column` を呼ぶ。
            self.migrate_store_url_column()
            self._initialized = True

    def migrate_store_url_column(self) -> None:
        """既存 DB に `track_view_url` を足す（既にあれば何もしない）。

        `CREATE TABLE IF NOT EXISTS` は**既存テーブルの形を変えない**ため、
        旧バージョンの DB にはこのカラムが存在しない。導線を読み書きする
        前に必ず移行する。放置すると `no such column` で失敗する。

        呼び出しは :meth:`ensure_schema` の初期化ブロックから**1 回だけ**、
        または `PreviewCache` が `no such column` を検知したときの
        リトライ経路から。共有ストアはパスごとに別インスタンスなので、
        他の DB の初期化済みに影響されることはない。

        `track_view_url` は NULL 許容なので、既存行の読み書きには影響しない
        （既存行は導線なしのまま扱う）。
        """
        try:
            with self.connect() as conn:
                columns = {
                    str(row[1])
                    for row in conn.execute("PRAGMA table_info(song_preview)")
                }
                if not columns or "track_view_url" in columns:
                    return
                conn.execute(
                    "ALTER TABLE song_preview ADD COLUMN track_view_url TEXT"
                )
                conn.commit()
        except SongStoreError:
            logger.warning(
                "プレビューキャッシュのスキーマ移行に失敗しました", exc_info=True
            )


def _shared_store(path: Path) -> _SqliteStore:
    """解決済みパスごとの 1 プロセス内 1 インスタンスを返す。

    サーバはリクエストごとにストアを生成する。インスタンスが毎回違うと
    ``_initialized`` とカウンタが毎回リセットされ、WAL pragma の連打と
    再生番号の重複が起きる。レジストリはロックで保護する。
    """
    key = str(path)
    with _REGISTRY_LOCK:
        store = _STORES.get(key)
        if store is None:
            if len(_STORES) >= _REGISTRY_LIMIT:
                for stale in list(_STORES)[: len(_STORES) - _REGISTRY_LIMIT + 1]:
                    _STORES.pop(stale, None)
            store = _SqliteStore(path)
            _STORES[key] = store
    return store


class SongHistoryStore:
    """年ごとの「最後に再生した位置」を記録する。

    Notes
    -----
    選択の単位は曲 ID ではなく **正規化済みの ``(曲名, アーティスト)``**。
    正本の表記が「モーニング娘。」でも「モーニング娘」でも同じ曲として
    扱うため（``core.songs.song_key``）。カタログの ``id`` を主キーにすると
    表記ゆれで同じ曲を 2 つ数えてしまい、ローテーションが壊れる。
    """

    def __init__(self, path: Optional[str] = None) -> None:
        self._store = _shared_store(_resolve_path(path))
        self._disabled = False

    @property
    def path(self) -> Path:
        return self._store.path

    def disable(self) -> None:
        """以後の記録を捨てる（読み取り専用デプロイ・テスト用）。"""
        self._disabled = True

    def ensure_schema(self) -> None:
        self._store.ensure_schema()

    def _next_seq(self) -> int:
        """単調増加カウンタ（1 個だけ取る場合）。

        ミリ秒の ``time.time()`` ではなくカウンタを使うのは、
        1 番組で 18 曲をまとめて記録するとき**同値 Tie** が起きると
        「最古」の順序が不定になるため。採番は **SQLite 内**で行う
        （プロセス内カウンタだとリクエストごとに新しいストアが生成され、
        スレッド間で同じ番号が使われて順序が壊れる）。
        """
        return self._alloc_seqs(1)[0]

    def _alloc_seqs(self, count: int) -> List[int]:
        """``count`` 個の連続した再生番号を**採番だけ**する（書き込まない）。"""
        if count <= 0:
            return []
        self.ensure_schema()
        with self._store.connect() as conn:
            return self._alloc_seqs_in(conn, count)

    @staticmethod
    def _alloc_seqs_in(conn: sqlite3.Connection, count: int) -> List[int]:
        """同一トランザクション内で番号を採番する。

        採番と履歴の書き込みを同じトランザクションにすることで、
        「番号だけ消費して記録が落ちる」取りこぼしを無くす。
        """
        # SELECT→UPDATE の間に他接続が同じ value を読むと**二重採番**になる
        # （sqlite3 の既定 deferred では SELECT はトランザクションを開かない）。
        # まず書き込みロック（BEGIN IMMEDIATE）を取り、その後に読む。
        # 呼び出し側（`_alloc_seqs` / `record`）はどちらも新規接続で
        # 未コミットのトランザクションを持たないため、BEGIN は安全。
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute("SELECT value FROM song_sequence WHERE id = 1").fetchone()
            start = int(row[0]) if row else 0
            end = start + int(count)
            if row is None:
                conn.execute("INSERT INTO song_sequence (id, value) VALUES (1, ?)", (end,))
            else:
                conn.execute("UPDATE song_sequence SET value = ? WHERE id = 1", (end,))
        except sqlite3.Error:
            conn.execute("ROLLBACK")
            raise
        return list(range(start + 1, end + 1))

    def last_played(self, year: int) -> Dict[str, int]:
        """``year`` の ``{song_key: played_seq}`` を返す（再生が無い年は空）。"""
        try:
            self.ensure_schema()
            with self._store.connect() as conn:
                rows = conn.execute(
                    "SELECT song_key, played_seq FROM song_playback WHERE year = ?",
                    (int(year),),
                ).fetchall()
        except SongStoreError:
            logger.warning("選曲履歴を読み込めません（空として扱います）", exc_info=True)
            return {}
        return {str(key): int(seq) for key, seq in rows}

    def record(self, year: int, song_keys: Sequence[str]) -> None:
        """``song_keys`` を「いま再生した」position として記録する。

        記録に失敗しても**番組の生成自体は止めない**。履歴は「被りにくさ」の
        ための補助であり、書けなければ毎回同じ順に選ぶだけで、
        音源が無いとか番組が出ないといった壊れ方とは質が違う。
        """
        keys = [str(key) for key in song_keys if key]
        if not keys or self._disabled:
            return
        try:
            self.ensure_schema()
            now = time.time()
            with self._store.connect() as conn:
                seqs = self._alloc_seqs_in(conn, len(keys))
                conn.executemany(
                    "INSERT INTO song_playback (year, song_key, played_seq, played_at) "
                    "VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(year, song_key) DO UPDATE SET "
                    "played_seq = excluded.played_seq, played_at = excluded.played_at",
                    [(int(year), key, seq, now) for key, seq in zip(keys, seqs)],
                )
                conn.commit()
        except SongStoreError:
            logger.warning(
                "選曲履歴を記録できませんでした（次回から重複が増える可能性があります）",
                exc_info=True,
            )

    def reset(self, year: Optional[int] = None) -> int:
        """履歴を削除する（``year`` 指定で 1 年だけ）。削除した行数を返す。"""
        self.ensure_schema()
        with self._store.connect() as conn:
            if year is None:
                cursor = conn.execute("DELETE FROM song_playback")
            else:
                cursor = conn.execute("DELETE FROM song_playback WHERE year = ?", (int(year),))
            conn.commit()
            return int(cursor.rowcount or 0)


class PreviewCache:
    """``(曲名, アーティスト)`` → iTunes プレビュー URL のキャッシュ。

    キャッシュは **肯定結果と否定結果の両方**を持つ。否定（``None``）を
    覚えておくのは、「この曲にはプレビューが無い」ことが分かった后再び
    ネットワークを叩く無駄を避けるため。肯定结果的 TTL は
    ``ttl_seconds`` で期限切れにする（iTunes の URL は永久とは限らない）。

    **否定結果にも TTL を設ける**（``negative_ttl_seconds``、既定 5 分）。
    これを設けないと、ネットワークの一時的な失敗・iTunes 側の障害・
    実装の不具合で入った「音源なし」が**恒久的に固定**され、その曲が
    永久に間奏になる。肯定結果より短くして「本当に無い曲」を叩き続ける
    無駄と、「一時的な失敗」の恒久固定を両立させない。

    さらに **ネットワークの失敗はそもそも否定結果を書き込まない**
    共有キャッシュなので、1 回の 5xx が全テナントに 6 時間伝播していた。
    """

    def __init__(
        self,
        path: Optional[str] = None,
        ttl_seconds: float = 7 * 86400.0,
        negative_ttl_seconds: float = 5 * 60.0,
    ) -> None:
        self._store = _shared_store(_resolve_path(path))
        self._ttl = float(ttl_seconds)
        self._negative_ttl = float(negative_ttl_seconds)
        self._disabled = False

    @property
    def path(self) -> Path:
        return self._store.path

    def disable(self) -> None:
        self._disabled = True

    def ensure_schema(self) -> None:
        self._store.ensure_schema()

    def _with_store_url_column(self, operation):
        """`track_view_url` を前提とする SQL を、1 回だけ移行して実行する。

        移行は通常経路では起こらない。共有ストアは**パスごとに 1 インスタンス**
        だけで作られ、`_initialized` はそのパスの最初の 1 回で立つため、
        `PreviewCache.get/put` のたびに移行を確認すると 1 回の番組生成で
        数十回の接続と PRAGMA が重複して開くことになる。

        一方で、**プロセスが生きている間に DB ファイルが別バージョンの
        スキーマへ差し替わる**ことは起こり得る（テストがそうする、
        別バージョンのプロセスと DB ファイルを共有する場合など）。
        そのため通常経路は高速化し、**`no such column` が出たときだけ**
        移行して 1 回リトライする。

        Parameters
        ----------
        operation:
            ``connect()`` のコンテキストマネージャを受け取り、接続内で
            SQL を実行して戻り値を返す呼び出し。
        """
        try:
            return operation(self._store.connect)
        except SongStoreError as exc:
            if "no such column" not in str(exc):
                raise
            logger.info("キャッシュ DB に track_view_url が無いため、その場で移行します")
            self._store.migrate_store_url_column()
            return operation(self._store.connect)

    def get(self, song_key: str) -> Optional[Dict[str, Optional[str]]]:
        """キャッシュ済みなら ``{preview_url, artwork_url, track_view_url}``、無ければ ``None``。

        肯定結果も否定結果も TTL で期限切れにして「再解決してください」を返す。
        """
        if self._disabled:
            return None

        def _query(connect):
            with connect() as conn:
                return conn.execute(
                    "SELECT preview_url, artwork_url, track_view_url, checked_at "
                    "FROM song_preview WHERE song_key = ?",
                    (str(song_key),),
                ).fetchone()

        try:
            self.ensure_schema()
            row = self._with_store_url_column(_query)
        except SongStoreError:
            logger.warning("プレビューキャッシュを読み込めません", exc_info=True)
            return None
        if not row:
            return None
        preview_url, artwork_url, track_view_url, checked_at = row
        age = time.time() - float(checked_at)
        if preview_url:
            if age > self._ttl:
                return None
        elif age > self._negative_ttl:
            # 否定結果も期限切れにする。一時的な失敗で恒久的に無音になるのを防ぐ。
            return None
        return {
            "preview_url": preview_url,
            "artwork_url": artwork_url,
            "track_view_url": track_view_url,
        }

    def put(
        self,
        song_key: str,
        preview_url: Optional[str],
        artwork_url: Optional[str],
        track_view_url: Optional[str] = None,
    ) -> None:
        """解決結果（``None`` 含む）を保存する。失敗しても選曲は続行する。"""
        if self._disabled or not song_key:
            return

        def _write(connect):
            with connect() as conn:
                result = conn.execute(
                    "INSERT INTO song_preview "
                    "(song_key, preview_url, artwork_url, track_view_url, checked_at) "
                    "VALUES (?, ?, ?, ?, ?) "
                    "ON CONFLICT(song_key) DO UPDATE SET "
                    "preview_url = excluded.preview_url, "
                    "artwork_url = excluded.artwork_url, "
                    "track_view_url = excluded.track_view_url, "
                    "checked_at = excluded.checked_at",
                    (
                        str(song_key),
                        preview_url,
                        artwork_url,
                        track_view_url,
                        time.time(),
                    ),
                )
                # `connect()` は終了時に commit しない（closed されるだけ）。
                conn.commit()
                return result

        try:
            self.ensure_schema()
            self._with_store_url_column(_write)
        except SongStoreError:
            logger.warning("プレビューキャッシュを保存できませんでした", exc_info=True)

    def stats(self) -> Dict[str, int]:
        """検証・デバッグ用の件数。"""
        self.ensure_schema()
        with self._store.connect() as conn:
            total = conn.execute("SELECT COUNT(*) FROM song_preview").fetchone()[0]
            with_preview = conn.execute(
                "SELECT COUNT(*) FROM song_preview WHERE preview_url IS NOT NULL"
            ).fetchone()[0]
        return {"total": int(total), "with_preview": int(with_preview)}


__all__ = [
    "PreviewCache",
    "SongHistoryStore",
    "SongStoreError",
]
