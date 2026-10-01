# データベースマイグレーション手順

このプロジェクトでは、SQLAlchemy と Alembic を使用してデータベースマイグレーションを管理しています。

## 開発環境での初期化

`init_db()` 関数（`retro_radio/db/session.py`）でテーブルを作成できます。
**`init_db()` はアプリケーション起動時に自動呼び出しされません。**
呼ぶのは `scripts/init_db.py`（または対話的に Python から直接）です。

```bash
python scripts/init_db.py
```

```python
# スクリプトとしてではなく手動で使う場合
from retro_radio.db.session import init_db
init_db()
```

## 本番環境でのマイグレーション（Alembic）

本環境ではマイグレーションは **Alembic が唯一の方法**です。
`init_db()` は `Base.metadata.create_all()` に相当するため、**新規テーブルの作成のみ**を
行います。既存テーブルの定義変更（外部キー・インデックス・制約）は反映されません。

### Alembic のインストール

```bash
pip install alembic
```

### 設定（すでに設定済みです）

このリポジトリには **`alembic.ini` と `db/migrations/`（`env.py` / `versions/` / `script.py.mako`）が
最初から含まれています**。`alembic init alembic` を実行する必要はありません。
パスを新規に作る場合は **Y ではなく `db/migrations`** にしてください。

- `alembic.ini` の `script_location` は **`db/migrations`**（`alembic/` ではありません）
- `db/migrations/env.py` は `retro_radio.config.get_settings()` 経由で
  **アプリと同じ** `RETRO_RADIO_DATABASE_URL` と `.env` を使うため、
  マイグレーションとアプリの DB が食い違うことはありません
- `target_metadata` は `retro_radio.db.models.Base.metadata` です

### マイグレーションスクリプトの生成

モデルに変更を加えた後、次のコマンドでマイグレーションスクリプトを自動生成します：

```bash
alembic revision --autogenerate -m "describe your changes"
```

これにより、`db/migrations/versions/` ディレクトリに
`54157f820607_describe_your_changes.py` のようなファイルが生成されます。
リビジョン ID の規則は [`migration_rules.md`](migration_rules.md) を参照してください。

### マイグレーションの適用

生成されたマイグレーションスクリプトをデータベースに適用するには、次のコマンドを実行します：

```bash
alembic upgrade head
```

### マイグレーションのロールバック

必要に応じて、マイグレーションをロールバックすることもできます：

```bash
# 1ステップ戻す
alembic downgrade -1
# 特定のリビジョンに戻す
alembic downgrade <revision_id>
```

## 注意点

- 既存 DB のスキーマ変更は `init_db()` では反映されません。必ず Alembic を使ってください
- マイグレーションスクリプトはバージョン管理システムにコミットしてください
- マイグレーションを適用する前に、必ずデータベースのバックアップを取ってください
  （[`db_backup.md`](db_backup.md)。**`cp` ではなく SQLite の `.backup` API**）

## トラブルシューティング

### 「アレムビックが設定されていない」エラー

`alembic` コマンドが見つからない場合は、`PATH` に Alembic のインストールディレクトリが含まれているか確認してください。

### マイグレーションの自動生成が機能しない

`db/migrations/env.py` で `target_metadata` が正しく設定されているか確認してください。
プロジェクトのモデルがインポートされていることを確認してください。

### `alembic check` が差分を報告する

`alembic check` は「モデル定義とマイグレーションの同期」を確認します。
差分が報告された場合は `alembic revision --autogenerate` で新規 revision を作成して
適用してください（CI の `Verify migrations are in sync` ステップがこれを行います）。

## 参考リンク

- [Alembic Documentation](https://alembic.sqlalchemy.org/en/latest/)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/en/14/)