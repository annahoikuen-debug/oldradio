# 実装計画 Round 4 — 残存リスク P0 + P1（機械的）対応（2026-10-01）

> **ステータス: 完了（2026-10-01）**
>
> 全項目実装済み。回帰テスト `tests/test_round4_regression.py`（22 件）全パス。
>
> ゲート結果:
> - フル pytest: **2940 passed / 3 skipped / 3 xfailed / 0 failed**
> - flake8 (`retro_radio utils eval scripts tests`): **違反 0 件**（KNOWN_F401_ALLOWLIST を空に更新）
> - eval `--offline --threshold 80`: **24/24 合格**、fact score 平均 100.0
> - alembic check: 新規マイグレーション無し
> - validate_songs / validate_facts: **fail 0 件**（warn のみ）
>
> 実装中に発見・修正した追加不具合:
> - `server.py` の `_QueueTicket` が `_job_queue_slots` をハードコードし、SSE チケットが
>   誤ってジョブキューのセマフォを解放していた → スロット注入型（`__init__(slots)`）に修正。
> - `.env.example` に `RETRO_RADIO_TTS_CACHE_MAX_FILES` / `RETRO_RADIO_RNG_SEED` が無く
>   環境テンプレート検証が落ちていた → 追加し、`INTENTIONAL_ENV_EXAMPLE_DIVERGENCE` に
>   RNG_SEED の相違理由を登記。
> - 未使用 import（fallback.py / script_generator.py / song_selector.py の `random`、
>   utils/design_tokens.py の `os` / `typing.List`）を解消。

対象: `docs/final_review_report_2026-10-01.md` 第 4 節の残存リスクのうち
**P0 3 項目** と **P1 の機械的な項目**。仕様判断が必要な
R2-08 / DB-01 / R2-07（P1 の「仕様判断が必要」版）は本 Round の対象外とする。

原則（Round 1〜3 の教訓を踏襲）:

1. **主担当が自分で再現**してから修正に入る（実証列に `ファイル:行` を書く）。
2. **仕様を変えるときは依拠しているテストを全部洗い出し**、壊れるテストは契約に合わせて更新する。
3. **修正ごとに回帰テストを書く**（リグレッション防止）。
4. 編集作業中に `git stash` を使わない。日本語長文字列は最小差分 + ASCII アンカーで書く。

---

## 0. ゲート基準（完了条件）

| ゲート | 目標 |
|---|---|
| `python -m pytest -q` | 2918 + 追加分 passed / 0 failed |
| `flake8 tests` / `flake8 eval` / app-baseline | EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0 / 24 of 24 |
| `alembic upgrade head` → `alembic check` | No new upgrade operations detected |
| `validate_songs.py` / `validate_facts.py` | fail 0 / warn のみ（現状維持） |

---

## 1. R2-04（P0）: Stripe webhook のリクエストボディ無制限

### 実証

- [`retro_radio/server.py:2078`](../retro_radio/server.py) — `payload = await request.body()`
  が**署名検証（`handler.handle_event`）の前に**ボディ全体をメモリに読む。
  Content-Length 制限が無いため、攻撃者は巨大ボディでメモリを枯渇させられる
  （署名検証で 400 になるとしても、読み込み時点で被害が発生する）。

### 修正

`retro_radio/server.py` の `stripe_webhook`:

- ボディ上限 `WEBHOOK_BODY_MAX_BYTES = 1_000_000`（Stripe 実イベントの最大を大きく下回る値）をモジュール定数として追加。
- `await request.body()` の**後**に `len(payload)` を検査し、超過は `413` で拒否
  （読み込み後の検査でも、上限を超えたペイロードが以降の処理に入らないことを保証）。
- `content-length` ヘッダーを**読み込み前**に検査し、上限超過は 413 で早期拒否
  （巨大ボディの読み込み自体を回避する第 1 防衛線）。

### 依拠テスト（洗い出し）

- `tests/test_billing.py` — webhook 系テスト（正常系の小さなペイロードのみ、影響なし見込み）。
- `tests/test_security.py` — 念のため確認。

### 回帰テスト

`tests/test_billing.py` に追加:

- `test_stripe_webhook_rejects_oversize_content_length` — `content-length` 超過で **413**、
  `handle_event` が呼ばれないこと。
- `test_stripe_webhook_rejects_oversize_body` — 実ボディ超過で **413**。
- `test_stripe_webhook_normal_payload_accepted` — 正常サイズは従来どおり受理。

---

## 2. R2-05 / JOB-02（P0）: `/api/generate` の入場枠がブロッキング acquire

### 実証

- [`retro_radio/server.py:1782`](../retro_radio/server.py) — `generate_radio` が
  `_generation_slots.acquire(timeout=settings.generation_wait_timeout)`（既定 30 秒）で
  **待つ**。同期 `def` のため FastAPI スレッドプールで実行され、40 並行リクエストで
  **anyio のスレッドプール（既定 40 スレッド）が枯渇**し、`/api/auth/session` まで応答不能になる。
- 同様のブロッキングは [`retro_radio/server.py:2116`](../retro_radio/server.py)（`_run_job` 内）にもあるが、
  こちらは**専用ワーカースレッド**（`jobs.start_worker` で自前スレッド）内なので
  anyio プールを消費しない。ただし待機ジョブがスレッドを掴み続ける点は同様。

### 修正

`retro_radio/server.py` の `generate_radio`:

- `acquire(timeout=...)` を **`acquire(blocking=False)`** に変更。空きが無い場合は
  既存の 503 を即座に返す（監査記録も既存のままでよい）。
  - トレードオフ: 「30 秒待って入れる」挙動がなくなる。ただし `/api/jobs`（非同期・202）
    が正式経路として存在するため、混雑時はそちらへ誘導する契約で一貫する
    （`create_job` も既に blocking=False + 503 で断る設計）。
- `_run_job` 側（2116 行）は保持: ワーカースレッド内での待ちは anyio プールを
  枯渇させない。ただし `generation_wait_timeout` 待ちを短縮せず現状維持（仕様判断の余地があるため）。

### 依拠テスト（洗い出し）

- `tests/test_job_api.py` / `tests/test_server_api.py` — スロット枯渇時の 503 検証。
  「acquire が timeout で失敗する」前提のテストは monkeypatch 方式なら影響なし。
- `tests/test_load.py` / `tests/test_load_concurrency.py` — 並行テスト。blocking=False 化で
  「待って入れていた」前提のテストがあれば更新。

### 回帰テスト

`tests/test_server_api.py` に追加:

- `test_generate_does_not_block_event_loop_when_slots_full` — スロット全埋めのとき
  同期ハンドラが即座に 503 を返すこと（30 秒待たない）。
- `test_generate_returns_503_immediately_under_load` — 40 並行で他の軽量エンドポイント
  （`/health`）が応答し続けること。

---

## 3. R2-06（P0）: SSE 同時接続数上限が無い

### 実証

- [`retro_radio/server.py:2340-2363`](../retro_radio/server.py) — `stream_job_events` に
  同時接続数の上限が無い。1 接続 = `_sse_stream` の
  `loop.run_in_executor(None, job.wait_for_events, ...)`（[`server.py:2319`](../retro_radio/server.py)）
  で**既定 executor（anyio スレッドプール）の 1 スレッドを `SSE_KEEPALIVE_SECONDS` 間隔の
  ポーリングで最大 `SSE_MAX_SECONDS`（900 秒）占有**する。
  接続数に上限が無いため、40 接続でスレッドプールが枯渇し全 API が応答不能になる。

### 修正

`retro_radio/server.py`:

- SSE 専用の入場枠 `_sse_slots = GenerationSlots(SSE_LIMIT)` を追加
  （`SSE_LIMIT = max(4, settings.max_concurrent_generations * 4)` — ジョブ枠と同じ感覚）。
- `stream_job_events` の入口で `_sse_slots.acquire(blocking=False)` し、取れなければ
  **503** で断る（StreamingResponse を作らない）。
- `_sse_stream` の `finally` で `_sse_slots.release()`（クライアント切断・終端・タイムアウト
  のどれでも必ず解放）。
- 解放の二重化を防ぐため ticket 方式（項目 5 と共通の `_QueueTicket`）を使う。

### 依拠テスト（洗い出し）

- `tests/test_job_api.py` — SSE ストリーム系テスト。単発接続のみなら影響なし。
- `tests/test_frontend_playback.py` — SSE 契約（ヘッダー・イベント種別）は変更しない。

### 回帰テスト

`tests/test_job_api.py` に追加:

- `test_sse_stream_rejects_when_concurrent_limit_reached` — 枠全埋めで **503**、
  ストリームが始まらないこと。
- `test_sse_stream_releases_slot_on_client_disconnect` — 切断時に枠が 0 に戻ること。

---

## 4. DB-02（P1, 機械的）: Stripe 解約時に `metadata.user_id` が無いと降格失敗

### 実証

- [`retro_radio/billing/webhook.py:171-188`](../retro_radio/billing/webhook.py) —
  `_handle_subscription_deleted` が `metadata.user_id` だけを見る。Stripe ダッシュボードや
  Billing Portal から解約された場合、subscription の metadata に `user_id` が無く
  （metadata は `checkout.session.completed` 時にこちらで付与したものが subscription に
  自動反映されるとは限らない）、**warning ログだけで降格されず DB 上で永久プレミアム**になる。

### 修正

`retro_radio/billing/webhook.py` の `_handle_subscription_deleted`:

- `metadata.user_id` が無いとき、**`customer_id` で逆引き**する:
  `repo` に `find_by_stripe_customer(customer_id)` を追加し
  （`retro_radio/db/repository.py` の `UserRepository`）、見つかったユーザーを FREE に降格。
- 逆引きでも見つからない場合のみ warning（現状の挙動）。
- `_handle_subscription_updated` も同様に逆引きを fallback として追加（同じ機械的欠落）。

### 依拠テスト（洗い出し）

- `tests/test_billing.py` — `customer.subscription.deleted` 系テスト。
  metadata.user_id ありの既存テストは影響なし。逆引きを足すだけのため壊れない見込み。

### 回帰テスト

`tests/test_billing.py` に追加:

- `test_subscription_deleted_downgrades_by_customer_id` — metadata に user_id が無くても
  `stripe_customer_id` 逆引きで **FREE に降格**すること。
- `test_subscription_updated_downgrades_by_customer_id` — 同上（updated 経路）。

---

## 5. JOB-08 → JOB-05（P1, 機械的）: `_job_queue_slots` の冪等性がカウンタ依存

### 実証

- [`retro_radio/server.py:252-279`](../retro_radio/server.py) — `_acquire_job_queue_slot` /
  `_release_job_queue_slot` がプロセスグローバルの `_job_queue_held` カウンタ（`threading.Lock`
  で保護されるが**スレッド単位でない**）で冪等性を実現している。
  「解放は 1 回だけ」という契約をカウンタで数えており、解放の呼び出し側が
  2 回呼ぶと他のジョブの枠を壊す。Ticket 方式（誰が取得したかを明示）にすることで
  二重解放がカウンタを壊さない構造にする。

### 修正

`retro_radio/server.py` + `retro_radio/jobs.py`:

- `_QueueTicket` を導入: `_acquire_job_queue_slot()` は成功時に ticket（`None` でないオブジェクト）
  を返し、`_release_job_queue_slot(ticket)` は ticket が有効な間だけ解放する。
  ticket は 1 回解放したら無効化される（二重解放でカウンタが壊れない）。
- `create_job` / `_run_job` / `_sse_stream` を ticket 経由に書き換える。

### 依拠テスト（洗い出し）

- `tests/test_job_api.py` — `_acquire_job_queue_slot` / `_release_job_queue_slot` を直接
  触るテストがある場合は戻り値型の変更に合わせて更新。

### 回帰テスト

`tests/test_job_api.py` に追加:

- `test_job_queue_ticket_double_release_is_noop` — 同一 ticket の二重解放で
  `_JOB_QUEUE_LIMIT` が壊れないこと。
- `test_job_queue_ticket_release_does_not_affect_other_slots` — 別 ticket の解放で
  枠数が正しいこと。

---

## 6. CACHE-01（P1, 機械的）: TTS キャッシュに個数上限が無い

### 実証

- [`retro_radio/server.py:482-515`](../retro_radio/server.py) — `_sweep_tts_cache` は
  TTL 判定のみ。`tts_cache_sweep_interval`（既定 50）+ `generate_tts_cached` 呼び出し回数の
  **二重ゲート**で定期スイープが 2500 回に 1 回（= 概ね 8〜9 リクエスト相当のはずが、
  実質ほぼ動かない）しか動かない可能性があり、かつ**個数上限が無い**ため
  TTL 内にファイルが無制限に蓄積する。
- [`retro_radio/services/tenant_cache.py:236-282`](../retro_radio/services/tenant_cache.py) —
  `sweep()` も TTL 判定のみで個数上限が無い。

### 修正

- `retro_radio/services/tenant_cache.py` — `TenantTtsCache` に `max_files`
  （既定 `settings.tts_cache_max_files`、config に新設 `tts_cache_max_files: int = 2000`）
  を追加。`sweep()` 内で TTL 削除の後、テナント配下のキャッシュファイル数が
  `max_files` を超えていれば**古い順に削除**する。
- `retro_radio/config.py` — `tts_cache_max_files: int = Field(default=2000, ge=100, le=100000)` を追加。
- `retro_radio/server.py` — `_sweep_tts_cache` のフラット版も同様に個数上限を適用。

### 依拠テスト（洗い出し）

- `tests/test_cache_service.py` / `tests/test_favorite_service_cache.py` — sweep 系。
  個数上限の追加は既存 TTL 挙動を壊さない。
- `tests/test_config.py` — 新設フィールドの既定値テストを追加。

### 回帰テスト

`tests/test_cache_service.py` に追加:

- `test_tenant_cache_sweep_enforces_max_files` — `max_files` 超過で古い順に削除されること。
- `test_tenant_cache_sweep_keeps_recent_files_over_ttl` — TTL 内の新しめのファイルが
  個数上限で消えないこと（上限は古い順に適用）。
- `test_config_has_tts_cache_max_files_default` — 既定 2000。

---

## 7. R2-05（コア, P1）: `order_candidates` が history 空で対象年保証を壊す

### 実証

- [`retro_radio/core/song_selector.py:96-98`](../retro_radio/core/song_selector.py) —
  `fresh = [pair for pair in decorated if pair[0] == 0]` → `self._rng.shuffle(fresh)`。
  `songs.pool_for_year` が「対象年の曲を先に返す」保証を持っていても、
  `select()` が 10 年広げて足した曲（他年）も `last_played == 0` になり
  **fresh に混ざってシャッフル**されるため、対象年の曲が `ordered[:count]` の
  枠外に落ちる（1964 年で 18 枠中対象年平均 2.5 曲、min 0）。

### 修正

`retro_radio/core/song_selector.py`:

- `pool_for_year` が「対象年 → 広げた年」の順で返していることを利用し、
  `order_candidates` に**入力順の安定キー**を足す: 同値グループ（fresh）内の
  シャッフルを、`candidates` の**元のインデックス**を tie-break に使った
  「部分シャッフル + 入力順 tie-break」にする。
  具体的には fresh を「対象年の曲 / それ以外」に 2 分割し、対象年の曲を先に
  配置してから各グループ内で `rng.shuffle` する。
  - 注意: `record` に `release_year` が無い候補もあるため、
    `record.get("release_year")` と `year` の比較で対象年判定する
    （判定できない曲は「それ以外」側に置く）。

### 依拠テスト（洗い出し）

- `tests/test_history_service.py` / `tests/test_fallback_mechanism.py` / eval の
  `song_duplication` ゲート。シャッフルの挙動を変えるため、
  「fresh 内が完全ランダム」を固定しているテストがあれば更新。

### 回帰テスト

`tests/test_history_service.py` に追加:

- `test_order_candidates_keeps_target_year_first` — 対象年の曲が必ず
  `ordered[:count]` に含まれること（広げた年が混ざっても）。
- `test_order_candidates_shuffles_only_within_year_groups` — 同一年グループ内は
  シャッフルされ、対象年 → 他年 の順序が保たれること。

---

## 8. 決定論の残り 3 箇所（P1, 機械的）: rng 注入

### 実証

- [`retro_radio/core/script_generator.py:111`](../retro_radio/core/script_generator.py) —
  `select_news_topics` が `random.sample(pool, ...)`。同一入力で最大 11 通りの
  台本ができる（テストの再現性・監査性が失われる）。
- [`retro_radio/core/song_selector.py:59`](../retro_radio/core/song_selector.py) —
  `self._rng = rng or random.Random()`。既定が非決定論的。
- [`retro_radio/core/fallback.py:263,265,289`](../retro_radio/core/fallback.py) —
  `get_fallback_song` / `get_fallback_songs` が `random.choice` / `random.shuffle`。

### 修正（seed 設計）

- `retro_radio/config.py` — `rng_seed: Optional[int] = None` を追加
  （`None` = 決定論なし・現行挙動。施設運用・eval では seed を設定）。
- `retro_radio/core/script_generator.py` — `select_news_topics(year, count, rng=None)`
  に rng 引数を追加し、`rng = rng or _module_rng()`。
  `_module_rng()` は `settings.rng_seed` が int のとき `random.Random(seed)` を返し、
  `None` のときグローバル `random` を返す。
- `retro_radio/core/fallback.py` — `get_fallback_song` / `get_fallback_songs` /
  `select_program_songs` に同様の rng 注入を追加（既定は `_module_rng()`）。
- `retro_radio/core/song_selector.py` — `SongSelector.__init__` の既定を
  `rng or _module_rng()` にする（seed 設定時は決定論的になる）。
- **互換**: 既存の呼び出し側（`server.py` / `pipeline.py`）は引数を渡さないため
  変更不要。`settings.rng_seed=None`（既定）なら現行挙動と完全に同じ。

### 依拠テスト（洗い出し）

- `tests/test_script_length.py` / `tests/test_script_song_alignment.py` / eval ゲート —
  既定（seed なし）は挙動不変のため影響なし。
- `tests/test_core_models.py` — rng 注入の単体テストを追加。

### 回帰テスト

`tests/test_core_models.py` に追加:

- `test_select_news_topics_deterministic_with_seed` — 同一 seed で同一結果。
- `test_select_news_topics_deterministic_default_when_configured` —
  `settings.rng_seed` 設定時に `select_news_topics` が同一入力で同一結果。
- `test_fallback_songs_deterministic_with_seed` — seed 設定時、
  `get_fallback_songs` が同一入力で同一結果。
- `test_song_selector_deterministic_with_seed` — seed 設定時、
  `order_candidates` が同一入力で同一順。

---

## 9. R2-07（コア, P1, 機械的）: `preview_resolver` のサーキットブレーカーが非連続失敗で開く

### 実証

- [`retro_radio/core/preview_resolver.py:298-310`](../retro_radio/core/preview_resolver.py) —
  `_fetch_itunes` 内の `raise_for_status()` で 400/404（=「この検索語に結果は無い」性質の
  **正常応答**）は `return []` で戻るが、この経路は `_breaker_success()` を通らない。
  結果として「正常な 404 を挟んだ失敗」が `_breaker_failures` を**非連続に**積み上げ、
  `_BREAKER_FAILURES` 回に達すると全ホスト 60 秒無音になる
  （正常応答が成功シグナルにならないのが根因）。

### 修正

`retro_radio/core/preview_resolver.py`（**1 行**）:

- 400/404 の `return []` の直前に `_breaker_success()` を追加する。
  「iTunes に到達して応答を解釈できた」時点で成功シグナルとするのが正しい
  （トランスポート層の正常性と検索結果の有無を区別する）。

### 依拠テスト（洗い出し）

- `tests/test_eval_harness_hardening.py` / `tests/test_fallback_mechanism.py` /
  `tests/test_catalog_and_fallback_hardening.py` — breaker 系テスト。
  400/404 で breaker が**リセットされる**ようになるため、「404 で failures が増える」
  前提のテストがあれば更新。

### 回帰テスト

`tests/test_fallback_mechanism.py` に追加:

- `test_breaker_does_not_open_on_intermittent_404` — 400/404 を挟んだ非連続失敗で
  遮断器が開かないこと（`_breaker_open_until == 0.0` のまま）。
- `test_breaker_success_signal_on_404` — 400/404 応答後に `_breaker_failures == 0`
  に戻ること。

---

## 10. 実装順序

1. **機械的で独立性の高いものから**（コンフリクトを避けるため）:
   - 項目 9（R2-07 コア・1 行）→ 項目 8（決定論・rng 注入）→ 項目 6（CACHE-01）
2. **billing 系**: 項目 4（DB-02）→ 項目 1（R2-04）
3. **並行制御系**（`server.py` の重複編集を避けてまとめて）:
   - 項目 5（`_QueueTicket`）→ 項目 2（R2-05）→ 項目 3（R2-06）
4. **コア選曲**: 項目 7（R2-05 コア）

各項目の完了ごとに `python -m pytest -q`（関連テストのみ先に実行して早期確認）し、
全項目完了後に最終ゲートを実行する。

## 11. 回帰テスト一覧（合計 17 件見込み）

| テストファイル | テスト名 | 対象項目 |
|---|---|---|
| tests/test_billing.py | test_stripe_webhook_rejects_oversize_content_length | R2-04 |
| tests/test_billing.py | test_stripe_webhook_rejects_oversize_body | R2-04 |
| tests/test_billing.py | test_stripe_webhook_normal_payload_accepted | R2-04 |
| tests/test_billing.py | test_subscription_deleted_downgrades_by_customer_id | DB-02 |
| tests/test_billing.py | test_subscription_updated_downgrades_by_customer_id | DB-02 |
| tests/test_server_api.py | test_generate_does_not_block_event_loop_when_slots_full | R2-05 |
| tests/test_server_api.py | test_generate_returns_503_immediately_under_load | R2-05 |
| tests/test_job_api.py | test_sse_stream_rejects_when_concurrent_limit_reached | R2-06 |
| tests/test_job_api.py | test_sse_stream_releases_slot_on_client_disconnect | R2-06 |
| tests/test_job_api.py | test_job_queue_ticket_double_release_is_noop | JOB-08 |
| tests/test_job_api.py | test_job_queue_ticket_release_does_not_affect_other_slots | JOB-08 |
| tests/test_cache_service.py | test_tenant_cache_sweep_enforces_max_files | CACHE-01 |
| tests/test_cache_service.py | test_tenant_cache_sweep_keeps_recent_files_over_ttl | CACHE-01 |
| tests/test_config.py | test_config_has_tts_cache_max_files_default | CACHE-01 |
| tests/test_history_service.py | test_order_candidates_keeps_target_year_first | R2-05（コア） |
| tests/test_history_service.py | test_order_candidates_shuffles_only_within_year_groups | R2-05（コア） |
| tests/test_core_models.py | test_select_news_topics_deterministic_with_seed | 決定論 |
| tests/test_core_models.py | test_select_news_topics_deterministic_default_when_configured | 決定論 |
| tests/test_core_models.py | test_fallback_songs_deterministic_with_seed | 決定論 |
| tests/test_core_models.py | test_song_selector_deterministic_with_seed | 決定論 |
| tests/test_fallback_mechanism.py | test_breaker_does_not_open_on_intermittent_404 | R2-07（コア） |
| tests/test_fallback_mechanism.py | test_breaker_success_signal_on_404 | R2-07（コア） |
