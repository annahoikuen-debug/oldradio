# マイグレーション運用ルール

対象: `db/migrations/versions/` と `alembic.ini`（`script_location` は `db/migrations`）。
手順は [`migrations.md`](migrations.md) を参照。

## 命名規則

- リビジョンID: **Alembic の既定（12 桁のランダム hex ハッシュ）**。時刻を埋め込みません
- ファイル名: `<revision_id>_<slug>.py`（`--autogenerate -m "change summary"` が slug を生成）
- 例（`db/migrations/versions/` の実在ファイル）:
  - `125758440996_initial_migration.py`
  - `54157f820607_test_autogenerate.py`
  - `88ad9aef7f29_add_fk_cascade.py`
  - `2c1f5a9b3d47_add_privacy_and_tenancy_tables.py`
  - `d7a3f0b1c9e4_normalize_plan_values.py`

> **タイムスタンプID（`20260923120000_add_user_index` 形式）は使いません。**
> リビジョン間の依存は `down_revision` で表現します。
> 時刻をIDに埋め込むと、二つのブランチが同じIDを生んで衝突し、
> 後からIDの改名が必要になります。

## 変更内容（メッセージ）の書き方

`-m` は**英語の slug** にします。本文だけで差分が分かるようにするのが目的です。

| 種別 | メッセージ例 |
|------|-------------|
| テーブル追加 | `create_users_table` |
| カラム追加 | `add_email_to_users` |
| カラム名変更 | `rename_email_to_email_address` |
| カラム削除 | `remove_legacy_plan_column` |
| インデックス追加 | `add_email_index` |
| 外部キー追加 | `add_fk_cascade` |
| 型変更 | `change_plan_type_enum` |

## 作成手順

1. モデル変更後に自動生成

   ```bash
   alembic revision --autogenerate -m "change summary in english"
   ```

2. 生成されたファイルを確認・編集
   - `downgrade()` が正しく書かれているか確認する
     （Alembic の自動生成は `downgrade()` を空のままにします。
     特に列削除・型変更・NOT NULL 追加は手書きが必須です）
   - SQLite では `ALTER TABLE ... DROP COLUMN` などが未実装のため、
     バッチモード（`op.batch_alter_table`）が必要になることがあります
   - テーブルを作り直すバッチ操作は、既存データを失うため必ず確認すること

3. 本番適用前は必ず SQL を確認

   ```bash
   alembic upgrade head --sql
   ```

## 適用手順

### 開発環境

```bash
alembic upgrade head
alembic check        # モデル定義とマイグレーションの差分がないことの確認
```

### 本番環境

1. SQL 確認

   ```bash
   alembic upgrade head --sql > migration.sql
   ```

2. レビュー後、本番DBで実行

   ```bash
   alembic upgrade head
   ```

3. 適用直後に `alembic current` が `head` と一致することを確認する

## ロールバック手順

```bash
# 1つ前のリビジョンに戻す
alembic downgrade -1

# 特定リビジョンまで戻す
alembic downgrade <revision_id>
```

## 注意事項

- 本番適用前は必ず [`db_backup.md`](db_backup.md) に従ってバックアップを取得する
  （**`cp` ではなく SQLite の `.backup` API**。WAL 下の `cp` は直近のコミットを失う）
- 既存 DB のバージョンを合わせるだけなら `alembic upgrade head` ではなく
  **`alembic stamp head`** を使う。リビジョンIDをハードコードしない
- ダウンタイムが発生する可能性がある操作は注意（カラム削除、型変更、
  インデックス作成など）
- 大量データの移行は段階的に実施する
- ロールバック手順（`downgrade()` の内容）を適用前に確認しておく
- CI の `Verify migrations are in sync` ステップは
  `alembic upgrade head` → `alembic check` を実行します。
  差分が報告された場合は PR を落とします
## レビュー項目

- [ ] SQL が正しいか（`--sql` で確認）
- [ ] `downgrade()` が正しく書かれているか
- [ ] 既存データへの影響がないか
- [ ] インデックスは適切か
- [ ] 外部キー制約は適切か
- [ ] SQLite で実際に適用できるか（`alembic upgrade head` を空 DB で試す）
- [ ] パフォーマンス影響は許容範囲か
