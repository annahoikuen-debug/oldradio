# トラブルシューティングガイド

## 起動に関する問題

### Q: `streamlit run` コマンドが見つからない / 使えない
A: Streamlit はこのプロジェクトから完全に廃止されました。
起動は uvicorn のみです。

```bash
uvicorn retro_radio.server:app --host 0.0.0.0 --port 8501
```

Windows なら `run_retro_radio.bat` / `run_retro_radio_ja.bat`、
開発者は `debug.bat`（`--reload` + `--log-level debug`）を使えます。

### Q: サーバーが起動直後に落ちる
A: ログを確認してください。

- `ModuleNotFoundError: No module named 'fastapi'` → `pip install -r requirements.txt`
- `Address already in use` → 8501番ポートが使用中です（`netstat -ano | findstr :8501`）
- `RuntimeWarning: RETRO_RADIO_SECRET_KEY が未設定です` → 警告のみ。起動は続行しますが
  セッション/認証機能は使えません。本番では必ず設定してください
- SQLite ファイルがロック中 → `PRAGMA busy_timeout=60000` / WAL 設定が `db/session.py` にあります

## 設定・環境変数に関する問題

### Q: 「Gemini APIキーが設定されていません」と表示される
A:
1. https://aistudio.google.com/app/apikey でAPIキーを取得
2. 環境変数として設定:
   - Windows: `set RETRO_RADIO_GEMINI_API_KEY=your-key-here`
   - Linux/Mac: `export RETRO_RADIO_GEMINI_API_KEY=your-key-here`
3. または `.env` ファイルを作成（`.env.example` をコピーして編集）:
   ```
   RETRO_RADIO_GEMINI_API_KEY=your-key-here
   ```
4. アプリを再起動

> 変数名には必ず `RETRO_RADIO_` プレフィックスが付きます。
> `config.py` の `env_prefix="RETRO_RADIO_"` / `case_sensitive=False` のためです。
> そのため素の `GEMINI_API_KEY` は読み込まれません。
>
> なお、Streamlit 時代の `.streamlit/secrets.toml` / `config.toml` 読み込み機構は
> 存在しません。万一 `secrets.toml` を作成しても**読み込まれません**（この
> プロジェクトで参照するコードは 0 件）。API キーは必ず `.env` または
> プロセス環境変数で渡してください。

### Q: 変更した環境変数が反映されない
A: `Settings` は `@lru_cache()` で1プロセスに1回だけ生成されます。
サーバーを再起動してください。`.env` は起動時にのみ読み込まれます。

### Q: `SettingsError: error parsing value for field "cors_origins"` で起動しない
A: `RETRO_RADIO_CORS_ORIGINS` は `List[str]` 型のため、pydantic-settings が
**JSON として解析**します。カンマ区切りでも単独の `*` でも失敗します。

```bash
# 正しい（JSON 配列）
RETRO_RADIO_CORS_ORIGINS=["http://localhost:8501","http://127.0.0.1:8501"]

# 誤り（いずれも SettingsError）
# RETRO_RADIO_CORS_ORIGINS=http://localhost:8501,http://127.0.0.1:8501
# RETRO_RADIO_CORS_ORIGINS=*
```

PaaS の環境変数（`render.yaml` / `fly.toml` / Railway の Variables）でも同じ JSON 形式が必要です。
`.env` を完全に削除すればコード既定値（localhost のみ）に戻ります。

> 補足: `config.py` の `split_cors_origins` は `pre=True` バリデータですが、
> pydantic-settings の env/dotenv ソースはバリデータより **先に** 値をパースするため、
> JSON 以外の形式を救済できません。

## データベースに関する問題

### Q: `sqlite3.OperationalError: FOREIGN KEY constraint failed` が出る
A: `db/session.py` が `PRAGMA foreign_keys=ON` を有効化しているため、古い
テーブル定義（`ON DELETE CASCADE` なし）のまま `delete_old()` などを実行すると
外部キー違反になります。マイグレーションを適用してください。

```bash
pip install alembic
alembic upgrade head
```

### Q: テーブル定義を変更したのに反映されない
A: `Base.metadata.create_all()` は**新規テーブルの作成のみ**を行います。
既存テーブルは変更されません。必ず `alembic revision --autogenerate` で
migration を作成して適用してください。

### Q: 管理者ユーザー（PROプラン）を作り直したい
A: repository は `flush()` までが責務で、commit は `get_db()` contextmanager のみが
行います。**必ず contextmanager の中で操作すること**が原則です。

```bash
python scripts/init_db.py            # テーブル作成 / マイグレーション適用
python scripts/create_admin.py       # 既定の管理者を作成（PRO）
python scripts/set_admin.py a@b.c   # 既存ユーザーを PRO に変更
```

`get_db_sync()` は後方互換用のラッパーで **commit しません**。commit 漏れの原因になるので
新規コードでは使わないでください。

## ヘルスチェック・運用

### Q: ヘルスチェックの URL は?
A: **`GET /health`** です（JSON を返します）。

```
$ curl http://localhost:8501/health
{"status":"degraded","service":"Retro Radio Time Machine","version":"2.0.0",
 "api_key_configured":false,"secret_key_configured":false}
```

- `status: "degraded"` は `RETRO_RADIO_GEMINI_API_KEY` 未設定の状態で、**HTTP 200 を返します**
  （コンテナ/K8s のヘルスチェックは「到達できること」が目的なので正常です）
- `status: "healthy"` にするには API キーを設定してください
- 旧ドキュメントの `/?health=check` は SPA の HTML を返すだけなので
  ヘルスチェックとして使用できません

### Q: 音声が再生されない
A:
1. ブラウザの音量とタブのミュート状況を確認
2. ネットワーク障害で gTTS / iTunes へ接続できない場合、フォールバック音源に切り替わります
3. ブラウザのコンソールで `/api/audio/{filename}` のレスポンスを確認
4. 別ブラウザ（Chrome推奨）で試す

### Q: ページが読み込み途中で止まる
A:
1. ページを更新（F5またはCtrl+R）
2. ブラウザのキャッシュをクリア（Ctrl+Shift+R）
3. 拡張機能を無効にして試す（特に広告ブロッカー）
4. `GET /health` が 200 を返すか確認する

### Q: 「混雑しています」（HTTP 503）が表示される
A: 同時生成数が `RETRO_RADIO_MAX_CONCURRENT_GENERATIONS`（既定 2）に達しました。
Gemini / gTTS / iTunes はいずれもブロッキングHTTPのため、同時実行数を制限しています。
待って再試行するか、必要なら上限を引き上げてください。

## 運用チェックリスト
- [ ] `GET /health` が 200 を返し `status` が `healthy` であること
- [ ] `RETRO_RADIO_SECRET_KEY` が設定されていること
- [ ] `alembic current` が `head` と一致していること
- [ ] `.env` が `.gitignore` に含まれていること（コミットしない）
- [ ] ページが正常に読み込まれ、CORS設定が実際の公開オリジンに合っていること
- [ ] 音声が正常に再生されていること
- [ ] サーバーコンソールにエラーがないこと
- [ ] `docker logs` / PaaS のログにエラーがないこと
