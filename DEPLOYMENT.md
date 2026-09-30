# レトロラジオ・タイムマシン デプロイメントガイド

## 前提条件
- Python 3.11以上
- インターネット接続（初回セットアップ時）
- DBマイグレーション用に `alembic`（`pip install alembic`）

## アプリケーションの起動方法

このプロジェクトは **Streamlit を完全に廃止**し、**FastAPI + 静的SPA** で動作します。
起動コマンドは以下のみです（`streamlit run` は存在しません）。

```bash
# モジュール形式（推奨）
uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501

# プログラム形式
python -c "import uvicorn; uvicorn.run('retro_radio.server:app', host='0.0.0.0', port=8501)"
```

Windows なら `run_retro_radio.bat`（英語）または `run_retro_radio_ja.bat`（日本語）を
ダブルクリックしてください。開発者は `debug.bat` がリロード有効・ログ詳細表示で起動します。

| エンドポイント | 用途 | レスポンス |
|---|---|---|
| `GET /` | SPA (`static/index.html`) | 200 / HTML |
| `GET /health` | **ヘルスチェック用**（JSON） | 200 / JSON |
| `GET /api/decades` | 対応年代一覧 | 200 / JSON |
| `GET /api/audio/{filename}` | TTS音声配信 | 200 / audio-mpeg |
| `POST /api/generate` | 番組生成 | 200 / JSON |

> `GET /` は SPA の HTML を返すだけなのでヘルスチェックには使えません。
> 旧設定の `/?health=check` は削除済みです。**`/health` を使用してください。**
>
> `/health` の `status` は `RETRO_RADIO_GEMINI_API_KEY` 未設定時に `degraded` になります。
> これは正常な応答（HTTP 200）です。

## ローカルデプロイ
1. リポジトリクローン
2. `pip install -r requirements.txt`（マイグレーションが必要なら `pip install alembic`）
3. `.env.example` を `.env` にコピーし、必要な値を設定
   ```bash
   cp .env.example .env        # Windows: copy .env.example .env
   ```
4. `alembic upgrade head`（テーブル作成・外部キー制約の適用）
5. `uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501`
6. ブラウザで http://localhost:8501 アクセス

### 必要な環境変数
| 変数 | 必須 | 説明 |
|---|---|---|
| `RETRO_RADIO_GEMINI_API_KEY` | 推奨 | 未設定でもフォールバックで起動するが、`/health` が `degraded` になる |
| `RETRO_RADIO_SECRET_KEY` | 本番必須 | 空だと起動時ログに `RuntimeWarning`。セッション/認証機能が使えない |
| `RETRO_RADIO_DATABASE_URL` | 本番 | 既定は `sqlite:///./retro_radio.db`、本番は PostgreSQL |
| `RETRO_RADIO_CORS_ORIGINS` | 同一オリジン外を使う場合 | 既定は `http://localhost:8501,http://127.0.0.1:8501` |

`.env.example` は `retro_radio/config.py` の `Settings` 全フィールドと1対1に対応しています。

> **重要: List 型の設定は JSON 配列で書きます。**
> `RETRO_RADIO_CORS_ORIGINS` は pydantic-settings が **JSON として解析する**ため、
> カンマ区切り（`http://a,http://b`）でも単独の `*` でも **起動時に `SettingsError`** になります。
> 正しい形式は次のとおりです。
> ```bash
> RETRO_RADIO_CORS_ORIGINS=["http://localhost:8501","http://127.0.0.1:8501"]
> ```
> `.env` ファイルでも、PaaS の環境変数でも同じ JSON 形式が必要です。

## データベースマイグレーション

`Base.metadata.create_all()` は **新規テーブルの作成のみ** を行い、既存テーブルの
定義（外部キー制約・インデックス・制約の追加や削除）を更新しません。
スキーマの変更は必ず Alembic で適用してください。

```bash
alembic upgrade head        # 適用
alembic downgrade -1        # 1つ戻す
alembic history             # revision チェーンの確認
alembic revision --autogenerate -m "..."   # models との差分を新規 revision 化
```

`alembic.ini` の `script_location` は `db/migrations` です。`db/migrations/env.py` は
`retro_radio.config.get_settings()` 経由で **アプリと同じ** `RETRO_RADIO_DATABASE_URL` / `.env`
を使うため、マイグレーションとアプリのDBが食い違うことはありません。

既存の `retro_radio.db`（Alembic未適用・テーブル定義が古い）がある場合は、
`alembic stamp 54157f820607` でバージョンを合わせてから `alembic upgrade head` を実行します。

## Dockerデプロイ

```bash
docker build -t retro-radio .
docker run -p 8501:8501 -e RETRO_RADIO_GEMINI_API_KEY=xxxx retro-radio
```

- イメージは `uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501` で起動します
- `HEALTHCHECK` は `GET /health` を `curl` で確認します（`curl` はイメージに同梱済み）
- マイグレーションはコンテナ起動時に自動実行されません。`docker run --rm retro-radio alembic upgrade head` などで先に適用してください
- `requirements.txt` には含まれませんが、イメージには `alembic` を導入済みです

## PaaSデプロイ

いずれもポート 8501・ヘルスチェック `/health`・起動 `uvicorn` に統一済みです。

| ファイル | 対象 | 備考 |
|---|---|---|
| `render.yaml` | Render | `preDeployCommand: alembic upgrade head`、ポートは `$PORT` |
| `railway.json` | Railway | `healthcheckPath: /health`、ポートは `$PORT` |
| `fly.toml` | Fly.io | `release_command = "alembic upgrade head"`、ポート 8501 |

### Streamlit Cloud について
`streamlit run` を前提としたデプロイは**サポート対象外**です。
Streamlit は依存関係・コード・Dockerfile・各PaaS設定から完全に削除されています。

## モニタリング項目
- レイテンシ（目標: 5秒以内）
- エラー率（目標: 1%未満）
- キャッシュヒット率（TTSキャッシュは起動時と `RETRO_RADIO_TTS_CACHE_SWEEP_INTERVAL` ごとに掃除）
- 同時生成数（`RETRO_RADIO_MAX_CONCURRENT_GENERATIONS` を超えると 503）
- `/health` の `status` が `degraded` でないこと
