# コードレビュー — retro_radio 全体精査（2026-11-02）

対象: `e49a474`（HEAD、作業ツリー クリーン）
方法: 7 領域をサブエージェントに分割して精査し、**重大項目は主担当が再現して確証**した。
本レポートは**指摘のみ**を行う（修正は後続の実装計画書で対応）。
過去 5 ラウンドのレビュー（`docs/code_review_2026-10-01.md`、`final_review_report_2026-10-01.md`、`code_review_2026-10-02.md`、`implementation_plan_round1〜4.md`）の指摘の**再検証**を含む。

---

## 0. ゲート実測（すべて実測値）

| ゲート | 結果 |
|---|---|
| `python -m pytest -q` | **3066 passed / 7 skipped / 3 deselected / 2 xfailed / 0 failed**（228.58s） |
| `flake8 tests` | EXIT 0 |
| `flake8 --config .github/flake8-app-baseline.ini retro_radio` | EXIT 0 |
| `flake8 eval` | EXIT 0 |
| `flake8 scripts utils` | EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0 / **24 of 24** / fact score 平均 100.0 |
| `python -m mypy retro_radio` | **EXIT 1 — 150 errors in 31 files**（CI 未接続のため未検出。§N-14） |
| `pip-audit -r requirements.txt` | 脆弱性 0 件（click 8.1.8 ピン継続中、PYSEC-2026-2132 は現行 DB では検出されず。§N-13） |
| `pip-audit -r requirements-dev.txt` | UTF-8 モードでは 0 件。**cp932（既定ロケール）では UnicodeDecodeError でクラッシュ**（§N-12） |
| `python scripts/validate_songs.py --quiet` | fail 0 / warn 3,139（unverified 3,108・thin-year 29・missing-year 2） |
| `python scripts/validate_facts.py` | warn 17 のみ（すべて `confidence: unverified`） |
| `alembic upgrade head`（一時 DB） | EXIT 0（6 リビジョン適用） |
| **`alembic check`（一時 DB）** | **FAILED — `remove_index ix_users_email` が検出**（§N-1） |

**注目**: pytest・eval・flake8 はすべて緑だが、`alembic check` が**赤**。直近の修正コミット（3faba84「P2 major items fixed」）がモデル側 `users.email` から `index=True` を削除した一方、マイグレーション `88ad9aef7f29` は `ix_users_email` を作り続けており、**モデルとマイグレーションがドリフト**している。CI（`ci.yml:72` で `alembic check` を実行）は HEAD で赤の可能性が高い。

---

## 1. 再検証: 過去指摘の現状

過去 5 ラウンド（Round 0〜4 + 10-02 全体精査）の指摘について、現行ツリー（HEAD `e49a474`）での状態を検証した。
凡例: **修正維持** = 当時の修正が現行コードに存在 / **不完了** = 修正記録があるが実際は部分的 / **未解消** = 意図的先送りのまま / **退行** = 修正が失われている。

### 1-1. 10-02 版 P0（8 件）— すべて修正維持 ✅

| ID | 内容 | 判定 | 現在の証拠 |
|---|---|---|---|
| P0-1 | 周回 2〜3 で司会が A を告げて B が流れる | 修正維持 | `server.py:1812-1876` `_step_pass_scripts`（音源解決後にパスごとの原稿を生成。commit 3620733） |
| P0-2/P0-6 | `_QueueTicket` の永久リーク・無制限スレッド増加 | 修正維持 | `jobs.py:1057-1069`（`on_finish` + `threading.Event` で冪等）、`server.py:2837` |
| P0-3/P0-4 | 許可リスト空で無防備・位置を見ない差し替え | 修正維持 | `script_generator.py:310-369`（差し替え廃止、空リストで全曲名除去） |
| P0-5 | 音源ゼロ/少数で「三つほどご用意しました」 | 修正維持 | `fallback.py:825-842` `_music_promise_sentence`（0/1/2/3+ 分岐）— ただし care/anniversary テンプレートに同種の残骸あり（**C-1**） |
| P0-7 | SSE `last_event_id` で完了済ジョブを 900 秒固定 | 修正維持 | `server.py:2892-2911`（クランプ）、`:2958-2967`（ループ先頭で終端判定） |
| P0-8 | `single_user_key` 最小長なし・bearer 経路にレート制限なし | 修正維持 | `config.py:17,496-497,559,578-605`、`server.py:2576-2597`（LoginThrottle 適用） |

### 1-2. 10-02 版 P1（12 件）

| ID | 内容 | 判定 | 備考 |
|---|---|---|---|
| P1-1 | `DELETE /api/me` が TTS 音声キャッシュを消さない | 修正維持 | `me.py:661`（`_purge_tenant_tts_cache` 配線済み） |
| P1-2 | Stripe webhook の冪等性・順序制御 | 修正維持（**後半未完 → M-11**） | 台帳 `webhook.py:122-170`（same-transaction、out-of-order 破棄）。一方 `subscription_data.metadata` は未接続で attribution が逆引き依存のまま |
| P1-3 | 非 active 状態の降格 | 修正維持 | `webhook.py:38-44`（`past_due`/`unpaid` を処理） |
| P1-4 | メール一致での Customer 共有 | 修正維持 | `stripe_client.py:66-79`（`metadata.user_id` 一致時のみ再利用） |
| P1-5 | `get_by_id` に所有者述語なし | 修正維持 | `repository.py:300-303`（user スコープ）、`:344-349`（`add` の所有権検証）。dead な `toggle` にのみ旧欠陥が残存（L-19） |
| P1-6 | favorites/audit_logs が削除請求を生き延びる | 修正維持 | `privacy_repository.py:996-1010`（favorites 削除・audit 匿名化）。残置事項は L-22 |
| P1-7 | 全曲 `preview_url` 無し・iTunes 障害時に嘘 | 修正維持（**単一の穴 → M-8**） | `server.py:1699-1737` で輸送エラー時 `selected_pairs=[]`、5 層で `[]`≠`None` を保存。`ctx.enriched` 空分岐のみ実曲名が漏洩 |
| P1-8 | 「また明日遊ぼうね」ハードコード | 修正維持 | `fallback.py:867,957`（「また明日」時刻表現に変更済み。カタログ照合でも新規インスタンス無し） |
| P1-9 | facts の `valid_to`/TV 番組混入 | 修正維持 | `facts/__init__.py:331-343` `radio_programs_for_year`（台本は radio のみ）。番組表への TV 含有は文書化された設計判断。**データ側の局名誤りは未検出だった → H-4** |
| P1-10 | `audit_coverage` が生成イベント比を計算しない | **不完了（→ M-1）** | ヘルパは修正済み（`audit.py:119-145`）だが呼び出し元 `audit.py:204,211` が同一値を両辺に渡すトートロジーのまま |
| P1-11 | `X-Forwarded-Proto` を素朴に信頼 | 修正維持（コード）/ **運用面未完（→ M-5）** | `server.py:176-186` は trusted_proxy 設定でゲート。だが fly.toml/render.yaml/railway.json のどこにも `RETRO_RADIO_TRUSTED_PROXY_HEADER` を設定しておらず、PaaS で Secure Cookie/HSTS が発動しない |
| P1-12 | personal モードの anonymous 畳み込み | 軽減（L-23） | `deps.py:227-233` で user 無しのゲート処理をスキップ、409 応答で閉域化。同意履歴の共有は残るが実害は情報表示のみ |

### 1-3. Round 0〜4 の主要項目

| 項目 | 判定 | 証拠 |
|---|---|---|
| R1 P0-1〜P0-11（secret_key 32 文字、/health 開示、songs=[] 潰し、text_cleaner 番号行/###/* 削除、`。を` 文法、lint ベースライン、assert True、PII ログ、逆方向 fail-open） | 修正維持 | `tokens.py:85-104`、`server.py:3184-3200`、`text_cleaner.py` の該当厳格化、`db/session.py:31-58` `hide_parameters` など。ただし text_cleaner は別パターン（音楽/曲開始行・文頭切断）が未修正（**H-3/M-6**） |
| R2-01（音源ゼロで care/anniversary が曲名を挙げる）/ NEW-A（空鉤括弧） | 修正維持 | `_cue_line` 統一（`fallback.py:896-900`、6 箇所すべて経由）。ただし**静的虚偽文は残存（C-1）** |
| R2-02/R2-03/R3-01/R3-04（匿名ログイン表示、fail-closed、/health 設定注入） | 修正維持 | `app.js:5135-5189`、`config.py:551-561`、`server.py:3158+` `settings_dependency` |
| Round 4: R2-04（webhook body 上限）/ R2-05・JOB-02（/api/generate 即 503）/ R2-06（SSE 上限）/ DB-02（逆引き）/ JOB-08（チケット冪等）/ R2-05core（対象年優先）/ R2-07core（breaker 400/404）/ 決定論（rng_seed） | 修正維持 | `server.py:149-150,2661-2671`、`server.py:2302-2313`、`server.py:2922-3029`、`webhook.py:295-303`、`jobs.py:1057-1069`、`song_selector.py:210-239`、`preview_resolver.py:341-351`、`script_generator.py:427-431`。CACHE-01 はサーバ側維持だがテナント側が別ポリシーに分岐（**M-9**） |
| Round 0 deferred: R0-#5（429 が検証前に (email, proxy-IP) をロック） | 未解消（設計判断のまま） | `server.py:2540-2553`。`TRUSTED_PROXY_HEADER` 未設定のデプロイでは全利用者が同一 proxy IP を共有するため、攻撃者 1 人の 5 回失敗で施設全体が 429 になる（**M-5** と複合） |
| Round 0 deferred: R0-#9b（admin audit の meta 無制限） | 修正維持（API 層） | `audit.py:62-100`（4,096 バイト/深さ 5）。リポジトリ層は未上限だが内部呼び出しは固定小辞書（L-24） |
| Round 0 deferred: R0-#10（/api/auth/session 400 が認証無効を匿名に開示） | 未解消（Low） | `server.py:2523-2524, 2388-2390` |
| 10-02 §5（テスト空洞 5-1〜5-8）、§6（デプロイ不整合）、10-1（bat の migration 欠落） | 個別（§6・§7 に詳述） | 5-1/5-3/5-4 は本体修正+残余、5-2(b) は**半修正**（M-13）、5-5/5-6/5-7（**悪化** M-14）/5-8 各項は大部分未解消 |

**再検証の結論**: 10-02 版 P0 全 8 件をはじめ、過去修正の大部分（検証 40 項目中 33 項目）は現行ツリーで保持されている。しかし **P1-10（監査カバレッジ）とクォータ（P3-2）の 2 件は「修正済み」記録に反して不完了**であり、さらに **3faba84（P2 修正コミット）が新たなモデル/マイグレーションドリフト（H-1）を混入**した。「修正済み」として記録された項目の再検証には継続的な価値がある。

---

## 2. Critical

### C-1【新規・再現済】care/anniversary テンプレートが**音源ゼロの放送で「いま鳴っている曲」を語る**

- **場所**: `retro_radio/core/fallback.py:1061,1084`（care）、`:1113,1129-1130`（anniversary）
- **深刻度の根拠**: 「利用者が嘘を聞く」状態。R2-01（10-01 版・Critical）と 10-02 版 P0-5 が修正したのと**同種の虚偽が、テンプレートの静的な装飾文として別経路に残存**している。`_cue_line` は曲なしで正しく空行を返すようになったが、曲に言及しない静的な文は条件分岐の外にある。
- **再現**（実行確認済み）:
  ```
  with fallback.pinned_songs([]):
      script = fallback.generate_care_script(1975, 6, 15)
  → 「いま鳴っている曲こそ、この番組のテーマでございます。」
  → 「この年のヒット曲が心を打つのは、…」
  （cue 行は正しく空 = 曲名の言及は無い。しかし「いま鳴っている」は虚偽）
  with fallback.pinned_songs([]):
      ann = fallback.generate_anniversary_script(1975, 6, 15, ...)
  → 「この番組のテーマ曲とともに、1975年へお迎えいたします。」
  → 「…大ヒット曲を、心からの祝福の気持ちを込めてお送りいたします。」
  ```
- **影響**: iTunes 全滅・サーキットブレーカ作動時（= すべてのスロットが間奏のとき）に、回想法モードの利用者（認知症の可能性のある高齢者）へ「曲が鳴っている」という嘘を朗読する。このプロジェクト自身が `script_generator.py:125-127` で「source monitoring error を乱す**安全要件**」と定義している問題クラス。
- **修正提言**: `_music_promise_sentence` と同じく音源数 0 の分岐を care/anniversary のオープニング・エンディング装飾文に導入する（`_generate_no_music_script` の既存文面を流用可能）。

---

## 3. High

### H-1【再現済・新規】`alembic check` が失敗 — モデル/マイグレーションドリフト（直近の修正コミットで混入）

- **場所**: `retro_radio/db/models.py:70` vs `db/migrations/versions/88ad9aef7f29_add_fk_cascade.py:40`
- **経緯**: コミット `3faba84`（「P2 major items fixed」）が 10-02 版 P2「unique 制約と unique 索引の二重定義」を修正する際、**モデル側のみ** `index=True` を削除した。マイグレーション `88ad9aef7f29` は `ix_users_email`（unique index）を作り続けている。
- **再現**（実行確認済み・一時 DB）:
  ```
  alembic upgrade head   → EXIT 0
  alembic check          → FAILED: New upgrade operations detected:
                           [('remove_index', Index('ix_users_email', Column('email', ...), unique=True))]
  ```
- **影響**: `ci.yml:72` が `alembic check` を実行しているため **HEAD で CI が赤**の可能性が高い（ローカル alembic 1.20.0 で確認）。また alembic で構築した DB と `create_all`（テスト）でスキーマが分岐し続ける。
- **修正提言**: `ix_users_email` を削除する後続リビジョンを追加（または 88ad9aef7f29 の INDEXES から外して注記）。

### H-2【実測済】トークン検証が**全リクエストごとに PBKDF2-100,000 回**（鍵キャッシュ無し）

- **場所**: `retro_radio/auth/tokens.py:116-120`（`_derive`）、`:123-125`（`_sign`）、`:138`（`_verify`）。`lru_cache` もモジュールレベル鍵キャッシュも無く、sign/verify のたびに再導出。
- **実測**: 1 回の導出 = **66.7 ms**（開発機・Python 3.12）。fly.toml は 1 共有 vCPU/512MB のため実機では更に遅い。
- **呼び出し経路**: `deps.py:157` `require_tenant` → 全保護ルート（`/api/generate`、`/api/jobs`、**mp3 セグメント 1 件ごとの `/api/audio/*`**、SSE 接続、認証付き `/health`）で `_verify` が走る。さらに**未認証の 401 経路**（ジャンク Cookie 付きリクエスト）も同じ ~100ms を燃やし、レート制限が無い。
- **影響**: 1 回の再生セッション（mp3 ×6+ ポーリング ×n + SSE）で**秒単位の純 CPU 消費**。ジャンク Cookie を ~15 req/s 送るだけで 1 vCPU を飽和させられる（認証前 DoS 増幅）。
- **修正提言**: `_derive` に `@lru_cache(maxsize=8)` を付す（導出済み HMAC 鍵のメモリ保持は秘密鍵そのものの保持と同等の露出で、追加リスクなし）。1 行の修正。

### H-3【実証済】text_cleaner が「音楽/曲」等で始まる**本文行を丸ごと削除**（R1 P0-4 同種の未修正パターン）

- **場所**: `retro_radio/utils/text_cleaner.py:152-157`（`_STAGE_DIRECTION_WITH_WORD`）、`:194` で適用
- **実証**（`clean_script_for_tts` への直接プローブ）:
  ```
  '音楽は私たちの思い出の箱でございます。' → ''（削除）
  '曲について、少しお話しさせてください。' → ''（削除）
  ```
  括弧 `（` も `※` も省略可能なため、これらの語で始まる**普通の日本語の本文行**が TTS 出力から消える。
- **影響**: ナレーション本文の欠落。モジュール自身の契約（`text_cleaner.py:181-184` 「**本文を消さないことが最優先**」）と矛盾。10-01 版 P0-4（番号付き本文行削除 = P0）と同クラスだが、トリガーが異なるパターンとして残存していた。
- **修正提言**: 括弧/`※` を必須にする（完全な括弧行のみ削除対象に）。

### H-4【データ確認済】facts レジストリの**放送局名が 3 件中 2 件誤り**、TTS が誤りを朗読する

- **場所**: `retro_radio/core/facts/programs.json`
- **確認済みデータ**:

  | id | 登録されている network | 実際 |
  |---|---|---|
  | `all_night_nippon_1971` | 文化放送 | **ニッポン放送**（description も「文化放送で始まった」と誤記） |
  | `packin_music_1970` | 日本放送（=ニッポン放送） | **TBSラジオ** |
  | `nhk_radio_first_1925` | NHK | 正しい |

- **伝播経路**: `fallback.py:1015-1032` `_program_sentence` → care（`:1068`）・anniversary（`:1118`）の台本文へ埋め込み、**TTS がそのまま読み上げる**。
- **影響**: このプロジェクトは事実の正確性を安全要件として扱う（`facts/__init__.py:3-6`）。`scripts/validate_facts.py` は出典の有無しか検査できず、内容の正誤は検出不能（全 17 レコードが `confidence: unverified`・`source_url: null`）。
- **修正提言**: 2 レコードの network/description を修正し、3 レジストリ全体を一次文献で 1 回の検証パスに通す（`confidence: verified` へ昇格）。

### H-5【コード確認済】ニュース候補が 10 年粒度のまま — **1958/1959 年の出来事を 1950 年の「その日の主要ニュース」として語りうる**

- **場所**: `retro_radio/core/script_generator.py:28-104`（`NEWS_TOPICS_BY_DECADE`）、`:107-119`（`select_news_topics` — **event year によるフィルタが存在しない**）
- **確認済みデータ**: 1950 バケットに「皇太子殿下と正田美智子さまのご成婚（1959年）」「東京タワー完工（昭和33年=1958年）」、2020 バケットに「東京2020オリンピック開催（**2021年**）」。プロンプト（`:700-703`）はこれらを「その日の主要ニュース」として語るよう指示する。
- **影響**: `year=1950` の番組で 9 年後の出来事を当日のニュースとして朗読しうる。`future_year_mentions` は番組表のスケジュールのみを検査（`fallback.py:1213-1222`）し、ニュース候補は検査対象外。eval の `fact_score` もレジストリ由来の主張しか検査しないためゲートを通過する。
- **修正提言**: `NewsTopic` に出来事年を追加し、`select_news_topics` で `event_year <= year` にフィルタ（決定的・seed 互換）。

---

## 4. Medium

### M-1【再発・不完了修正】`coverage_ratio` が恒等的に 1.0 — P1-10 の「修正済み」記録が呼び出し元に届いていない

- **場所**: `retro_radio/api/audit.py:204`・`:211`
- **エビデンス**: `generation_events = by_action.get(ACTION_GENERATION, 0)` の**同一式**が `audit_coverage(分子, 分母)` の両辺に渡る → `min(1.0, x/x)` ≡ **常に 1.0**。ヘルパ側（`audit.py:119-145`）は正しく修正済みで、docstring も「P1-10」への言及を持つが、独立した分母（実際の生成イベント数、例: `generations` テーブルや `started` 行数）はどこからも取得されない。
- **影響**: `GET /api/admin/audit/stats` の完全率が定数になる。`_record_generation_audit` は DB 障害時に黙って False を返す（`server.py:516-518`）ため、監査漏れが実際に起きてもこの指標は絶対に検知しない。削除 SLA 監視の隣接指標として機能していない。`tests/test_me_api.py:474-493` がトートロジーを期待値として固定している。
- **修正提言**: 独立ソース（`GenerationRepository` のカウント等）から分母を取得。加えて呼び出し元を検査するテストに書き換え。

### M-2【不完了修正】生成クォータが 3 重に死んでいる — 加算経路なし・`/api/jobs` にチェックなし・リセット未永続化。さらに plans.py と README が矛盾

- **場所**: `server.py:2252-2276`（チェックは `/api/generate` のみ）、`server.py:2780-2855`（`create_job` にチェック**無し**）、`auth/authenticator.py:429-436`（`increment_generation_count` — 唯一の呼び出し元は死蔵の `app/plan_control.py:24`）、`billing/billing_manager.py:70-73`（`BillingManager()` が db 無しで生成されるためリセットがメモリのみ）
- **データ矛盾**: `billing/plans.py:29-54` は FREE=5/月・PREMIUM=30/月を定義する一方、`README.md` は「生成回数制限: 撤廃されています（FREE でも無制限）」と明記。**どちらが意図かがコード上で確定できない**。
- **影響**: 現状は README 通り無制限で動くが、(a) plans.py の有限上限は到達不能な死データ、(b) 429 経路（`server.py:2272-2276`）は発火不能、(c) 将来クォータを生かすと `/api/jobs` バイパスと未永続化リセットが即バグになる。
- **修正提言**: 仕様を 1 つに決め（README の無制限なら plans.py の上限値を削除）、残す場合は生成成功時に加算し `create_job` にもチェックを配線。

### M-3【設計判断の確認】CSRF 対策が SameSite=Lax のみ — Origin/Referer 検査もトークンも無し

- **場所**: `server.py:2625-2633`（Cookie 設定。CSRF 対策はここが唯一の層）。全 app を grep しても Origin/Referer/トークン検査は存在しない。
- **残存リスクの列挙**（A1 調査）: (a) **Lax+POST 2 分窓** — 新規ログイン直後 ~2 分はトップレベル form POST に Cookie が乗る → 任意の閲覧ページから `/api/auth/logout`（body 無し・未認証可）や `POST /api/me/consent/withdraw`（body 無し・`me.py:402-411`）が成立しうる。(b) **同サイト兄弟サブドメイン** — `app.example.com` 運用時に `*.example.com` の攻撃ページが同サイト扱いで Cookie を送れる。JSON body を要求するエンドポイントは 422 で概ね防がれるが、上記 2 つの body 無し POST は完全に form 可能。(c) 状態変更 GET は存在しない（確認済み）。
- **修正提言**: middleware 1 本で unsafe method に Origin/Referer 許可リスト検査を追加（Cookie 認証なら最小の防御層）。

### M-4【新規】webhook 以外の全 POST にボディサイズ上限が無い — 特に未認証の `/api/auth/session`

- **場所**: 上限は `WEBHOOK_BODY_MAX_BYTES`（`server.py:149-150`）のみ。`/api/auth/session:2506`、`/api/generate:2244`、`/api/jobs:2780`、`/api/me/*`、`/api/admin/audit:218` は Pydantic 検証の前にボディ全体をメモリに読む。
- **二次的な穴**: webhook の content-length 事前チェック（`:2661-2667`）は **`Transfer-Encoding: chunked`（content-length 無し）で迂回**され、`:2668-2671` の二次防御が発動する時点で既に全文がバッファ済み。
- **影響**: 匿名 10GB POST → メモリ枯渇。LoginThrottle はボディ解析**後**（`:2540`）に走るため抑止にならない。
- **修正提言**: グローバルなボディ読取ミドルウェアで上限（例: 1MB）を強制。webhook は content-length 不在時に 411/413 で拒否。

### M-5【運用面で P1-11 が未完】どのデプロイ設定も `RETRO_RADIO_TRUSTED_PROXY_HEADER` を設定していない → **Secure Cookie も HSTS も本番で発動しない**・ログインスロットルが proxy IP 共有で崩壊

- **場所**: `fly.toml:26-82`・`render.yaml:24-87`・`railway.json` — `TRUSTED_PROXY` の記述 0 件（`.env.example:117,123` のみ文書化）。
- **連鎖**: Fly/Render/Railway はエッジで TLS 終端し内部は平文 → `_request_is_https`（`server.py:176-186`）は header 未信頼で False → `secure=False`（`server.py:2631`）・HSTS 無し（`:204`）。同一設定が `_client_ip`（`:2436-2438`）も制御するため、未設定だと全利用者が proxy IP を共有 → **攻撃者 1 人の 5 回失敗で施設全体が 429**（R0-#5 の先送りと複合）。
- **修正提言**: fly.toml `[env]` に `RETRO_RADIO_TRUSTED_PROXY_HEADER = "x-forwarded-for"`（+ hops）を追加、render/railway も同様。

### M-6 text_cleaner が文頭を切断 — 「くらしの風景/主要ニュース/その日の主要」で始まる普通の文が壊れる

- **場所**: `text_cleaner.py:44-48`（`_LABEL_ONLY_PATTERNS` — ラベル接頭辞+**省略可能なコロン**・行末アンカー無し）
- **実証**（プローブ済み）: `'くらしの風景を映していた曲です。…'` → `'を映していた曲です。…'`。`'主要ニュースに選ばれたのは、…'` → `'に選ばれたのは、…'`。これらの語は生成プロンプトに literally 含まれる（`script_generator.py:611,700-706`）ため Gemini が文頭で反復する可能性が高い。
- **修正提言**: ラベルが行全体かコロン終端のときのみ除去（`:57-58` の条件/口調パターンと同じ形に）。

### M-7【10-02 P2 未解消】パス間の曲埋めで `used_keys` が無視され、コメントが実装と矛盾

- **場所**: `server.py:2038-2055`（fill ループ — `used_keys` は write-only）vs `:2024-2028` のコメント「**他のパスで既に使った曲はこのパスでは使わない**（P2-17）」
- **影響**: 薄い年（1952-1955、1996、2005…）で窓内の演奏可能曲が尽きると、`elsewhere` が pass 0 と同一の曲集合を同一順で再充填 → パス 1 がパス 0 を丸ごと再放送する。誠実性は保たれる（原稿も同じ曲を指す）が、文書化された重複回避が機能していない。
- **修正提言**: elsewhere 源の前に `[item for item in elsewhere if _key(item) not in used_keys]` を挿入。

### M-8【10-02 P2 未解消・P1-7 の単一の穴】`ctx.enriched` 空分岐が実曲名を `GenerateResponse.songs` に入れ、INTERMISSION_TITLE 契約を迂回

- **場所**: `server.py:1961-1969`
- **エビデンス**: 他の全出口（`_song_item:1290-1299`、`_to_song_dicts:1349-1370`）は preview 無しスロットを `INTERMISSION_TITLE` + `borrowed_song` に書き換えるが、この分岐だけ `get_fallback_song(year)` の**実在するカタログ曲名**を `preview_url: None` のまま返す。
- **影響**: フロント以外の API 消費者に「鳴らない曲名」を見せる。さらに `_persist_generation`（`:2134-2148`）が実曲名を `generations` テーブルのリード曲として記録し、開示 API で返る。
- **修正提言**: `_to_song_dicts` 経由で構築（`title=INTERMISSION_TITLE`、実名は `borrowed_song`）。

### M-9【新規】テナント TTS キャッシュの `max_files` 強制が **TTL 内のファイルも削除** — 施設モードで再生済み番組の音声が 404 しうる

- **場所**: `services/tenant_cache.py:287-319`（oldest-mtime-first・TTL 無視）vs `server.py:525-575`（フラット側は「**TTL 内のファイルは削除しない**」が意図的な契約、`:531-536`）
- **影響**: 1 テナントが 7 日内に 2,000 区分（≈300 番組）を超えると、`playlist[].audio_url` がまだ参照している音声が削除され、過去番組の再生で 404。3 つの掃除実装（server/tenant/死蔵 core/tts）が**ポリシーまで分岐**している。
- **修正提言**: テナント側をフラット側の「期限切れのみ・数上限はその内」ポリシーに統一（共通ヘルパ化が望ましい）。

### M-10【新規】Stripe webhook 台帳書込が IntegrityError ガードの外 — 同時二重配送で 1 回の無害な 500

- **場所**: `retro_radio/billing/webhook.py:169`（`ledger.record(...)` が `try/except IntegrityError`（`:150-165`）の**外側**）
- **影響**: 同一 event の同時配送で、`already_processed` を通った 2 つ目の台帳 INSERT が PK 衝突 → 未捕捉の 500 → Stripe が再送（自己修復するが、無害な 500 + 再送ラウンドトリップ）。データ破壊は無い（ハンドラ副作用はロールバックされる）。
- **修正提言**: `ledger.record` を try 内に移動（衝突 = 「重複、200」扱い）。

### M-11【P1-2 後半未完】`create_checkout_session` が `subscription_data.metadata` を付けない — subscription イベントの帰属が逆引きフォールバック頼み

- **場所**: `retro_radio/billing/stripe_client.py:81-94`（session-level `metadata` のみ。`client_reference_id` も未設定）
- **影響**: Checkout は session metadata を subscription にコピーしないため、`customer.subscription.updated/deleted` には `metadata.user_id` が乗らず、`_resolve_user_id`（`webhook.py:282-304`）は `find_by_stripe_customer` 逆引きに頼る。`checkout.session.completed` の顧客保存（`:235-239`）が一度でも失敗すると、そのユーザーの subscription イベントは恒久に帰属不能（記録のみで破棄、`:246-252`）。現状 checkout API に本番呼び出し元が無いため潜在害。
- **修正提言**: checkout を配線する前に `subscription_data={"metadata": {"user_id": ...}}` と `client_reference_id` を追加。

### M-12【新規・フロント】Service Worker が `/health` を cache-first で保存 — **ログイン成功後も匿名ペイロードを返し続ける**

- **場所**: `static/service-worker.js:45-52,106-142`（`/api/`（`:127`）と `/static/` 以外は cache-first。`/health` はそのどちらにも該当しない）× `static/app.js:5209,5297`（ログイン後に `cache: 'no-store'` で再取得するが、SW の `caches.match` はリクエストの cache mode を無視）
- **影響**: ログイン直後の `checkHealth` が古い匿名ペイロードを受け取り、`describeHealth`（`:5169-5175`）が `authNeeded: true` を再導出 → **「ログインしました」の直後にログインパネルが再表示される**。SW バージョンが上がるまで持続。
- **修正提言**: fetch ハンドラで `/health` を `/api/` と同様に network-first（または非 `/static` の同一オリジン GET を network-first）へ。

### M-13【5-2(b) 半修正】eval ゲートの checklist 曲照合だけが全カタログ照合のまま

- **場所**: `eval/metrics/__init__.py:398-400`（`fact_score` には `allowed_song_titles=selection_window_titles(year)` を渡す=修正済み）vs `:401`（`run_checklist(text, year)` — **渡していない**）→ `eval/metrics/songs.py:174-178` が全体カタログに解決
- **影響**: 「カタログに実在するが、この番組の選曲窓に無い曲」を原稿が名指しても checklist `song_match` は合格。`fact_score` 側は WARN 止め（`fact_score.py:434-436`）。「過去の曲だが鳴らない」が両ゲートを素通りする。10-02 版 5-2(b) の修正が半分だけ適用された状態。
- **修正提言**: `run_checklist(text, year, allowed_song_titles=selection_window_titles(year))` + 配線テスト 1 本。

### M-14【5-7 悪化】テストの import 時 env 書き込みが 2 ファイルへ拡散

- **場所**: `tests/test_db_session.py:8`・`tests/test_db_user_repo.py:6` — どちらも `os.environ['RETRO_RADIO_DATABASE_URL'] = 'sqlite:///:memory:'` を import 時に書き、**復元しない**（両ファイルとも UTF-8 BOM 付き）。
- **影響**: 単独実行で conftest の per-PID 一時 DB ではなく `:memory:` エンジンを生成し、同一プロセスの後続 import に漏出する収集順序の地雷（10-02 版 5-7 のパターンが 1 件 → 2 件に）。
- **修正提言**: 両行を削除（`conftest.py:44-47` が分離済み）または monkeypatch fixture 化。

### M-15【死蔵コード全体】約 **2,690 LOC の死蔵コード**。うち ~600 LOC は `__init__.py` の eager import で**毎回起動時に実行される**（import 時に `%TEMP%/retro_radio_tts_cache` を mkdir する副作用付き）

- **場所**: `core/__init__.py:1,4`（`pipeline` と `tts` を起動時に import → `core/tts.py:104-105` が import 時 mkdir）、`api/__init__.py:6`（未マウントの `v1.py` をロード）
- **完全死蔵（削除推奨）**: `core/pipeline.py`(186)・`core/tts.py`(317)・`core/batch_processor.py`(35)・`services/favorite_service.py`(214)・`services/export_service.py`(215)・`services/cache_service.py`(86)・`app/plan_control.py`(63)・`utils/i18n.py`+`locales/`(276)・`api/v1.py`(50)・`auth/models.py`(3)・`music_search.search_itunes_by_year`(32)・`music_profile` スコアリング半分(~250)・`utils/session.py` の SessionManager 以外(~160) — **計 ~1,940 LOC**
- **死蔵ツール類**: `design_tokens/`+`styles/generated.css`+`utils/design_tokens.py`(~676 LOC、フロントは `static/app.css` のみ読む)・`probe_edgecache.py`(70 — **`.dockerignore` 非対象のため Docker イメージに同梱される**)
- **削除時の注意**: `core/__init__.py`・`api/__init__.py`・`services/__init__.py:1-5`・`tests/test_hygiene_regression.py` の W292 allowlist（`:74-89` が死蔵ファイルを列挙）・dockerignore keep-list（`:785` locales/ja.json）の同時更新が必要。副次効果として `core/__init__ → pipeline → music_search → preview_resolver` の循環 import が解消される。
- **修正提言**: 段階的削除計画（`docs/implementation_plan_round5.md` 相当）として実施。

### M-16 mypy が CI 未接続 — 現在 **150 errors in 31 files**

- **実測**: `python -m mypy retro_radio` → EXIT 1（150 エラー）。注目すべき実害候補: `server.py:2678`（BaseException 派生外の raise 型）、`server.py:2833`（`start_worker` への `Callable[[Job], GenerateResponse]` vs `Callable[[Job], None]` 型不一致 — 実害は無いが契約が嘘）、`server.py:1019,1053`（None 代入）。
- **現状**: pre-commit の `mirrors-mypy v1.5.0`（2023 年ピン）のみで CI には無い（10-02 版 5-8(c) 未解消）。
- **修正提言**: まず計測を CI に入れてベースライン化（現在の 150 を記録）、実害候補 3 件は手動確認のうえ修正。

### M-17【10-02 §5 再計測】「緑だが何も検査していない」テストが 25 件残存（ベースライン 23 → ほぼ横ばい）

- **実測**（AST 全数調査・1,856 test functions）: zero-assert **25 件**。最悪ファイル `tests/test_billing.py`（5/49 — `test_unhandled_event_type_is_ignored` 等 webhook 経路の名ばかりテスト群）、`tests/test_tts_branches.py`（3/26）。
- **指名 targets 未修正**: `test_server_regression.py:897-910`（CORS トートロジー — try/except で両パスが pass）、`test_round4_regression.py:127-139`（チケットリーク検出テストが assert 無し）、`test_retro_theme.py:123-130`（WCAG 検証が literal `pass`）。
- **修正提言**: billing 5 件は実挙動 assert に置換（webhook は M-10 とも関連）。他は 1 行ずつの assert 追加で済む。

### M-18【新規】本番の `/me/export` JSON に `truncated` 標識が無い（死蔵の二重実装には存在）

- **場所**: `api/me.py:484-500`（無し）vs `services/export_service.py:149-185`（有り — 死蔵モジュール側のみ）
- **影響**: 開示請求の JSON 消費者が、5,000 行上限で**切り捨てられたかどうかを判別できない**（CSV 側には同等の考慮あり）。プライバシー開示の正確性として不完全。
- **修正提言**: me.py の JSON エクスポートに `truncated` を追加（または行数を明記）。

### M-19【10-02 5-8(d) 未解消】network マーカー付き 3 テストが自動化上**恒久に死んでいる**

- **場所**: `pytest.ini:14-17` で除外、どの workflow も `-m network` を実行しない。実 iTunes / 実 gTTS / e2e-medley 統合は**一度も自動検証されていない**。
- **修正提言**: 週次 scheduled job で `pytest -m network`（`continue-on-error: true`）を実行するか、テストを削除するか決める。

### M-20【新規】`db_pool_recycle` 設定が黙って無視される（`db/session.py` が 3600 をハードコード）

- **場所**: `config.py:99`（フィールド定義・`.env.example:194` に出荷）vs `db/session.py` 約 68 行目（`pool_recycle=3600` 直書き）
- **影響**: 運用者が設定しても反映されない（演算子向けの無言無視。他の死蔵 Settings（`page_title`/`layout` 等 Streamlit 残骸、elevenlabs 3 フィールド、`cache_ttl`、`default_language`）も死蔵モジュール削除時に一掃すべき）。
- **修正提言**: フィールドを読むか、フィールドと .env.example の行を削除。

---

## 5. Low

### 5-1. 認証・HTTP 表面（A1）

| ID | 内容 | 場所 |
|---|---|---|
| L-1 | `/api/auth/session` の 400 detail が認証無効（`REQUIRE_AUTH=0`）を匿名に開示（R0-#10 未解消） | `server.py:2523-2524,2388-2390` |
| L-2 | `_request_is_https` が設定されたヘッダ名を無視して `x-forwarded-proto` をハードコード・`trusted_proxy_hops` 未反映。`_client_ip` は最初の header 発生のみ読むため、攻撃者が先に XFF を置けば勝つ | `server.py:176-186,2436-2455` |
| L-3 | `audio_tenant` 経由の `/api/audio/*` が env-vs-Settings 矛盾 503 ガードを迂回（test 時のみ到達可能な防御的深さの問題） | `deps.py:277-283` vs `server.py:396-412` |
| L-4 | `GET /api/me` の docstring が同意必須と謳うがコードは `require_tenant` のみ（文書ズレ） | `me.py:11` vs `:424-425` |
| L-5 | `auth/authenticator.py:22` のモジュールレベル settings スナップショット（`upgrade_to_premium` でのみ使用、ほぼ死蔵） | `authenticator.py:22,442-445` |
| L-6 | LoginThrottle のインメモリ・プロセス内限界は自己文書化済み（現行デプロイは全単一レプリカで整合 — 受容済みリスクとして記録） | `authenticator.py:119-123` |

### 5-2. 生成パイプライン（A2）

| ID | 内容 | 場所 |
|---|---|---|
| L-7 | `normalize_song_text` が鉤括弧のみの曲名で空文字を返す → キー衝突でカタログ無音重複削除の潜在的罠（実在例: `【パラレル・シンクロニシティ】`） | `core/songs/__init__.py:108-135,311-324` |
| L-8 | `_tts_gate` がプロセス全体で TTS を直列化（テナント横断の律速。wait 中もロック保持）。per-pass 化で TTS 呼出は ~16-19 回/生成に増加中 | `server.py:886-939` |
| L-9 | edge-tts 合成（第一エンジン）にタイムアウト無し — gTTS で修正済みのハング問題クラスが主経路に残存 | `core/tts_engines.py:103-114` |
| L-10 | 陳腐な文書化: `songs/__init__.py:16-17`「36 曲しか無い」（実際 3,108 曲/74 年）、`server.py:599-600`「6〜7 回呼ばれる」（実際 ~16-19 回）、`song_selector.py:34`「3030 件」 | 各所 |
| L-11 | 2020s クイズが 2025 万博を過去形で出題（2020-2024 年の番組で未来の出来事。API 応答のクイズパネルに出るのみ・朗読されず） | `fallback.py:744-748` |

### 5-3. 並行処理・ジョブ（A3）

| ID | 内容 | 場所 |
|---|---|---|
| L-12 | `_run_job` が `succeed()` → `emit(done)` の順 — ドレインが 2 文の間に走ると SSE クライアントだけ `done` を取り逃す（他の全終端経路は emit-first。窓はマイクロ秒） | `server.py:2724-2725,2990-3002` |
| L-13 | SSE 待機がループ既定 executor（1 vCPU で ~5 スレッド）を占有し、接続上限 8 と不整合 → keep-alive 遅延が最悪 ~2 倍。専用 executor 化が一案 | `server.py:2968-2969,2922-2923` |
| L-14 | `create_job` の `emit(ESTIMATE)`・監査呼び出しがチケット取得後の try 外（理論上のリーク窓。実際には raise 不能） | `server.py:2822-2844` |
| L-15 | `client_gone` が set-only で読み手無し（P3-4 修正後の残骸） | `server.py:2953,3005`、`jobs.py:628` |
| L-16 | レイテンシ統計の `music` バケットが iTunes 一括解決（`_step_resolve_previews`）を計測しておらず、`estimate_generation_ms` が主要コストを過小評価。`DEFAULT_TTS_CALL_MS` は死蔵 | `server.py:1677,1970`、`jobs.py:456,550-552` |
| L-17 | SSE 切断でジョブごとキャンセルされる設計は文書化された意図（ページ更新が生成を止める）— 受容記録 | `server.py:2948-2955` |

### 5-4. データ・課金（A4）

| ID | 内容 | 場所 |
|---|---|---|
| L-18 | `FavoriteRepository.toggle` が所有権検証なし（P1-5 と同型の旧欠陥が死蔵メソッドに残存 — 配線前に修正 or 削除） | `db/repository.py:369-389` |
| L-19 | `find_by_stripe_customer` が `.first()` 無順序 — 顧客共有の旧データが残る場合の非決定性 | `db/repository.py:190-194` |
| L-20 | stripe_client の dict 分岐に `or {}` ガード無し（`metadata` が None の dict で AttributeError。本物の Stripe オブジェクトでは不発） | `stripe_client.py:74` |
| L-21 | alembic 構築 DB で `users.email` に unique 制約 + unique 索引が二重（H-1 と同根。後続リビジョンで統一） | `125758440996:36` + `88ad9aef7f29:40` |
| L-22 | 削除請求後の残置: `stripe_webhook_events.user_id` は匿名化されず 30 日の TTL のみ。削除イベントの監査行は意図的に生 user_id を保持（me.py:620-623 で文書化 — 法務確認事項として記録） | `privacy_repository.py:1004-1010`、`me.py:645-653` |
| L-23 | personal モードの同意 anonymous 畳み込みは残存するが、ゲート処理が user 無しをスキップし 409 で閉域化済み（実害は情報表示のみ — P1-12 の受容記録） | `me.py:379,408-411`、`deps.py:227-233` |
| L-24 | `AuditRepository.record` は meta にサイズ/深さ上限なし（API 層 `audit.py:62-100` は上限済み。内部呼び出しは固定小辞書のみ） | `privacy_repository.py:913-925` |
| L-25 | `authenticator.py:420,434` が `datetime.now()`（ローカル時刻）を一時代入 — 現在は `repo.update` が上書きするため不発、1 リファクタ先で発火 | `authenticator.py:420,434` |
| L-26 | `PURGE_TABLES` に消費者が無い（実際の purpe は `purge_user_personal_data`。メンテナがタプルを更新しても何も変わらない罠。コメントも陳腐） | `privacy_models.py:263-268` |
| L-27 | 非推奨 `datetime.utcnow()` が 4 箇所（本番 1: `me.py:488`、死蔵 3: `export_service.py:162,171,181`）。プロジェクト標準ヘルパは 3 重定義され共用されていない | 各所 |

### 5-5. フロントエンド（A5）

| ID | 内容 | 場所 |
|---|---|---|
| L-28 | SSE `seq` が `id:` 行にしか無く `GET /api/jobs/{id}` は `last_event_id` を無視 → フロントの再開ブックキーピングが両側で死蔵コード（`jobLastSeq` 常に 0。全 poll が全イベントを再適用 — 実害は冪等で無し） | `app.js:1828-1833,1751-1753`、`jobs.py:587-589`、`server.py:2858-2865` |
| L-29 | 生成タイムアウト 900 秒がジョブ経路で UI 終端を持たない + `pollJobOnce` のリトライが非 404 エラーで無限（サーバ側上限で有界） | `app.js:1472-1479,1740-1796` |
| L-30 | 202 応答前のキャンセル/タイムアウトが孤児ジョブを残す（token ガードが DELETE を握りつぶす） | `app.js:1281-1296,1514-1569` |
| L-31 | `beforeprint` リスナが `handlePrint` の保存済み状態を上書き（ボタン印刷で折りたたみ状態が復元されない） | `app.js:5107-5121` |
| L-32 | `loadDecades` の既定年レース（初期値と同値の明示選択が上書きされる）+ デケードチップがサーバ一覧を無視してハードコード | `app.js:5346-5382`、`index.html:186-196` |
| L-33 | 死蔵 state/refs（`consentRecorded`、`jobStreamToken`、`endedCount`、`xfadePending`、`guideDialogTitle`）、混入した外国語コメント（`:3180` アラビア語、`:2023` typo） | `app.js` 各所 |
| L-34 | 死蔵 CSS セレクタ（`.btn-audio-action`）+ 印刷コメントがコードと正反対 + `@media print` 3 ブロック重複 | `app.css:2419,3334-3344,2047-2114,2850-2932` |
| L-35 | ARIA tabs の `aria-controls` が自己参照（実害なし・表記不正） | `app.js:1087-1103` |

### 5-6. テスト・CI・依存（A6 + 主担当）

| ID | 内容 | 場所 |
|---|---|---|
| L-36 | ソース文字列テスト ~45 件残存（実挙動テストへの置換対象。TTS レート制限・SSE・GENERATION_STEPS 順序の grep が代表） | `test_tts_rate_limit.py:37-51`、`test_no_audio_gaps.py:590-598` ほか |
| L-37 | CI の lint 経路欠落: **`eval/` はどこでも lint されない**、`db/`/`utils/`/`scripts/` は pre-commit のみ、mypy CI 無し、coverage gate 無し | `ci.yml:51,58`、`.pre-commit-config.yaml:34` |
| L-38 | dukpy の `importorskip` が 4 ファイルで無警戒 — wheel ビルド失敗時に **app.js 振舞いテスト全体が黙って消える**（`import dukpy` の無条件ガード無し） | `test_frontend_playback.py:22` ほか |
| L-39 | 順序乱数ガード無し（`release.yml:46` の `-p no:randomly` は未インストールで no-op） | `requirements-dev.txt`、`release.yml:46` |
| L-40 | Dockerfile HEALTHCHECK が 8501 固定 vs render/railway の `--port $PORT`（現在は両プラットフォームが Docker HEALTHCHECK を無視するため不発） | `Dockerfile:72-73` |
| L-41 | fly.toml:27 の `PORT` env は死設定 | `fly.toml:27` |
| L-42 | 起動 bat に `alembic upgrade head` 無し（10-1 未解消）+ `.env` 探知ループのトークン解析バグ（値と鍵名を比較して常に一致せず、鍵設定済みでも再入力を促す） | `run_retro_radio.bat:35-44`、`run_retro_radio_ja.bat:38` |
| L-43 | requirements-dev の死蔵 pin（`pytest-mock` 未使用、`types-pytz` は pytz 無し、`codecov` PyPI は非推奨・CI は action 使用） | `requirements-dev.txt` |
| L-44 | `tests/test_load.py` が locust skip で恒久死 + Streamlit 時代のエンドポイントを叩く残骸 | `tests/test_load.py:3-18` |
| L-45 | requirements.txt の上限なしピン（fastapi/edge-tts/google-genai 等に上限なし・lockfile 無し。Dependabot + pip-audit 週次で緩和済み。click<8.2/PYSEC-2026-2132 は実測付きで文書化 — 例外運用として適切に管理されている） | `requirements.txt` |
| L-46 | pip-audit が `requirements-dev.txt` を cp932 既定の Windows で UnicodeDecodeError でクラッシュ（UTF-8 の日本語コメント付きのため。CI は ubuntu で無影響） | `requirements-dev.txt` |
| L-47 | conftest のネットワーク遮断が aiohttp（edge-tts の依存）をカバーしない（TTS_ENGINE=gtts 強制で緩和済みの文書化された地雷） | `conftest.py:164-175` |
| L-48 | eval README の「Nightly N1-N3」に該当 workflow が存在しない（N4 は pytest で実質カバー） | `eval/README.md:256-264` |
| L-49 | UTF-8 BOM が 6 ファイルに混入（素朴なツールを壊す。プロジェクト自身の衛生テスト方針と不整合） | `test_db_session.py` ほか 5 件 |
| L-50 | README のテスト数 3,012 件は実測 3,066 passed に対し軽微に陳腐（実行件数ベースではほぼ正確。静的関数数 1,856 とは計数法が異なる） | `README.md:437` |
| L-51 | `probe_edgecache.py` が `.dockerignore` 非対象 → Docker イメージに同梱 | `Dockerfile:46`、`.dockerignore` |
| L-52 | `rng_seed=None`（既定）時に `module_rng()` がグローバル `random` を返す（並行決定性なし）は文書化済みの意図的挙動 — seed 設定時は決定的 | `core/rng.py:36-39` |

---

## 6. テストの穴（10-02 §5 の再計測）

AST 全数調査（1,856 test functions・100 ファイル）:

| 指標 | 10-02 版 | 現在 | 傾向 |
|---|---|---|---|
| assert/raises/mock 検査ゼロのテスト | 23 | **25** | 横ばい（+2）。最悪: `test_billing.py` 5/49、`test_tts_branches.py` 3/26 |
| 実装ソース文字列を検査するテスト | 50 | **~45** | 微減（計数法差あり） |
| 指名 targets（5-1/5-3/5-5/5-6/WCAG） | 6 件 | 3 件修正・3 件残存 | 5-1/5-3/5-4 は本体修正済み、5-5/5-6/WCAG `pass` は残存 |
| import 時 env 書き込み | 1 ファイル | **2 ファイル** | **悪化**（M-14） |
| eval ゲート | 全体的に空虚 | fact_score 側は修正・**checklist 側が残存** | 半修正（M-13） |

**eval ゲートの残存限界**（5-2(a) は設計として存続）: `--offline` の eval は `_deterministic_script`（フォールバック生成器）のみを計測し、Gemini 経路（`generate_radio_script`）は一度も通らない。Gemini 品質の対抗手段は `eval/bww/` の人手評価（`eval/README.md` 記載）だが、**実施記録が 1 件も存在しない**（`eval/results/` は README のみ）。ゲートが測るものと「原稿品質」の距離は文書化された設計判断として維持するなら、bww の初回実施を運用タスクとして明示すべき。

## 7. デプロイ設定の不整合（統合表）

| 項目 | 状態 | 場所 | 深刻度 |
|---|---|---|---|
| `RETRO_RADIO_TRUSTED_PROXY_HEADER` が全デプロイ設定に無い → Secure Cookie/HSTS 不発・スロットル崩壊 | **未解消（最重要）** | `fly.toml`/`render.yaml`/`railway.json` | **M-5** |
| Dockerfile HEALTHCHECK 8501 固定 vs `--port $PORT` 起動（render/railway） | 未解消（現在は不発） | `Dockerfile:72-73` | L-40 |
| fly.toml `PORT` env 死設定 | 未解消 | `fly.toml:27` | L-41 |
| 起動 bat に migration 無し + `.env` 探知バグ | 未解消 | `run_retro_radio*.bat` | L-42 |
| `probe_edgecache.py` がイメージに同梱 | 新規 | `.dockerignore` | L-51 |
| railway.json に env が書けない件 | **解消済み**（`railway.env.example` が正本・`requiredMountPath` で音を立てて失敗） | `railway.json` | ✅ |
| `.dockerignore` の secret/DB 除外 | **良好**（`.env`/`*.db`/秘密パターンを明示除外・テストも存在） | `.dockerignore:22-37` | ✅ |

## 8. 着手順（推奨）

**Phase A — 利用者が嘘を聞く状態（最優先・内容誠実性）**
1. **C-1**（ゼロ音源の虚偽朗読）— care/anniversary の 4 箇所に音源 0 分岐を追加
2. **H-4**（facts 局名 2 件修正）+ 3 レコードの一次文献検証パス
3. **H-5**（ニュース候補の event_year フィルタ）
4. **M-8**（`ctx.enriched` 空分岐の INTERMISSION_TITLE 契約）+ **M-13**（eval checklist の照合窓 — 以降の内容系 P0 が eval で赤くなるようにする最初の 1 行）

**Phase B — ビルド・ゲートの赤（先に直さないと以降の CI が信用できない）**
5. **H-1**（`ix_users_email` ドリフト）— `alembic check` を緑に戻す
6. **M-16**（mypy を CI に計測として入れる）+ **M-17**（billing の zero-assert 5 件）

**Phase C — リソース・CPU・可用性**
7. **H-2**（PBKDF2 鍵キャッシュ — 1 行）
8. **M-4**（ボディ上限ミドルウェア）+ **M-10**（webhook 台帳の try 移動）
9. **M-9**（テナント max_files ポリシー統一）+ **L-9**（edge-tts タイムアウト）

**Phase D — 運用・保守**
10. **M-5**（TRUSTED_PROXY_HEADER をデプロイ設定へ）
11. **M-1**（coverage_ratio の独立分母）+ **M-2**（クォータ仕様の確定）
12. **M-3**（Origin/Referer ミドルウェア）+ **M-19**（network マーカーの週次実行）
13. **M-15**（死蔵 ~2,690 LOC の段階的削除 — `__init__` 2 本・services/__init__・hygiene allowlist の同時更新が必要）
14. **L-37〜L-44**（CI/テストの衛生）と Low 項目の掃除

## 9. 検証方法

- **ゲート実測**: §0 の表のとおり全コマンドを実行（pytest 228.58s、alembic は `%TEMP%` の一時 DB）。記憶・推測の値は書いていない。
- **再現コマンド**（Critical/High の主なもの）:
  - C-1: `python -c` で `pinned_songs([])` コンテキスト下の `generate_care_script` / `generate_anniversary_script` を生成し虚偽文を検出（§2 参照）
  - H-1: 一時 DB で `alembic upgrade head && alembic check` → `remove_index ix_users_email`
  - H-2: `hashlib.pbkdf2_hmac('sha256', s, b'domain', 100_000)` × 5 回の平均 = 66.7 ms
  - H-3: `clean_script_for_tts('音楽は私たちの思い出の箱でございます。')` → `''`
  - H-4: `programs.json` の network フィールド照合（オールナイトニッポン = ニッポン放送が正、パックイン = TBSラジオが正）
  - H-5: `NEWS_TOPICS_BY_DECADE[1950]` の候補に 1958/1959 年の出来事が含まれることを確認 + `select_news_topics` に年フィルタが無いことをコード確認
- **全領域の所見は file:line のコード引用を必須化**し、領域レビュー（A1〜A7、7 本のサブエージェント）が報告したエビデンスを主担当が抽読して採録した。重大項目（C-1、H-1〜H-5）は主担当が自ら再現・確認済み。

## 10. よかった点（潰さないこと）

1. **内容誠実性の 5 層防御**は構造的に正しい: `_song_item`/`_to_song_dicts` だけが曲を API 形へ変換し、`[]`≠`None` の意味が server → `validate_song_pairs` → allowlist → `pinned_songs` → テンプレートまで保存されている（M-8 の 1 分岐を除く）。
2. **preview_resolver の transport/miss 弁別**は模範的: `PreviewTransportError` で「無い」と「繋がらない」を分け、負キャッシュを汚さない、R2-07core の 400/404 breaker-success も維持。
3. **ジョブ/チケット機構は回帰テストで釘付け済み**: `on_finish` 冪等化（JOB-08）の全終端経路に専用テスト、`GenerationSlots.release` の順序も修正維持（A3 検証）。
4. **フロントの XSS 規律**は静的解析でも揺るがなかった: innerHTML シンクは 1 箇所のみ（`app.js:1994`）で全補間が `escapeHtml` 済み、外部リンクはホスト許可リスト、`aria-live` 運用（進捗ティッカーを SR に流さない配慮）など a11y は本物。
5. **CI workflow の衛生**は極めて高い: 全 action を 40 桁 SHA でピン（テストで強制）、最小権限、`test_infra_hardening.py` が workflow YAML をパースして検査（grep ではなく）。
6. **秘密情報の扱い**: `.env` 未追跡、32 文字下限の 4 経路強制、`hide_parameters=True` + ログ redact、`/health` の匿名開示面が過去修正を維持。
7. **requirements.txt の click コメント**は計測証拠つきの例外運用文書として模範例（PIP-audit ignore との相互参照も維持）。
8. **文書化文化**: 障害→修正→回帰テスト→コメントの連鎖が樹木化されており、今回の再検証（40 項目中 33 項目の修正維持）はこの文化の直接の成果。
9. **eval ハーネスの構造**: threshold の report==enforce 契約が構造的に保証され、「計測不能は N/A」を明示、exit code を subprocess で e2e テスト。
10. **カタログ規模の拡大**: 36 曲 → 3,108 曲/74 年に拡張され、`catalog_health` で不足年を監視する運用が組まれている（陳腐な「36 曲」コメントだけ更新要）。

---

## 付録: 領域別の所見数

| 領域 | Critical | High | Medium | Low/Info |
|---|---|---|---|---|
| A1 HTTP・認証 | — | H-2 | M-1, M-2, M-3, M-4, M-5 | 6 |
| A2 生成パイプライン | C-1 | H-3, H-4, H-5 | M-6, M-7, M-8 | 5 |
| A3 並行処理 | — | — | M-9（A7 と共同）, M-2（共同） | 6 |
| A4 データ・課金 | — | — | M-10, M-11 | 10 |
| A5 フロントエンド | — | — | M-12 | 8 |
| A6 テスト・CI | — | — | M-13, M-14, M-17, M-19 | 12 |
| A7 死蔵コード | — | — | M-15, M-18, M-20 | 多数（§5 表） |
| 主担当（ゲート/マイグレーション） | — | H-1 | M-16 | L-46, L-50 |

