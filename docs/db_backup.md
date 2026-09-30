# データベースバックアップ・リストア手順書

## SQLite (開発環境)

### バックアップ
```bash
# 方法1: sqlite3 コマンド
sqlite3 retro_radio.db ".backup retro_radio_backup.db"

# 方法2: ファイルコピー (SQLiteは単一ファイル)
cp retro_radio.db retro_radio_backup.db

# 方法3: SQLダンプ (移植性が高い)
sqlite3 retro_radio.db .dump > retro_radio_backup.sql
```

### リストア
```bash
# 方法1: バックアップファイルから復元
sqlite3 retro_radio.db ".restore retro_radio_backup.db"

# 方法2: SQLダンプから復元
sqlite3 retro_radio.db < retro_radio_backup.sql

# 方法3: ファイル置き換え (シンプル)
cp retro_radio_backup.db retro_radio.db
```

## PostgreSQL (本番環境)

### バックアップ
```bash
# 完全バックアップ
pg_dump -U postgres -h localhost -d retro_radio -F c -b -v -f retro_radio_backup.dump

# SQL形式
pg_dump -U postgres -h localhost -d retro_radio > retro_radio_backup.sql

# 特定スキーマのみ
pg_dump -U postgres -h localhost -d retro_radio -n public > retro_radio_backup.sql
```

### リストア
```bash
# カスタム形式から復元
pg_restore -U postgres -h localhost -d retro_radio -v retro_radio_backup.dump

# SQL形式から復元
psql -U postgres -h localhost -d retro_radio -f retro_radio_backup.sql

# 並列リストア (高速化)
pg_restore -U postgres -h localhost -d retro_radio -j 4 retro_radio_backup.dump
```

## 定期バックアップ設定 (cron例)

```bash
# 毎日午前3時にバックアップ
0 3 * * * /usr/bin/pg_dump -U postgres -h localhost -d retro_radio -F c -f /backup/retro_radio_$(date +%Y%m%d).dump

# 古いバックアップの削除 (30日以上前)
0 4 * * * find /backup -name "retro_radio_*.dump" -mtime +30 -delete
```

## 確認事項

- [ ] バックアップファイルのサイズ確認
- [ ] リストアテストの実施 (月1回)
- [ ] バックアップファイルの暗号化
- [ ] オフサイトバックアップの確保