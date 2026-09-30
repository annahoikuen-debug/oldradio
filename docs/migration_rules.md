# マイグレーション運用ルール

## 命名規則
- リビジョンID: YYYYMMDDHHMMSS 形式のタイムスタンプ + 短い説明
- 例: 20260923120000_add_user_index

## 作成手順
1. モデル変更後に自動生成:
   alembic revision --autogenerate -m "変更内容の説明"
2. 生成されたファイルを確認・編集
3. 本番適用前は必ず --sql で SQL 確認:
   alembic upgrade head --sql

## 適用手順
### 開発環境
alembic upgrade head

### 本番環境
1. SQL 確認:
   alembic upgrade head --sql > migration.sql
2. レビュー後、本番DBで実行:
   alembic upgrade head

## ロールバック手順
# 1つ前のリビジョンに戻す
alembic downgrade -1

# 特定リビジョンまで戻す
alembic downgrade <revision_id>

## 注意事項
- 本番適用前は必ずバックアップを取得
- ダウンタイムが発生する可能性がある操作は注意 (カラム削除、型変更等)
- 大量データの移行は段階的に実施
- ロールバック手順を事前に確認しておく

## 命名規則例
| 種別 | 例 |
|------|-----|
| テーブル追加 | 20260923120000_create_users_table |
| カラム追加 | 20260923120001_add_email_to_users |
| カラム名変更 | 20260923120002_rename_email_to_email_address |
| カラム削除 | 20260923120003_remove_old_column |
| インデックス追加 | 20260923120004_add_email_index |
| 型変更 | 20260923120005_change_plan_type |

## レビュー項目
- [ ] SQL が正しいか (--sql で確認)
- [ ] 既存データへの影響がないか
- [ ] インデックスは適切か
- [ ] 外部キー制約は適切か
- [ ] ダウングレード手順が正しいか
- [ ] パフォーマンス影響は許容範囲か