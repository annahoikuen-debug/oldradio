# レトロラジオ・タイムマシン デプロイメントガイド

## 前提条件
- Python 3.11以上
- インターネット接続（初回セットアップ時）
- DBマイグレーション用の `alembic`（`requirements.txt` に同梱済み。別途 `pip install alembic` は不要）

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
| `POST /api/generate` | 番組生成（同期） | 200 / JSON |
| `POST /api/jobs` | 番組生成（非同期ジョブ受付） | 202 / JSON |
| `GET /api/jobs/{job_id}` | ジョブ状態（ポーリング） | 200 / JSON |
| `DELETE /api/jobs/{job_id}` | ジョブのキャンセル | 200 / JSON |
| `GET /api/jobs/{job_id}/events` | 進捗の SSE | 200 / text-event-stream |
| `POST /api/auth/session` | ログイン（セッション Cookie 発行） | 200 / JSON |

> 認証を有効にした状態（既定）では `/api/generate`・`/api/jobs*`・`/api/audio/*` は
> 資格情報を要求し、鍵が未設定なら **503** を返します（fail-closed）。
> 詳細は [`docs/privacy_and_tenancy.md`](docs/privacy_and_tenancy.md) を参照。

> `GET /` は SPA の HTML を返すだけなのでヘルスチェックには使えません。
> 旧設定の `/?health=check` は削除済みです。**`/health` を使用してください。**
>
> `/health` の `status` は `RETRO_RADIO_GEMINI_API_KEY` 未設定時に `degraded` になります。
> これは正常な応答（HTTP 200）です。

## ローカルデプロイ
1. リポジトリクローン
2. `pip install -r requirements.txt`（`alembic` を含む）
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
| `RETRO_RADIO_DATABASE_URL` | 本番 | 既定は `sqlite:///./retro_radio.db`、本番は PostgreSQL（`postgresql+psycopg://`） |
| `RETRO_RADIO_CORS_ORIGINS` | 同一オリジン外を使う場合 | 既定は `http://localhost:8501,http://127.0.0.1:8501`（CSV / JSON 配列 / `*` を受け付けます） |
| `RETRO_RADIO_REQUIRE_AUTH` | 本番必須（既定 `1`） | `/api/generate` と `/api/audio/*` の認証要否。`1` のまま鍵が無いと **503（fail-closed）** |
| `RETRO_RADIO_SINGLE_USER_KEY` | 個人モードで任意 | 単一ベアラー資格情報。`SECRET_KEY` が無いときの認証手段 |
| `RETRO_RADIO_ADMIN_EMAILS` | 任意 | 初回管理者として当てるメールアドレス（CSV / JSON 配列） |

`.env.example` は `retro_radio/config.py` の `Settings` 全フィールドと1対1に対応しています。
（末尾の「Settings 外で読まれる環境変数」節のみ例外です）

> **PostgreSQL を使う場合**: ドライバは `psycopg` 3.x（`requirements.txt` の
> `psycopg[binary]`）です。URL スキームは **`postgresql+psycopg://`** を使ってください
> （`postgresql://` や `postgresql+psycopg2://` は方言が見つかりません）。

> **重要: `RETRO_RADIO_CORS_ORIGINS` は複数の表記を受け付けます。**
> このフィールドは `NoDecode` 付きで、`split_cors_origins` の `mode="before"`
> バリデータが **人の手で書くすべての表記**を正規化してから検証に入ります
> （JSON 配列 / カンマ区切り / 単独の `*` / 単独の origin）。
> **CSV や `*` で `SettingsError` になることはありません。** 次のどれでも起動します。
> ```bash
> # JSON 配列
> RETRO_RADIO_CORS_ORIGINS=["http://localhost:8501","http://127.0.0.1:8501"]
> # カンマ区切り
> RETRO_RADIO_CORS_ORIGINS=http://localhost:8501,http://127.0.0.1:8501
> # ワイルドカード（RETRO_RADIO_CORS_ALLOW_CREDENTIALS=1 とは組み合わせ不可）
> RETRO_RADIO_CORS_ORIGINS=*
> ```
> `.env` ファイルでも、PaaS の環境変数でも同じです。
>
> ただし `RETRO_RADIO_CORS_ORIGINS=*` と `RETRO_RADIO_CORS_ALLOW_CREDENTIALS=1`
> の組み合わせだけは `reject_cors_wildcard_with_credentials` が拒否します
> （ブラウザが攻撃者のオリジンを反射し、資格情報つきリクエストまで通ってしまうため）。

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
`alembic stamp head` でバージョンを head に合わせてから `alembic upgrade head` を実行します。
**`head` 以外のリビジョン ID をハードコードしないでください**（リビジョンが増えると
指しているリビジョンが古くなり、意図しない downgrade の起点になります）。

## Dockerデプロイ

```bash
docker build -t retro-radio .
docker run -p 8501:8501 -e RETRO_RADIO_GEMINI_API_KEY=xxxx retro-radio
```

- イメージは `sh -c 'alembic upgrade head && exec uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501'` で起動します
- `HEALTHCHECK` は `GET /health` を `curl` で確認します（`curl` はイメージに同梱済み）
- **マイグレーションはコンテナ起動時に自動実行されます**（`Dockerfile:77` の `CMD` が `alembic upgrade head` を先に走らせる）。手動で `docker run --rm retro-radio alembic upgrade head` を実行する必要はありません
- **`alembic` は `requirements.txt` に含まれています**（`alembic>=1.13.0,<2.0.0`）。`Dockerfile:21-25` も `requirements.txt` を唯一の正本としている。`pip install alembic` を別途行うと導入バージョンが二重管理になるため**不要**
- 起動時にスキーマが未準備だとアプリは**起動を拒否**する（`retro_radio.server._require_database_schema`）。`no such table` の 500 を起動時に出す代わりに、原因が分かるエラーで止まる

## PaaSデプロイ

いずれもポート 8501・ヘルスチェック `/health`・起動 `uvicorn` に統一済みです。

| ファイル | 対象 | 備考 |
|---|---|---|
| `render.yaml` | Render | `startCommand` が `alembic upgrade head` を実行（`preDeployCommand` は使わない）、ポートは `$PORT` |
| `railway.json` | Railway | `healthcheckPath: /health`、ポートは `$PORT` |
| `fly.toml` | Fly.io | `[processes].app` が `alembic upgrade head` を実行（`release_command` は**意図的に使わない**。ボリュームをマウントしない release 実行だと ephemeral FS に書いてしまうため）、ポート 8501 |

### Streamlit Cloud について
`streamlit run` を前提としたデプロイは**サポート対象外**です。
Streamlit は依存関係・コード・Dockerfile・各PaaS設定から完全に削除されています。

## モニタリング項目
- レイテンシ（目標: 5秒以内）
- エラー率（目標: 1%未満）
- キャッシュヒット率（TTSキャッシュは起動時と、`RETRO_RADIO_TTS_CACHE_SWEEP_INTERVAL`（既定 50）**回**の `generate_tts_cached` 呼び出しごとに掃除。時間間隔ではありません）
- 同時生成数（`RETRO_RADIO_MAX_CONCURRENT_GENERATIONS` を超えると 503）
- `/health` の `status` が `degraded` でないこと
