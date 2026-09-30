# データベースマイグレーション手順

このプロジェクトでは、SQLAlchemy と Alembic を使用してデータベースマイグレーションを管理しています。

## 開発環境での初期化

開発環境では、`init_db()` 関数を使用してテーブルを作成できます。この関数は `retro_radio/db/session.py` に定義されており、アプリケーション起動時に自動的に呼び出されます。

```python
from retro_radio.db.session import init_db
init_db()
```

## 本番環境でのマイグレーション（Alembic 推奨）

本番環境では、マイグレーションを管理するために Alembic を使用することを強く推奨します。

### Alembic のインストール

```bash
pip install alembic
```

### Alembic の初期化

プロジェクトのルートディレクトリで、次のコマンドを実行します：

```bash
alembic init alembic
```

これにより、`alembic` ディレクトリが作成され、設定ファイルが生成されます。

### 設定の調整

生成された `alembic.ini` ファイルの `sqlalchemy.url` を環境に合わせて設定します。環境変数 `DATABASE_URL` を使用することを推奨します。

また、`alembic/env.py` ファイルを編集し、プロジェクトのモデルをインポートして自動生成を有効にします。

```python
# alembic/env.py
from retro_radio.db import models
from retro_radio.db.session import engine

# この行を追加してモデルをインポート
target_metadata = models.Base.metadata
```

### マイグレーションスクリプトの生成

モデルに変更を加えた後、次のコマンドでマイグレーションスクリプトを自動生成します：

```bash
alembic revision --autogenerate -m "describe your changes"
```

これにより、`alembic/versions` ディレクトリに新しいマイグレーションスクリプト Jul 24 12:34:56 2026_describe_your_changes.py` が生成されます。

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

- 開発環境では `init_db()` を使用しても問題ありませんが、本番環境では必ず Alembic を使用してください。
- マイグレーションスクリプトはバージョン管理システムにコミットしてください。
- マイグレーションを適用する前に、必ずデータベースのバックアップを取ってください。

## トラブルシューティング

### 「アレムビックが設定されていない」エラー

`alembic` コマンドが見つからない場合は、`PATH` に Alembic のインストールディレクトリが含まれているか確認してください。

### マイグレーションの自動生成が機能しない

`alembic/env.py` で `target_metadata` が正しく設定されているか確認してください。プロジェクトのモデルがインポートされていることを確認してください。

## 参考リンク

- [Alembic Documentation](https://alembic.sqlalchemy.org/en/latest/)
- [SQLAlchemy Documentation](https://docs.sqlalchemy.org/en/14/)