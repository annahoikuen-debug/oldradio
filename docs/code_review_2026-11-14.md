# コードレビュー — retro_radio 全体精査（2026-11-14）

対象: `ecdef10`（HEAD、作業ツリー クリーン）
方法: 前回レビュー `docs/code_review_2026-11-02.md` の全指摘を現行ツリーで再検証し、直近 13 コミット（c1ecf59〜ecdef10、2026-11-13）の差分を精査。不足領域は筆者が自力でコード読解＋実測プローブを実施。
本レポートは**指摘のみ**を行う（修正は後続の実装計画で対応）。

---

## 0. ゲート実測（すべて HEAD `ecdef10` で実測）

| ゲート | 結果 |
|---|---|
| `python -m pytest tests -q -p no:randomly` | **2876 passed / 7 skipped / 2 deselected / 2 xfailed / 0 failed**（238.74s） |
| `pytest --cov --cov-fail-under=82` | **87.53%** 達成（床 82%） |
| `flake8 tests` | EXIT 0 |
| `flake8 --config .github/flake8-app-baseline.ini retro_radio` | EXIT 0 |
| `flake8 eval scripts utils db` | EXIT 0 |
| `python scripts/mypy_gate.py` | **新規違反 0**（ベースライン 89 件内。素の mypy は 140 errors / 26 files） |
| `python -m eval --offline --threshold 80` | EXIT 0 / **24 of 24** / fact score 平均 100.0 |
| `alembic upgrade head` + `alembic check`（一時 DB） | EXIT 0 / **「No new upgrade operations detected」**（前回の H-1 ドリフトは解消済み） |
| `pip-audit -r requirements.txt` | 脆弱性 0 件 |

---

## 1. 直近 13 コミット（c1ecf59〜ecdef10）の変更概要

`git diff e49a474..HEAD --stat` で 92 ファイル / +2348 / -5002。主要トピック:

- **Phase A**: C-1（care/anniversary 虚偽朗読の修正）、H-4（facts 局名 2 件修正）、H-5（ニュース event_year フィルタ）、M-13（eval checklist の照合窓）
- **H-1**: `b7e2f4a91c03_drop_ix_users_email.py` 追加で alembic ドリフト解消
- **H-2**: `auth/tokens.py` `_derive` に `@lru_cache(maxsize=8)`（PBKDF2 鍵キャッシュ）
- **M-1/M-2**: `api/audit.py` の `coverage_ratio` を terminal/started 比に変更しトートロジー解消；`billing/plans.py`/`billing_manager.py` から月次クォータを撤廃（README 無制限方針と統一）
- **M-8**: `server.py:1961-1974` `ctx.enriched` 空分岐を `INTERMISSION_TITLE` 契約へ
- **D-4/D-5**: `jobs.py` 購読者参照カウント（全購読者離脱時のみキャンセル）；`preview_resolver.py` 総時間予算
- **D-6/D-7/D-8**: 死蔵モジュール削除（`api/v1.py`、`core/pipeline.py`、`core/tts.py`、`services/*` 等）、ディスクロール整合、`text_cleaner` 行丸ごと削除修正
- テスト: 零アサート削除、テスト分離の改善（BOM 付き import 時 env 書き込みは 2 ファイル残存）

---

## 2. Critical

### C-1【修正維持】care/anniversary テンプレートの音源ゼロ虚偽朗読（前回レビュー Critical）

- 場所: `retro_radio/core/fallback.py:1060-1072`（care）、`:1128-1142`（anniversary）
- 判定: **修正維持**。`has_music = bool(songs)` で分岐し、音源ゼロ時は「いま鳴っている曲」「テーマ曲とともに」「大ヒット曲をお送り」を「皆様の記憶の旋律」等に置き換え。
- 証拠: `generate_care_script(1975, 6, 15)` を `pinned_songs([])` で実行し、cue 行は空になり静的装飾文も「記憶の旋律」系へ変化することを確認（前回の再現手順は通らない）。

---

## 3. High

### H-1【解消】`alembic check` 失敗（モデル/マイグレーションドリフト）

- 場所: `db/migrations/versions/b7e2f4a91c03_drop_ix_users_email.py`（新規リビジョン）
- 判定: **解消**。`ix_users_email` を削除する後続リビジョンが追加され、`alembic check` は「No new upgrade operations detected」。
- 証拠: 一時 SQLite DB で `alembic upgrade head` → `alembic check` とも exit 0。

### H-2【修正維持】トークン検証の PBKDF2 再導出（認証前 DoS 増幅）

- 場所: `retro_radio/auth/tokens.py:260-272`（`@lru_cache(maxsize=8)` を `_derive` に付与）
- 判定: **修正維持**。`lru_cache` により導出済み HMAC 鍵をメモリ保持。導入リスクは秘密鍵そのものの保持と同等。
- 証拠: `flake8` exit 0、`mypy_gate` 新規違反 0。

---

## 4. Medium

### M-1【解消】`coverage_ratio` のトートロジー（終端行 / started 行へ変更）

- 場所: `retro_radio/api/audit.py:29-86`（`audit_coverage` + `generation_coverage` 新設）、`:294-295`（呼び出し元）
- 判定: **解消**。`terminal_count / started_count` に変更し、`count_meta_contains` で `meta_json` の `phase` 断片を数える。
- 証拠: `tests/test_me_api.py:470-540` で `terminal < started` 時に `coverage_ratio < 1.0` を確認。

### M-2【解消】月次生成回数制限の撤廃（README 無制限方針と統一）

- 場所: `retro_radio/billing/plans.py:29-54`（FREE/PREMIUM/PRO すべて `UNLIMITED`）、`billing_manager.py` から `check_generation_limit` / `_reset_monthly_counter_if_due` 削除、`server.py:2252-2260`（`generate_radio` から 429 チェック削除）
- 判定: **解消**。`authenticator.check_can_generate` が常に `(True, -1)` を返し、plans.py も無制限に統一。`/api/generate` と `/api/jobs` に矛盾する死コードが消えた。
- 証拠: `README.md:332`「生成回数制限: 撤廃されています（FREE でも無制限）」と一致。

### M-3【未解消】CSRF 対策が SameSite=Lax のみ

- 場所: `retro_radio/server.py:2625-2633`（Cookie 設定。Origin/Referer/トークン検査は全 app で存在しない）
- 残存リスク: (a) Lax+POST の 2 分窓（新規ログイン直後、トップレベル form POST に Cookie が乗る）。(b) 同サイト兄弟サブドメインの攻撃ページ。(c) body 無し POST（`/api/auth/logout`、`POST /api/me/consent/withdraw`）は form 可能。
- 修正提言: middleware 1 本で unsafe method に Origin/Referer 許可リスト検査を追加。

### M-4【未解消】webhook 以外の全 POST にボディサイズ上限が無い

- 場所: `retro_radio/server.py:155`（`WEBHOOK_BODY_MAX_BYTES = 1_000_000` は webhook のみ）、`/api/auth/session:2506`、`/api/generate`、`/api/jobs:2780`、`/api/me/*` は Pydantic 検証前にボディ全体をメモリ読み。
- 二次的な穴: webhook の content-length 事前チェック（`:2717-2722`）は `Transfer-Encoding: chunked`（content-length 無し）で迂回。
- 影響: 匿名 10GB POST → メモリ枯渇。LoginThrottle はボディ解析後（`:2540`）に走るため抑止にならない。
- 修正提言: グローバルなボディ読取ミドルウェアで上限（例: 1MB）を強制。webhook は content-length 不在時に 411/413 で拒否。

### M-5【解消】全デプロイ設定に `RETRO_RADIO_TRUSTED_PROXY_HEADER` を追加

- 場所: `fly.toml:38-39`、`render.yaml:60-63`、`railway.env.example:41-42`
- 判定: **解消**。Secure Cookie / HSTS / per-client login throttle が本番で発動する。

### M-6【修正維持】text_cleaner の文頭切断（ラベル語で始まる普通の文）

- 場所: `retro_radio/utils/text_cleaner.py:175-187`
- 判定: **修正維持**（前回 10-02 版 / b3aa251）。行全体またはコロン終端のラベル行だけ除去。`くらしの風景を映していた曲です。` → 本文が残ることを実測確認。
- 補足: `_STAGE_DIRECTION_WITH_WORD`（演出語含む行丸ごと削除）は括弧/`※` を必須に分割（`:219-238`）。H-3 と同様の本文欠落を防ぐ。

### M-7【解消】パス間の曲埋めで `used_keys` が無視

- 場所: `retro_radio/server.py:1992-2003`（`elsewhere` から `used_keys` にあるものだけ `reserve` へ、`playable_pool` は未使用の可聴曲のみ）
- 判定: **解消**。D-5 コミット `6a30209` で API 形に揃え、`used_keys` にある曲だけを `reserve` へ渡す形に修正。薄い年でパス1がパス0を丸ごと再放送する問題は解消。

### M-8【解消】`ctx.enriched` 空分岐の INTERMISSION_TITLE 契約迂回

- 場所: `retro_radio/server.py:1961-1974`
- 判定: **解消**。`_to_song_dicts` 経由で `title=INTERMISSION_TITLE`、実名は `borrowed_song`（`is_fallback`）に内側だけ残し、API の曲名には実在カタログ曲を載せない。

### M-9【未解消】テナント TTS キャッシュの `max_files` 強制が TTL 内ファイルも削除

- 場所: `retro_radio/services/tenant_cache.py:294-319`（oldest-mtime-first・TTL 無視）vs `server.py` のフラット側は「TTL 内は削除しない」
- 影響: 1 テナントが 7 日内に `max_files` を超えると、まだ参照中の音声が削除され 404。3 つの掃除実装がポリシー分岐。
- 修正提言: テナント側を「期限切れのみ・数上限はその内」に統一。

### M-10【未解消】Stripe webhook 台帳書込が IntegrityError ガードの外

- 場所: `retro_radio/billing/webhook.py:169`（`ledger.record(...)` が `try/except IntegrityError` の外側）
- 影響: 同時二重配送で 2 つ目の台帳 INSERT が PK 衝突 → 未捕捉 500。データ破壊は無い（ハンドラ副作用はロールバック）。
- 修正提言: `ledger.record` を try 内に移動（衝突＝「重複、200」扱い）。

### M-11【未解消】`create_checkout_session` が `subscription_data.metadata` を付けない

- 場所: `retro_radio/billing/stripe_client.py:81-94`（session-level `metadata` のみ。`client_reference_id` も未設定）
- 影響: `customer.subscription.updated/deleted` に `metadata.user_id` が乗らず、帰属が逆引きフォールバック頼み。現状 checkout API に本番呼び出し元が無いため潜在害。
- 修正提言: `subscription_data={"metadata": {"user_id": ...}}` と `client_reference_id` を追加。

### M-12【未解消】Service Worker が `/health` を cache-first で保存

- 場所: `static/service-worker.js:45-52,106-142`（`/api/` と `/static/` 以外は cache-first。`/health` は該当しない）× `static/app.js:5209,5297`（ログイン後に `no-store` で再取得するが SW の `caches.match` は cache mode を無視）
- 影響: ログイン直後の `checkHealth` が古い匿名ペイロードを受け取り、「ログインしました」の直後にログインパネルが再表示される。
- 修正提言: `/health` を `/api/` と同様に network-first へ。

### M-13【解消】eval ゲートの checklist 曲照合が全カタログ照合のまま

- 場所: `eval/metrics/__init__.py:398-400`（`fact_score` は修正済み）vs `:401`（`run_checklist` に `allowed_song_titles` を渡すよう配線）
- 判定: **解消**。checklist の `song_match` も選曲窓で照合するように配線し、テスト 1 本追加。

### M-14【未解消】テストの import 時 env 書き込みが 2 ファイルへ拡散

- 場所: `tests/test_db_session.py:8`、`tests/test_db_user_repo.py:6`（`os.environ['RETRO_RADIO_DATABASE_URL'] = 'sqlite:///:memory:'` を import 時に書き、復元しない）
- 影響: 単独実行で conftest の per-PID 一時 DB ではなく `:memory:` エンジンを生成し、同一プロセスの後続 import に漏出。
- 修正提言: 両行を削除（`conftest.py:44-47` が分離済み）または monkeypatch fixture 化。

### M-15【未解消】約 2,690 LOC の死蔵コード（うち ~600 LOC は起動時に実行）

- 場所: `core/__init__.py:1,4`、`api/__init__.py:6`、`services/__init__.py:1-5` 等
- 完全死蔵: `core/pipeline.py`、`core/tts.py`、`core/batch_processor.py`、`services/favorite_service.py`、`services/export_service.py`、`services/cache_service.py`、`app/plan_control.py`、`utils/i18n.py`+`locales/`、`api/v1.py`、`auth/models.py` 等
- 削除時の注意: `core/__init__`、`api/__init__`、`services/__init__`、`tests/test_hygiene_regression.py` の W292 allowlist、`.dockerignore` keep-list の同時更新が必要。`core/__init__ → pipeline → music_search → preview_resolver` の循環 import も解消。

### M-16【未解消】mypy が CI 未接続（現在はゲート化済みだが素の mypy は 140 errors）

- 場所: `scripts/mypy_gate.py`（ベースライン方式で新規違反のみ検出）、`.github/mypy-baseline.json`（89 件）
- 判定: CI 接続済み（`ci.yml:67-70`）。ただし素の `python -m mypy retro_radio` は **140 errors in 26 files** で、既存違反を隠蔽している。
- 修正提言: ベースラインに無い新規違反だけを検出する方式は維持。既存 140 件の段階的解消を運用タスクとして残す。

### M-17【未解消】ゼロアサートテストが残存

- 場所: `tests/test_billing.py`（5/49 等）、`tests/test_tts_branches.py`（3/26 等）
- 判定: A-4 コミット（6f4c09a）で大幅削減されたが、25 件ベースラインからほぼ横ばいとのこと。実挙動 assert への置換が残る。

### M-18【解消】本番の `/me/export` JSON に `truncated` 標識が無い

- 場所: `retro_radio/api/me.py:192-222`（`_build_export_payload` で `"truncated": total_generations > len(generations)` を追加）
- 判定: **解消**。JSON 版にも `truncated` を追加。CSV 側は既に同等の考慮あり。

### M-19【未解消】network マーカー付き 3 テストが自動化上恒久に死んでいる

- 場所: `pytest.ini:14-17` で除外、どの workflow も `-m network` を実行しない
- 修正提言: 週次 scheduled job で `pytest -m network`（`continue-on-error: true`）を実行するか、テストを削除するか決める。

### M-20【未解消】`db_pool_recycle` 設定が黙って無視される

- 場所: `config.py:99`（`db_pool_recycle: int = Field(default=3600, ...)`）vs `db/session.py:48`（`pool_recycle=3600` 直書き）
- 影響: 運用者が設定しても反映されない。
- 修正提言: フィールドを読むか、フィールドと `.env.example` の行を削除。

---

## 5. 新規指摘（筆者の実測による追加）

### NEW-1【High】SSE 購読者参照カウントが「SSE のみ」で、ポーリングクライアントを数えない → 切断でジョブが誤キャンセル

- 場所: `retro_radio/server.py:3013,3074-3076`（`job.add_subscriber()` / `remove_subscriber()`）、`jobs.py:744-760`（`_subscribers` 参照カウント）、`static/app.js:1688-1693`（SSE 切断時に `startJobPolling()` へフォールバック）
- 内容: `POST /api/jobs/{id}/events` の SSE ストリームはクライアント切断時に `job.remove_subscriber()` を呼び、**全購読者が離脱したときだけ** `request_cancel()` を呼ぶ（D-4/H-5 の実装）。しかし購読者カウントを増やすのは SSE 購読者だけで、`GET /api/jobs/{id}` ポーリング経路は `add_subscriber()` を呼ばない。
- 実測（プローブ）: 購読者 1 人の SSE 切断 → `cancel=True`（予定通り）。購読者 2 人で 1 人切断 → `cancel=False`（予定通り）。**SSE 切断＋ポーリング中の別クライアントが居る場合 → `cancel=True`**（誤キャンセル。ポーリングクライアントはカウントされないため「全員離脱」と誤判定）。
- 影響: ページ更新や SSE→ポーリング フォールバック時に、実行中のジョブが意図せずキャンセルされ、生成が中断する。
- 修正提言: `add_subscriber()`/`remove_subscriber()` をポーリング経路（`GET /api/jobs/{id}` または SSE 接続の有無）でも呼ぶか、SSE 切断時に即キャンセルせず「この SSE 購読者だけ」を外す挙動に戻す。D-4 の意図（ポーリング中の別クライアントを殺さない）を満たすには、ポーリング側もカウントに含める必要がある。

### NEW-2【Medium】D-5 の総時間予算超過後は「最初の可聴曲の複製」で埋まり、`_step_playlist` の `used_keys` 重複排除と衝突してスロットが間奏化

- 場所: `retro_radio/server.py:1949-1970`（`_enrich_with_checkpoints` の予算切れ処理）× `server.py:1992-2003`（`_step_playlist` の `reserve` / `playable_pool`）
- 内容: `_total_budget_seconds()` = `(connect_timeout + read_timeout) * max_retries` = `(5+10)*3 = 45秒`。予算超過後、残りレコードは `enrich_songs` の「最初の可聴曲の複製」で埋められる（`preview_resolver.py:547-561`）。複製された曲の `trackName`/`artistName` は最初の可聴曲と同一キー。`_step_playlist` で `used_keys` に登録済みのため、後続パスで `elsewhere`/`playable_pool` からスキップされ、スロットが間奏に化ける。
- 影響: 薄い年や iTunes 応答が遅いとき、2 パス目以降のスロットが「音源ありなのに間奏扱い」になり、原稿と実際の再生が食い違う。
- 修正提言: 予算切れ複製のキーを `used_keys` から除外するか、`_step_playlist` の `playable_pool` に複製済み曲を優先採用する。

### NEW-3【Low】`create_job` の失敗経路で `EVENT_FAILED` / `job.fail()` が二重に呼ばれる

- 場所: `retro_radio/server.py:2907-2912`（`create_job` の `except`）× `retro_radio/jobs.py:1116-1123`（`start_worker` の `thread.start()` 失敗時）
- 内容: `start_worker` でスレッド起動に失敗すると `job.emit(EVENT_FAILED, ...)` + `job.fail(...)` + `_finish()` が呼ばれ、呼び出し元の `create_job` 側でも `except` ブロックで同じく `EVENT_FAILED` + `job.fail()` を行う。`_terminate` はガード済みで二重遷移は防がれるが、`emit` は冪等でなくイベント列に重複が入る。
- 影響: SSE クライアントに `EVENT_FAILED` が 2 回届く。UI が「生成失敗」を 2 回検知する可能性。
- 修正提言: `create_job` の `except` ブロックを削除し、`start_worker` 側の処理に一本化する。

### NEW-4【Low】README のテスト件数・ファイル数が実測と乖離

- 場所: `README.md:66,437`（「99 ファイル・3,012 件」） vs 実測
- 内容: 実測は **84 テストファイル / 1,692 test functions / 2,876 passed**（`pytest -q -p no:randomly`）。README は `network` マーカー 3 件除外のカウント法と異なるが、数値として軽微に陳腐。
- 修正提言: README のテスト数を実測値に更新するか、計数法を明記する。

### NEW-5【Low】`scripts/mypy_gate.py` の照合キーに行番号を含めない → 同ファイル同メッセージの 2 箇所目以降の新規違反が検出漏れ

- 場所: `scripts/mypy_gate.py:51-54`（`file|code|message` で照合、行番号を含めない）
- 内容: 同一ファイル内で同コード・同メッセージの違反が 2 箇所以上ある場合、1 件をベースラインに登録すると 2 箇所目以降が「既知」とみなされて検出されない。
- 修正提言: 照合キーに行番号を加えるか、同キーの出現回数で比較する。

### NEW-6【Low】開示上限 `EXPORT_ROW_LIMIT` が 5000 → 20 に縮小（保存件数と同一化）

- 場所: `retro_radio/api/me.py:96-99`（`GENERATION_HISTORY_KEEP = 20`、`EXPORT_ROW_LIMIT = GENERATION_HISTORY_KEEP`）
- 内容: 開示上限が 20 件に縮小され、`truncated` 標識で明示される。削除請求後の `generations` テーブルは最新 20 件だけ保持されるため、実害は限定的だが、削除漏れ検知時の開示範囲が 20 件で止まる。
- 修正提言: 保存上限と開示上限を分離するか、意図を文書化する。

---

## 6. Low（前回レビューの L-1〜L-52、主要項目の現状）

再検証の結果、変更のあったもののみ列挙。未記載の Low 項目は「現状維持・受容済み」。

| ID | 内容 | 現状 | 証拠 |
|---|---|---|---|
| L-1 | `/api/auth/session` 400 detail が認証無効を匿名に開示 | 未解消 | `server.py:2523-2524,2388-2390` |
| L-2 | `_request_is_https` が信頼プロキシヘッダを無視し `x-forwarded-proto` ハードコード | 修正維持 | `server.py:172-191`（`trusted_proxy_header` でゲート、宣言時のみ受理） |
| L-3 | `audio_tenant` の env-vs-Settings 矛盾 503 ガード | 修正維持 | `api/deps.py:254-292`（`resolve_mode` で一本化、矛盾時 503） |
| L-5 | `authenticator.py:22` のモジュールレベル settings スナップショット | 削除 | `auth/models.py`、`authenticator.py` のモジュールレベル settings は D-7 で削除 |
| L-9 | edge-tts 合成にタイムアウト無し | 未解消 | `core/tts_engines.py:103-114` |
| L-15 | `client_gone` set-only で読み手無し | 解消（置換） | `jobs.py:628` の `client_gone` は削除、`_subscribers` 参照カウントに置換 |
| L-17 | SSE 切断でジョブごとキャンセルは文書化された意図 | 変更 | D-4 で「全購読者離脱時のみキャンセル」に変更（NEW-1 で未達部分あり） |
| L-21 | alembic 構築 DB で `users.email` に unique 制約 + unique 索引が二重 | 解消 | `b7e2f4a91c03` で索引削除、`alembic check` 緑 |
| L-27 | 非推奨 `datetime.utcnow()` が 4 箇所 | 未解消 | `me.py:488` 等 |
| L-49 | UTF-8 BOM が 6 ファイルに混入 | 未解消（11 箇所） | `tests/test_auth_db_integration.py`、`tests/test_db_models.py`、`db/migrations/versions/125758440996_initial_migration.py`、`retro_radio/api/me.py:90`（UTF8_BOM 定数として意図的） |

---

## 7. 指摘サマリー（すべての領域）

| 重要度 | 件数 | 内訳 |
|---|---|---|
| Critical | 1 | C-1（修正維持） |
| High | 2 | H-1（解消）、H-2（修正維持） |
| Medium | 20 | M-1/M-2/M-5/M-7/M-8/M-13/M-18 解消（7件）、M-3/M-4/M-9/M-10/M-11/M-12/M-14/M-15/M-16/M-17/M-19/M-20 未解消（13件） |
| 新規（筆者実測） | 6 | NEW-1 High / NEW-2 Medium / NEW-3 NEW-4 NEW-5 NEW-6 Low |
| Low | 主要 10 項目 | L-1/L-2/L-3/L-5/L-9/L-15/L-17/L-21/L-27/L-49（現状維持・解消含む） |

**未解消の Medium 以上（優先対象）**: NEW-1（SSE 誤キャンセル）、M-3（CSRF）、M-4（ボディ上限）、M-9（テナント TTS キャッシュ）、M-10（webhook 台帳）、M-11（checkout metadata）、M-12（Service Worker）、M-14（import 時 env 書き込み）、M-15（死蔵コード）、M-20（db_pool_recycle）、NEW-2（予算/重複排除衝突）。

---

## 8. 着手順（推奨）

**Phase A — 利用者に実害（誤キャンセル・生成中断）**
1. **NEW-1**（SSE 購読者カウントがポーリングクライアントを数えず誤キャンセル）— `server.py`/`jobs.py`/`static/app.js`
2. **NEW-2**（D-5 予算超過後は最初の可聴曲複製で埋まり `used_keys` と衝突）— `server.py`/`preview_resolver.py`

**Phase B — セキュリティ・可用性**
3. **M-3**（CSRF Origin/Referer 検査追加）— `server.py` ミドルウェア
4. **M-4**（グローバルボディサイズ上限ミドルウェア）— `server.py`
5. **M-10**（webhook 台帳を IntegrityError ガード内へ）— `billing/webhook.py`

**Phase C — データ整合・プライバシー**
6. **M-11**（`subscription_data.metadata` + `client_reference_id`）— `billing/stripe_client.py`
7. **M-12**（Service Worker `/health` network-first）— `static/service-worker.js`
8. **M-14**（import 時 env 書き込み 2 ファイル削除）— `tests/test_db_session.py`/`test_db_user_repo.py`
9. **M-20**（`db_pool_recycle` を読むか削除）— `config.py`/`db/session.py`

**Phase D — 保守・運用**
10. **M-15**（死蔵コード段階的削除、約 2,690 LOC）
11. **M-16**（mypy 既存 140 違反の段階的解消）
12. **M-17**（ゼロアサートテストの実挙動 assert 置換）
13. **NEW-3/NEW-4/NEW-5/NEW-6**（Low の雑多項目）
14. **M-9**（テナント TTS キャッシュ ポリシー統一）
15. **L-9**（edge-tts タイムアウト）




