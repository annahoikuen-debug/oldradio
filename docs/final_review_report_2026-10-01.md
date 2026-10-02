# 最終レポート — retro_radio コードレビュー 3 周（2026-10-01）

## 1. 実施内容

「コードレビュー → 実装計画書 → 修正」のサイクルを **3 回**繰り返し、各周で
領域を 4 つに分割したサブエージェント（合計 **12 本**）を並行起動した。
各エージェントは「調査・報告のみ／ファイル編集・pytest 実行禁止」を強制し、
**主担当が全ての重大項目を自分で再現して確証したうえで**修正に入った。

| 周 | サブエージェント | 計画書 | 修正項目 | テスト数 |
|---|---|---|---|---|
| 1 | 4 本 | `docs/implementation_plan_round1.md` | P0 11 項目 | 2801 → 2885 |
| 2 | 4 本 | `docs/implementation_plan_round2.md` | 3 項目 | 2885 → 2898 |
| 3 | 4 本 | `docs/implementation_plan_round3.md` | 4 項目 | 2898 → 2918 |

---

## 2. 最終ゲート結果（すべて実測）

| ゲート | 開始時 | 最終 |
|---|---|---|
| `python -m pytest -q` | 2801 passed / 0 failed | **2918 passed**, 3 skipped, 3 deselected, 3 xfailed / **0 failed** |
| `flake8 tests` | EXIT 0 | EXIT 0 |
| `flake8 --config .github/flake8-app-baseline.ini retro_radio` | EXIT 0 | EXIT 0 |
| `flake8 eval` | EXIT 0 | EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0 / 24 of 24 / 平均 100.0 | EXIT 0 / 24 of 24 / 平均 100.0 |
| `alembic upgrade head` → `alembic check` | `No new upgrade operations detected.` | 同左 |
| `validate_songs.py` | fail 0 | fail 0 |
| `validate_facts.py` | warn のみ | warn のみ |
| ルートの `tmp_*` 残骸 | 0 件 | 0 件 |

**追加した回帰テスト: +117 件**（2801 → 2918）。
**修正した実バグ: 18 件**（Round 1: 11、Round 2: 3、Round 3: 4）。

---

## 3. 修正した実バグ（17 件）

### Round 1（11 件）

| ID | 深刻度 | 内容 | 実証 |
|---|---|---|---|
| P0-1 | **P0** | `secret_key` の32文字検査が全経路で無効。1文字鍵で admin セッション Cookie を**オフライン偽造**でき、`GET /api/me/export`（他人の原稿開示）と `DELETE /api/me`（他人のデータ削除）に通る | `issue_session_token(secret='a')` → `read_session_token` が `{'uid':'victim','role':'admin'}` を返す |
| P0-2 | P0 | `/health` が認証モード（`auth_mode`/`auth_ready`/`secret_key_configured`）を匿名に漏洩。攻撃者に認証経路の選択の手がかりを与える | docstring は「匿名には出さない」と宣言Preheatコードには分岐なし |
| P0-3 | **P0** | `songs=[]` が `None` に潰れ、司会が**鳴らない曲を紹介**していた（利用者は嘘を聞く） | `validate_song_pairs([])` → `None` |
| P0-4 | **P0** | `text_cleaner` が番号付き**本文行**を丸ごと削除（司会の会話が失われる） | `'1. 1975年…\n2. 石油危機\nbody.'` → `'body.'` |
| P0-5 | **P0** | 単独 `###` 行が**次の行の本文**を削除（`\s` が改行を跨ぐ） | `'line2 IMPORTANT.'` が消失 |
| P0-6 | P0 | フォールバック原稿の文法破綻「〜をお届けします。**を**「神田川」」（TTS がそのまま読んでいた） | `。を` が 3 箇所 |
| P0-7 | P0 | `* ` 箇条書きが全削除 | `'body line.'` のみ残る |
| P0-8 | P0 | lint ベースラインが `extend-ignore` ベースのため**新規違反が検出できない**（ベースラインコメントが事実と反転） | F401/W292/W391 の実 10 件が無言で抑制 |
| P0-9 | P0 | 性能テスト 3 件が `assert True` のプレースホルダー（何も計測しないのに passed に数えられていた） | Streamlit という廃止技術への言及付き |
| P0-10 | P0 | SQLAlchemy 例外文字列に**パスワードハッシュと PII**が混入 | `hide_parameters=True` + ログ redact 20 ケース |
| P0-11 | P0 | 認証設定の**逆向き**の矛盾で fail-open（匿名 principal が素通り） | 判定を 1 本の式に畳み、両方向 503 に |

### Round 2（3 件）

| ID | 深刻度 | 内容 | 実証 |
|---|---|---|---|
| R2-01 | **Critical** | 音源ゼロでも `care_recreation` / `anniversary` が曲名を挙げる | 修正前: `care` → `京急リスト`/`UBCの歌`/`虹の橋` |
| R2-02 | High | 音源ゼロ原稿が eval の length 下限を下回る | 737字（下限 800字）→ 910字 |
| R2-03 | **Critical** | 短すぎる `secret_key` で `session` モードが成立し、`TokenError` が `app_error_handler` を素通りして **500**、既存 Cookie も `invalid_session` で**ログイン画面が開かない** | `require_auth_config()` を `unavailable`（fail-closed）に |

### Round 3（4 件）

| ID | 深刻度 | 内容 | 実証 |
|---|---|---|---|
| R2-02 | **Critical** | 匿名利用者に**ログイン枠が出ない**（HUD が偽エラー「⛔ 管理者に依頼」） | `auth_ready === true` が匿名で falsy、かつ `auth_mode` 比較も永久に偽 |
| R3-01 | Medium | `auth_ready` が `require_auth_config()` と矛盾（31 文字の鍵で `unavailable` かつ `auth_ready is True`） | 判定を一本化 |
| R3-04 | Medium | `/health` がモジュールグローバル設定を使うため、`cache_clear()` 後に**古い設定**を報告（テストが順序依存で緑/赤を揺らす） | `Depends(settings_dependency)` に変更 |

### Round 3 追記（NEW-A）: 音源が 3 曲未満で原稿に空の鉤括弧が出る

**ファイル**: `retro_radio/core/fallback.py`

**実証**: Round 2 のガード `first_song = songs[0] if songs else ("", "")` は
`pinned_songs([])`（0 曲）を防いだが、**1〜2 曲**のときは `_cue_song` が `None` を返し
`or ("", "")` が空のタプルを渡した。結果:

- `この年の懐かしい名曲「」（）を、`（care）
- `記念の一曲「」（）をお届けします。`（anniversary）
- `とをつなぎますと`（anniversary）

`clean_script_for_tts` は曲名のある行のみを対象にするので除去されず、
**TTS がそのまま読み上げていた**。

**修正**: `care_recreation` / `anniversary` の曲振り文 4 箇所すべてを
`_cue_line`（`None` で空行を返す）経由に統一。直接埋め込みは 0 件になった。

**副次発見**: 統一の過程で「2 曲をつなげる文」を `_cue_line` 相当で実装すると
**曲名が 2 回出る**（`song_duplication` で eval ゲートが EXIT 1）。
`eval.metrics.checklist.song_duplication` の実測で検出したため、
装飾文は削除して各 `_cue_line` に責務を一本化した。

### 副次的な修正

- **CFG-01**: `ConfigurationError` の二重定義を解消。`utils/app_errors.py`（循環しない葉モジュール）へ `AppError` 階層を分離。循環 import も解消。
- **DB-11**: 接頭辞なしの `DATABASE_URL` は**まったく読まれない**ため、`tests/test_db_session.py` が本番DBを `drop_all` していた。修正 + ガードテスト。
- **ERR-03**: `JSONFormatter` に `redact_secrets()` を追加（第 2 防衛線）。

---

## 4. 残存リスク（**次の SPRINT で着手すべき順**）

### P0 — 本番で顕在化する

| ID | 内容 | 状態 |
|---|---|---|
| **R2-04** | リクエストボディ無制限。`stripe_webhook` が署名検証前に `await request.body()` | 未着手 |
| **R2-05 / JOB-02** | `/api/generate` に入場枠が無い。40 並行で anyio のスレッドプールが枯渇し `/api/auth/session` まで応答不能 | 未着手 |
| **R2-06** | SSE 同時接続数に上限が無い。1 接続 = 既定 executor の 1 スレッドを最大 900 秒占有 | 未着手 |

### P1 — 実害がある

| ID | 内容 | 状態 |
|---|---|---|
| **R2-08 / DB-01** | `record_generation` が未配線（`generations` は恒久に空）。かつ `DELETE /api/me` が `generations` を消さない | **仕様判断が必要**（`docs/privacy_and_tenancy.md:126` は「消さない」と明記、`SEC-07` は「消す」方針）。**②削除 → ①配線**の順でないと原稿全文が残存する |
| **R2-07** | 記念日モードの `normalize_target_name`（16 文字）と `validate_anniversary_input` が未配線。要配慮個人情報が 64 文字・月日つきで記録される | **仕様判断が必要**（UI 契約の 64 文字とプライバシー目標の 16 文字が競合） |
| **R2-03** | 429 がパスワード検証**前**に返る（恒久ロックアウト）。プロキシ背後で `(email, proxy-IP)` キーが全利用者で共有される | プロキシ信頼 + 猶予設計が仕様判断 |
| **DB-02** | Stripe 解約時に `metadata.user_id` が無く降格失敗 → **DB 上で永久プレミアム** | 機械的（Wave A1） |
| **JOB-08 → JOB-05** | `_job_queue_slots` の冪等性がプロセスグローバルカウンタ（スレッド単位でない）で、`running` ジョブ数が無制限に増える | 機械的（`_QueueTicket` 導入） |
| **CACHE-01** | TTS キャッシュに個数上限が無い。**二重ゲートで定期スイープが 2500 回に 1 回**しか動かない | 機械的（Wave A11） |
| **R2-05（コア）** | `order_candidates` が history 空のとき均一シャッフルするため、`songs_for_year` の「対象年の曲を先に返す」保証が**下流で壊されている**（1964 年で 18 枠中対象年平均 2.5 曲、min 0） | 未着手 |
| **R2-03/04/06（コア）** | 決定論の残り 3 箇所（`script_generator` の `random.sample` で同一入力 11 通り、`song_selector` の既定 rng、`fallback` の `random.choice`） | 機械的（seed 設計が要る） |
| **R2-07（コア）** | `preview_resolver` のサーキットブレーカーが**非連続失敗**で開く（正常な 404 が成功シグナルにならない）→ 全ホスト 60 秒無音 | 機械的（1 行） |
| **R2-11（コア）** | facts の `valid_to: null` により 1925 年開始の `NHKラジオ第一` が 1950〜2025 の全番組表に出る。`kind` が検証されるだけで**参照されない**ため、`tv_program` の `料理教室` がラジオ台本に出る | データ修正 + フィルタ |

### P2 — 保守性・運用

| ID | 内容 |
|---|---|
| **R3-06（frontend）** | `eval/`（12 ファイル）が **lint 5 経路すべてから外れている**。CI の eval ゲートの**実装そのもの**が無検査 |
| **R3-08** | `KNOWN_W292_ALLOWLIST` 14 件中 13 件が陳腐（Round 1 で修正済みだが allowlist から削除していない）。W292 に F401 版のような陳腐検出テストが無い |
| **R3-09** | 「201 上限」は 0 件コードを baseline に足しても count が変わらないため検出しない。集合比較のガードが必要 |
| **R3-16** | `network` マーカー付き 3 件が CI で恒久 deselect（nightly ジョブが無い） |
| **R3-15** | `requirements.txt` の runtime 依存 10 個に上限がない |
| **R3-14** | ポート 8501 が 4 箇所にハードコード。`fly.toml` の `[env] PORT` は完全な死設定 |
| **MIG-04 → MIG-01 → MIG-02** | マイグレーション engine に pragma が無い（rolling deploy で `database is locked`）／ `users.email` に UNIQUE 制約と索引の両方が存在 |
| **DB-05 / MIG-05** | `REQUIRED_TABLES` に privacy 6 テーブルが無い。`session.py:79-86` の auto-create が alemic の所有するスキーマを勝手に作る（以降 `alembic upgrade` が恒久失敗） |
| **DB-06** | プラン権限が 3 箇所に重複し、どれも HTTP 層から参照されない（デッドコード） |
| **R2-08（security）** | `/api/admin/audit` の `meta` にサイズ・深さ制限なし、`user_id` をリクエストボディから信頼（偽装可能） |
| **R2-13** | `/docs` / `/redoc` / `/openapi.json` が認証なしで公開（API 面の全取得） |
| **R3-22 / R3-17** | ドキュメントの不一致（`docs/privacy_and_tenancy.md:229-230` は匿名/認証済みの区別なし、`docs/code_review_2026-10-01.md` は見出し番号が重複） |
| **F-02 / R3-06** | `RETRO_RADIO_CSP` / `_HSTS_*` / `_FULL_SCRIPT_TTS` の 4 変数が `.env` を読めない（`os.environ` 直読み） |

### P3 — 仕様判断・データ検証

| ID | 内容 |
|---|---|
| **S-02** | songs 3030/3030・facts 17/17 が `confidence: unverified` かつ**どのコードも値を参照しないデッドコントロール** |
| **S-03** | facts の `valid_to: null` の意味論（何时まで有効かが未定義） |
| **MIG-03** | PostgreSQL の `sa.Enum` が `downgrade()` で drop されない（SQLite では顕在化しない） |

---

## 5. 3 周で学んだ教訓

### (1) 仕様を変えるときは「その仕様の依拠しているテスト」を全部洗い出す

Round 1 で `_cue_song` 方式（トークの直後の曲だけを名ざす）を導入したが、
`tests/test_song_alignment.py` は「1 曲でも必ず名前を挙げる」を固定していて
**8 件が落ちていた**。1 件だけテストを通して Airbnb とした時点では
「壊した」とは気づけない。

### (2) フロントとバックの payload 契約は、テストで固定しないと壊れる

Round 1 で `/health` から認証情報を隠したが、フロントのテストは
**サーバが出さない値**（`auth_ready: True`、`auth_mode: "anonymous"`）を
payload に書いていたため、修正後も**緑のまま**だった。
実際のバグ（匿名でログイン枠が出ない）は Round 3 まで **2 周見落とされた**。

### (3) `get_settings()` の `lru_cache` は隠れた結合を作る

モジュールグローバルに束縛した `Settings` は、`cache_clear()` の後に古くなる。
`/health` がモジュールグローバルを読んでいたため、テストが順序依存になっていた。

### (4) レポートには誤りがある（3 周合計 13 件の事実誤認）

- `MemoryError` は `Exception` のサブクラス（Round 1 の JόБ-04 は誤り）
- alembic の `compare_type` は既定 `True`（MIG-02 の「未設定」は誤り）
- `song_store._alloc_seqs` の呼び出し元は 0 件（DB-08 の「採番が消費されない」は潜在）
- `fly.toml:118-124` のコメント自身が「1 Machine のみ」と明記（JOB-01 の「2 台目 Machine」は到達不能）
- Round 1 の F-04 / F-13 / F-18 / F-27 は現ツリーで再現しない
- `hide_parameters=True` が既存ハンドリングを壊す → 該当コード 0 件

**推測を書かず `ファイル:行` と「そのコードをどう読んだか」を根拠として書かせること、
そして主担当が自分で再現することを必須にした。**

### (5) `git stash` を作業途中で使うと情報が失われる

`git stash push` → `pop` の往復で、未コミットの変更（`_cue_song` / `_cue_line` の定義と
`_script_songs(year, songs, 5)`）が混線した。編集作業中は使わない。

### (6) 文字化けは「書いてから直す」が危険

日本語を含む長い文字列を `edit` ツールや Python スクリプトで書き込むと、
繰り返し文字化けした。**最小差分 + ASCII アンカー + `\uXXXX` エスケープ**の
手法に切り替えてから解消した。

---

## 6. 生成物

| ファイル | 内容 |
|---|---|
| `docs/implementation_plan_round1.md` | P0 11 項目（実証方法・修正内容・回帰テスト名） |
| `docs/implementation_plan_round2.md` | R2-01/02/03（+  Benn teach した教訓） |
| `docs/implementation_plan_round3.md` | R2-02 / R3-01 / R3-04 |
| `docs/code_review_2026-10-01.md` | 既存（Round 0 の記録） |

### 主な変更ファイル

```
retro_radio/auth/tokens.py                     MIN_SECRET_LENGTH 1→32
retro_radio/config.py                          ConfigurationError 一本化 / auth_ready / require_auth_config
retro_radio/utils/app_errors.py                新設（循環しない葉モジュール）
retro_radio/utils/errors.py                    再エクスポート化
retro_radio/utils/logging_config.py            redact_secrets 追加
retro_radio/utils/text_cleaner.py              番号行・見出し・演出指示の判定を厳密化
retro_radio/core/fallback.py                   has_pinned_songs / 3状態判定 / 文法破綻 / 音源ゼロ原稿
retro_radio/core/script_generator.py           validate_song_pairs の 3 状態区別
retro_radio/db/session.py                      hide_parameters=True
retro_radio/server.py                          /health の認証分岐 + fail-open 修正 + 注入 Settings
static/app.js                                  describeHealth の匿名判定
tests/ (7 ファイル)                             回帰テスト +100 件
.github/flake8-app-baseline.ini                F401/W292/W391 を除外
.pre-commit-config.yaml                        retro_radio/ を lint 対象化
tests/test_performance_baseline.py             assert True → 実測 7 件
```