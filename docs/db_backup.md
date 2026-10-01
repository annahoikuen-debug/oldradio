# データベースバックアップ・リストア手順書

> **`cp` / ファイルコピーで SQLite をバックアップしないでください。**
> 本アプリは WAL モードで動作しています。**直近のコミットはまだメイン DB ファイルに
> 書かれておらず、`-wal` ファイルに残っています。**
> メインファイルだけをコピーすると、直近のコミットが黙って失われます。
> 必ず SQLite の **`.backup` API** を使ってください。

## バックアップ対象

| 対象 | 既定パス | 内容 | 重要度 |
|---|---|---|---|
| アプリDB | `retro_radio.db`（`RETRO_RADIO_DATABASE_URL`） | ユーザー・生成履歴・監査ログ・同意記録など | **必須** |
| 選曲ストアDB | `retro_radio_song_store.db`（`RETRO_RADIO_SONG_STORE_PATH`） | 選曲ローテーション履歴とプレビュー解決結果 | 推奨 |
| TTS キャッシュ | `cache/`（`server.py` の `CACHE_DIR`。テナントごとに `<tenant_id>/`） | 再生成可能な一時ファイル | 任意 |

`RETRO_RADIO_SONG_STORE_PATH` が空のときは、アプリDBと**同じディレクトリ**に
`retro_radio_song_store.db` が作られます。実パスを推論せず、設定で確認してください。

TTS キャッシュは消しても生成し直すだけなので、**障害の直接原因にはなりません**。
退避時間を短縮したいなら、ここは省いて構いません。

---

## SQLite

### バックアップ

```bash
# 方法1（推奨）: sqlite3 コマンドの .backup API
# 稼働中でも一貫したスナップショットが取れる
sqlite3 retro_radio.db ".backup '/backup/retro_radio_$(date +%Y%m%d_%H%M%S).db'"
sqlite3 retro_radio_song_store.db ".backup '/backup/song_store_$(date +%Y%m%d_%H%M%S).db'"

# 方法2: Python の sqlite3 標準ライブラリ（sqlite3 CLI がない環境向け）
python -c "import sqlite3; src=sqlite3.connect('retro_radio.db'); dst=sqlite3.connect('/backup/retro_radio.db'); src.backup(dst); dst.close(); src.close()"

# 方法3: SQL ダンプ（可搬性は高い）
sqlite3 retro_radio.db .dump > retro_radio_backup.sql
```

> **方法1・2 は `cp` ではありません。** `.backup` と `Connection.backup()` は
> SQLite 内部でページ単位に読み、ロックと WAL の状態を処理します。
> `cp retro_radio.db ...` は WAL では不正です。
>
> `-shm` / `-wal` ファイルは**コピー不要**です。`.backup` の出力に統合されます。

### TTS キャッシュの退避（必要な場合のみ）

```bash
# 再生成可能な一時ファイルなのでファイルコピーで構いません。
# ただし実行中のコピーは中途半端な mp3 になることがあるので、
# 可能なら退避中に新規生成を止めてください。
cp -r cache/ /backup/cache_$(date +%Y%m%d)/
```

### 検証

```bash
# バックアップが壊れていないか確認する
sqlite3 /backup/retro_radio_20260930.db "PRAGMA integrity_check;"

# 中身が入っているか確認する
sqlite3 /backup/retro_radio_20260930.db "SELECT count(*) FROM audit_logs;"
```

`integrity_check` が `ok` を返さないバックアップは **使えません**。

### リストア

```bash
# 既存DBへ復元（先にサービスと -wal / -shm を止める）
#   1. サービスを停止
#   2. retro_radio.db-wal と retro_radio.db-shm を削除
#   3. 復元する
sqlite3 retro_radio.db ".restore '/backup/retro_radio_20260930.db'"

# SQL ダンプから復元
sqlite3 retro_radio.db < retro_radio_backup.sql

# ファイル置換（必ずサービス停止後）
#   stop service
#   rm -f retro_radio.db-wal retro_radio.db-shm
#   cp /backup/retro_radio_20260930.db retro_radio.db
#   start service
```

復元後は必ず `alembic current` が `head` と一致することを確認し、
一致しなければ `alembic stamp head` → `alembic upgrade head` で整合させてください。

---

## PostgreSQL（本番環境）

### バックアップ

```bash
# 完全バックアップ（カスタム形式）
pg_dump -U postgres -h localhost -d retro_radio -F c -b -v -f retro_radio_backup.dump

# SQL形式
pg_dump -U postgres -h localhost -d retro_radio > retro_radio_backup.sql
```

### リストア

```bash
# カスタム形式から復元
pg_restore -U postgres -h localhost -d retro_radio -v retro_radio_backup.dump

# SQL形式から復元
psql -U postgres -h localhost -d retro_radio -f retro_radio_backup.sql

# 並列リストア（高速化）
pg_restore -U postgres -h localhost -d retro_radio -j 4 retro_radio_backup.dump
```

---

## 定期バックアップ設定（cron 例）

```bash
# SQLite は .backup API を呼ぶ。cp では代用しない。
0 3 * * * /usr/bin/sqlite3 /srv/retro_radio/retro_radio.db ".backup '/backup/retro_radio_$(date +\%Y\%m\%d).db'"
0 3 * * * /usr/bin/sqlite3 /srv/retro_radio/retro_radio_song_store.db ".backup '/backup/song_store_$(date +\%Y\%m\%d).db'"

# 30日以上前のバックアップを削除
0 4 * * * find /backup -name "retro_radio_*.db" -mtime +30 -delete

# PostgreSQL の場合
0 3 * * * /usr/bin/pg_dump -U postgres -h localhost -d retro_radio -F c -f /backup/retro_radio_$(date +\%Y\%m\%d).dump
```

## 確認事項

- [ ] `.backup`（または `pg_dump`）を使っている — **`cp` を使っていない**
- [ ] アプリDB と選曲ストアDB の両方を採っている
- [ ] `PRAGMA integrity_check` が `ok`
- [ ] バックアップファイルのサイズ確認（前回比で急に減っていないか）
- [ ] リストアテストの実施（月1回）
- [ ] バックアップファイルの暗号化
- [ ] オフサイトバックアップの確保
- [ ] 監査ログの保持期間方針が [`privacy_and_tenancy.md`](privacy_and_tenancy.md) と整合しているか
