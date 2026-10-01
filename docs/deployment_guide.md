# デプロイメントガイド

> **正本は [`../DEPLOYMENT.md`](../DEPLOYMENT.md) です。**
> 本書は「`DEPLOYMENT.md` にない補足（チェックリスト・監視・ロールバック）」をまとめたものです。
> 起動コマンドや CORS・マイグレーションの契約が食い違う場合は
> **`DEPLOYMENT.md` を優先してください。**
>
> 旧版（`streamlit run app.py` / `retro_radio.main:app` / プレフィックスなしの環境変数 /
> `alembic/` パス）は完全に書き直しました。Streamlit 時代の記述は残りません。

---

## 1. 起動エンティティ

Streamlit は廃止済みです。起動は必ず `uvicorn` 経由です。

| 対象 | 起動コマンド |
|---|---|
| モジュール形式（推奨） | `uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501` |
| プログラム形式 | `python -c "import uvicorn; uvicorn.run('retro_radio.server:app', host='0.0.0.0', port=8501)"` |
| 開発（リロード有効） | `uvicorn retro_radio.server:app --port 8501 --reload` |
| Windows | `run_retro_radio.bat` / `run_retro_radio_ja.bat`（`debug.bat` は `--reload` + 詳細ログ） |
| Docker | `docker build -t retro-radio . && docker run --rm -p 8501:8501 --env-file .env retro-radio` |

`retro_radio.main:app` というモジュールは**存在しません**（`ModuleNotFoundError`）。
ASGI アプリの実体は `retro_radio.server:app` です。

---

## 2. 環境変数

**すべての変数名に `RETRO_RADIO_` プレフィックスが付きます。**
`config.py` の `env_prefix="RETRO_RADIO_"` / `case_sensitive=False` のためです。
素の `GEMINI_API_KEY` や `DATABASE_URL`、`.streamlit/secrets.toml` は**読み込まれません**
（このプロジェクトで参照するコードは 0 件）。

### 2.1 最低限

| 変数名 | 説明 | 既定値 |
|---|---|---|
| `RETRO_RADIO_GEMINI_API_KEY` | Gemini API キー。未設定でも起動するが `/health` が `degraded` になり定型原稿モードになる | （空） |
| `RETRO_RADIO_SECRET_KEY` | セッション署名と画面ログインの鍵。認証を使う構成では**必須** | （空） |
| `RETRO_RADIO_DATABASE_URL` | 接続URL。SQLite が既定 | `sqlite:///./retro_radio.db` |
| `RETRO_RADIO_REQUIRE_AUTH` | `/api/generate` と `/api/audio/*` の認証要否 | `1`（安全側） |

### 2.2 任意（抜粋）

| 変数名 | 説明 | 既定値 |
|---|---|---|
| `RETRO_RADIO_SINGLE_USER_KEY` | 個人モード用の単一ベアラー資格情報 | （空） |
| `RETRO_RADIO_ADMIN_EMAILS` | bootstrap 管理者（CSV / JSON 配列 / 単独のいずれも可） | （空） |
| `RETRO_RADIO_CORS_ORIGINS` | CSV / JSON 配列 / `*` / 単独 origin のすべてを受け付けます | `http://localhost:8501,http://127.0.0.1:8501` |
| `RETRO_RADIO_CSP` | CSP ヘッダの完全上書き（`Settings` の外で読む） | （空） |
| `RETRO_RADIO_HSTS_ENABLED` | HSTS の有無（`Settings` の外で読む） | `1` |
| `RETRO_RADIO_HSTS_MAX_AGE` | HSTS `max-age`（秒） | `31536000` |
| `RETRO_RADIO_FULL_SCRIPT_TTS` | 全体版ナレーション TTS。`0` で無効化 | `1` |
| `RETRO_RADIO_DEBUG` | デバッグモード | `false` |
| `RETRO_RADIO_PORT` | ポート番号 | `8501` |

全フィールドの正本は [`../.env.example`](../.env.example) と
`retro_radio/config.py` です。`DEBUG` / `LOG_LEVEL` / `PORT` という
**プレフィックスなしの名前は読み込まれません**。

### 2.3 `.env` ファイル

```bash
cp .env.example .env          # Windows: copy .env.example .env
```

`Settings` は `@lru_cache()` で**1 プロセスに 1 回だけ**生成されます。
`.env` は起動時にのみ読み込まれるので、変数を変更したら**必ず再起動**してください。

---

## 3. データベースマイグレーション

```bash
pip install alembic
alembic upgrade head
```

`alembic.ini` の `script_location` は **`db/migrations`** です（`alembic/` ではありません）。
`db/migrations/env.py` は `get_settings()` 経由でアプリと同じ `.env` /
`RETRO_RADIO_DATABASE_URL` を使うため、マイグレーションとアプリの DB が食い違います。

その他の操作とロールバック手順は
[`migrations.md`](migrations.md) と [`migration_rules.md`](migration_rules.md)、
PaaS / Docker での適用方法は [`../DEPLOYMENT.md`](../DEPLOYMENT.md) を参照してください。

---

## 4. デプロイ前チェックリスト

- [ ] 環境変数すべて設定済み（特に `RETRO_RADIO_REQUIRE_AUTH` と認証の資格情報）
- [ ] `alembic upgrade head` 完了 / `alembic check` が差分なし
- [ ] `GET /health` が 200 を返し、`status` / `auth_required` / `auth_ready` が意図した値
- [ ] CORS 設定が実際の公開オリジンに合っている（`Settings` の外の 4 変数は `.env` にあるか）
- [ ] TLS 終端（ロードバランサ / リバースプロキシ）と、リバースプロキシ側の
      SSE バッファリング無効化（`X-Accel-Buffering: no`）
- [ ] バックアップスケジュール設定（`db_backup.md`。**`cp` ではなく SQLite `.backup`**）
- [ ] ログローテーション設定
- [ ] 静的ファイル配信設定（`/static` はアプリ自身が `StaticFiles` で配信します）

---

## 5. ロールバック手順

```bash
git checkout <previous-tag>
alembic downgrade -1
# コンテナ / PaaS の再起動
```

`alembic downgrade` の後は必ず [`db_backup.md`](db_backup.md) に従って
退避前の DB を [`db_backup.md`](db_backup.md) の手順で `.backup` で取得し、退避の
**前に**置いてください。

## 6. 監視

- ヘルスチェック: `GET /health`
- 主な観測点: レイテンシ / エラー率 / TTS キャッシュヒット率 / 同時生成数 / `auth_*`
- 詳細: [`../OPERATIONS.md`](../OPERATIONS.md)
