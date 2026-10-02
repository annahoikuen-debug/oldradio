# コードレビュー — retro_radio 全体精査（2026-10-02）

対象: `eca1eb0`（HEAD）+ 未コミット変更（Round 4 相当）
方法: 5 領域をサブエージェントに分割し、**重大項目はすべて主担当が自分で再現して確証**した。

---

## 0. ゲート実測（すべて実測値）

| ゲート | 結果 |
|---|---|
| `python -m pytest -q` | **2940 passed / 3 skipped / 3 xfailed / 0 failed**（237.9s） |
| `flake8 tests` / app-baseline / `eval` / `utils scripts` | すべて EXIT 0 |
| `python -m eval --offline --threshold 80` | EXIT 0 / 24 of 24 / 平均 100.0 |
| 本レビューで再現した実バグ | **P0 8 件 / P1 12 件 / P2 24 件 / P3 8 件** |
| 緑のまま何も検査していないテスト | 23 件（assert ゼロ）+ 50 件（ソース文字列検査） |

**緑 2940 件は、この后发现した 8 件の P0 のどれ一つも検出できていない。**
かつ `python -m eval` の「24/24・平均 100.0」も、
下記 P0-1〜P0-5 の**正にそれを隠す入力**でのみ緑になっている（第 6 節参照）。

> **第 10 節 = 実機デバッグ結果。** uvicorn を実際に起動して 5 年分を生成し、
> P0-1 / P0-4 / P0-7 を **HTTP 経路で実証**した。あわせて
> 「18 スロットに対し可聴曲が 6〜14 曲しかない」という
> どのゲートも測っていない稼働指標が出た。

---

## 1. P0 — 出荷すると利用者・収入・可用性を壊す

### P0-1【新規・再現済】周回 2〜3 周目で「司会が A を告げて B が流れる」

**これが本プロジェクト自身が最重要欠陥と定義した問題**（`retro_radio/core/preview_resolver.py:6-16`）。
Round 4 でパス内の対応（曲 i の直後 = 曲 i+1）は直ったが、**パス間は直っていない**。

経路:
- `retro_radio/server.py:1750-1760` — `GENERATION_STEPS` は `_step_generate_script` を**1 回だけ**実行する。
- `retro_radio/server.py:1682-1738` — `_step_build_passes` は `loop_count`（既定 3）回 `build_playlist` を呼ぶが、
  **渡るのは `ctx.segments`（同一オブジェクト）だけ**。曲 `chunk` だけがパスごとに異なる。
- `static/app.js:3176-3179` — フロントも `buildPass(data, usablePasses[k])` でパスごとに同じ原稿を再生する。

**再現（決定的）**:

```
segs  = [talk0, talk1, talk2]        # 同一の ScriptSegment 3 つ
pass1 = build_playlist(segs, [A0,A1,A2,A3])
pass2 = build_playlist(segs, [B0,B1,B2,B3])

pass1 [('song','A0'),('talk','t0'),('song','A1'),('talk','t1'),('song','A2'),('talk','t2'),('song','A3')]
pass2 [('song','B0'),('talk','t0'),('song','B1'),('talk','t1'),('song','B2'),('talk','t2'),('song','B3')]
```

`pass2` のトーク本文は `pass1` と完全に同一 = 「この年のヒット曲『A0』をお届けいたします」
を言いながら B0 が流れる。**既定 3 周のうち 2 周が嘘になる。**

追加の深刻度: トークの音声（TTS）も同一ファイルなので、
「曲 A を 3 回紹介」しつつ 3 周分別の曲が流れる。

**既存テストの穴**: `tests/test_script_song_alignment.py` の `_cue_mismatches` は
`build_playlist(segments, _song_dicts())` を**1 パスだけ**呼ぶ。パス間の検証が 1 件も無い。
`tests/test_song_catalog.py:449-495` は逆に「パスごとに別の曲」を**契約として要求**しているため、
この設計がミスマッチを生むことをテストが固定 الوق足している。

**修正方針（要仕様判断）**: (a) パスごとに原稿を生成する、(b) 全パスで同一の曲集合を使う、
(c) 曲名を出さないモードPamを既定にする。現実的には (c)+(a) の折衷。
どれを採るかは `plans/ui_ux_contract.md` と相談が必要。

---

### P0-2【新規・再現済】`_QueueTicket` が `cancel-before-start` で永久リーク

Round 4 で導入された入場枠チケットが、**ジョブを開始前にキャンセルされると永久に解放されない**。

`retro_radio/jobs.py:887-890`:
```python
if job.is_cancel_requested:
    job.mark_cancelled("cancelled_before_start")
    job.emit(EVENT_CANCELLED, reason="cancelled_before_start")
    return          # ← target(job) が呼ばれない
```

`target` は `server.py:2304` の `lambda j: _run_job(j, req, principal, ticket)` で、
**チケットを解放する唯一の所持者**。したがってこの経路では `ticket.release()` が呼ばれない。

**再現**:
```
job.state: cancelled | target invoked: False
ticket released: False
```

`_JOB_QUEUE_LIMIT = max(2, max_concurrent_generations*4)` = **8**。
8 回で `POST /api/jobs` が**恒久的に 503**。サービス再起動まで回復しない。

同型のリークが `server.py:2303-2306` にもある。`jobs.start_worker` は
`thread.start()` が `RuntimeError`（スレッド枯渇時）を投げると例外が漏れるが、
そのとき `ticket.release()` は `except` 節（`server.py:2290-2293`）の外にあるため実行されず、
`Job` は `STATE_QUEUED` のまま二度と終端しない。

**修正**: チケットの解放を `_run_job` の**最初**ではなく、
`create_job` 側の `finally` に移す。
`jobs.start_worker` には「`target` を呼ばなかった場合に必ず，端末状態を確定する」責務を明示するのが筋。

---

### P0-3【新規】`enforce_song_allowlist` は**許可リストが空だと無防備になる**

`retro_radio/core/script_generator.py:336-338`:
```python
allowed = validate_song_pairs(songs)
if not allowed or not script:
    return script                       # ← 空リストなら原稿をそのまま返す
```

`_step_resolve_previews` は iTunes が 1 曲も返さなかったとき
`ctx.selected_pairs = []` にする（`retro_radio/server.py:1503`）。
`generate_radio_script` はその空リストを `enforce_song_allowlist(result, [])` に渡す（`:825`）。

**再現**:
```
enforce_song_allowlist('では「Gradle fur Alle」（Nena）をお聞きください。', [])
  -> 出力が入力とバイト単位で一致（True）
```

**これが起きるのは最も危険な瞬間**（音源が 1 曲も無い = 全部間奏になるとき）だけである。
`script_generator.py:197-210` の docstring が防止しようとしているのがまさにこの状態なのに、
ガードはそこで**完全に無効**になる。LLM が実在しない曲名を告げたまま間奏が流れる。

**修正**: `allowed` が空のときは**原稿を破棄して無音楽原稿へ落とす**
（Round 1 で `songs=[]` に対して行ったのと同じ方針）。

---

### P0-4【新規】許可リストの差し替えが**位置を見ず**、すでに鳴った曲に差し替える

`retro_radio/core/script_generator.py:351`:
```python
picked_title, picked_artist = allowed[replaced % len(allowed)]
```

round-robin で「許可リストの先頭から順に」取るだけ。`build_playlist` は
**トーク i → 曲 i+1** で対応する（`fallback.py:722-735`）ため、
許可リストがトーク数より短いと**すでに鳴ったオープニング曲を次の曲として告げる**。

**再現**（`allowed=[Alpha, Beta]`）:
```
トーク1 …それでは「Alpha」（A1）をお届けします。   ← オープニング曲そのもの
トーク2 …続いて「Beta」（A2）をお届けします。     ← すでに再生済みの 2 番目
トーク3 …では「Alpha」（A1）をお聞きください。    ← 鳴らない
```

`script_generator.py:331-334` の docstring 自身が「嘘を嘘に置き換える」ことを
認めつつ実装を続けている。Round 4 で直した「曲 i+1 対応」との関係で
**許可リスト自体を位置付きで渡す**必要がある。

---

### P0-5【新規】音源ゼロ/少数のとき原稿が「三つほどご用意しました」と約束したまま曲名を零個にする

音源 0 曲（`songs=[]`）の経路、`retro_radio/core/fallback.py:700, 717`:
```
本章では、{year}年のヒット曲と、当時のくらしの風景を三つほどご用意しました。
さて、ここからは皆様お待ちかねの音楽の時間でございます。
```
スロット 0 は `INTERMISSION_TITLE`・`preview_url: None`（`server.py:1272`, `:1123`）。

`generate_care_script` には**音源ゼロの分岐が無く**、`fallback.py:884, 886`:
```
大きな歓声が飛び交う会場ではなく、静かで温かなレコードの音色が…満たしてゆくようでございます。
いま鳴っている曲こそ、この番組のテーマでございます。
```
`generate_anniversary_script` は `fallback.py:946, 949`。949 行は
**一度も名前を出さなかった曲への宙に浮いた後方参照**。

また音源が 1〜2 曲だと、`fallback.py:777-779` の `_cue_song(pinned, 1..3)` が
`pinned[2..4]` を要求するため **曲名を一切出さずに終わる**のに、
`fallback.py:786` はまだ「三つほどご用意しました」と言う。

**修正**: 音源数に応じて約束文そのものを分岐させる
（0 曲 = 「usicなし」の原稿、1 曲 = 1 曲だけの原稿）。

---

### P0-6【新規】入場枠がスレッド数を制限していない（無制限スレッド増加）

`retro_radio/server.py:2183-2186`:
```python
if ticket is not None:
    ticket.release()          # ← スレッド生きたまま解放
if not _generation_slots.acquire(timeout=settings.generation_wait_timeout):
```

`server.py:255` のコメントは「枠は『生成中 + 待機中』をまとめて上から数える」と契約しているが、
実装は**待機前の数 µs で解放している**。`_job_queue_slots`（上限 8）が縛れるのは
`thread.start()` と新スレッドが走り出す間の時間だけ。

**シナリオ**: `max_concurrent_generations=2` のまま認証済みクライアントが 20 rps で `POST /api/jobs`。
各リクエストは 202 を返し、**30 秒何もせずブロックする daemon スレッドを 1 本作る**。
定常状態は約 600 スレッド。`fly.toml:106-109` は `cpus=1, memory_gb=512`。
OOM キル、または最悪 GIL 競合で全 API が応答不能。

**関連**: `jobs.py:829-836` の `_jobs` 退避は `j.is_finished` のみを見るため、
セマforo待ちのジョブは**永久に退避されない**。`Job._events`（`jobs.py:581`）は
`deque` でもなく上限なしの `list` で、`snapshot()` は全イベントを実体化する。
スレッド数とイベント数の両方が無制限。

---

### P0-7【新規・再現済】SSE の `last_event_id` で「完了済みジョブ」を 900 秒固定できる

`retro_radio/server.py:2353-2359` は `Last-Event-ID` ヘッダー **またはクエリ**から
上限なしの整数をそのまま受け取る。

`retro_radio/server.py:2399-2408`:
```python
if not pending:
    yield jobs.sse_comment()
    if time.monotonic() > deadline:      # SSE_MAX_SECONDS = 900
        ...
    continue
for event in pending:
    ...
    if job.is_finished:                  # ← pending が空だと永久に到達しない
        return
```

**再現**:
```
wait_for_events(after=9999) -> []  | elapsed 1.01 | is_finished True
```

完了済みジョブに `GET /api/jobs/{id}/events?last_event_id=9999` を送ると、
`wait_for_events` が常に空を返し、`is_finished` 判定に到達せず **900 秒間**接続を保持する。
1 接続 = 既定 executor の 1 スレッドも占有（`server.py:2396` の `run_in_executor(None, ...)`）。

`_SSE_MAX_CONNECTIONS = max(4, max_concurrent_generations*4)` = **8**。
同一テナントの認証済み利用者が 8 本送れば、テナント全体の進捗ストリームが 503 になる。

**修正**: ループ先頭で `if job.is_finished and not pending: return` を判定し、
`after > 最終 seq` の場合は即 終端（または 400）する。

---

### P0-8【新規】`single_user_key` に最小長がなく、Cookie 発行経路にレート制限もない

P0-1（Round 1 で修正）で `secret_key` に 32 文字の床を入れたが、
**もう一つの資格情報には何も無い**。

- `retro_radio/config.py:419-420` — `"bearer"` を返す条件は `bool(self.single_user_key)` のみ
- `retro_radio/config.py:361` — `auth_ready` も `bool(self.single_user_key)`
- `retro_radio/config.py:322-332` — `warn_insecure_secret_key` は `secret_key` しか見ない

`RETRO_RADIO_SINGLE_USER_KEY=x` でも警告 0 で起動する。

そして `retro_radio/server.py:2062-2069`（**未認証**の `POST /api/auth/session`）:
```python
else:
    presented = extract_bearer(request.headers.get("Authorization"))
    if not presented and payload is not None:
        presented = payload.token
    if not _verify_bearer_secret(presented, current_settings.single_user_key):
```
`_verify_bearer_secret`（`server.py:1964-1979`）は生の定数時間比較のみで**長さ検査なし**。
429 の—as ブロック（`server.py:2039-2051`）は `payload.email and payload.password` の**内側**にあるため、
この経路には一切_rate limit がない。

**シナリオ**: 運用者が 32 文字以上の `RETRO_RADIO_SECRET_KEY` を設定している（→ `require_auth_config()` は `"session"`）。
guess 可能な `RETRO_RADIO_SINGLE_USER_KEY` を 1 つ残す。攻撃者は
`POST /api/auth/session {"token": "<guess>"}` を**無制限に**叩き、当たれば
**署名済み 8 時間セッション Cookie** を|elevation| 8 時間 самого erhalten。
`GET /api/me/export`（他人の原稿開示）と `DELETE /api/me`（他人のデータ削除）に通る
（Round 1 の P0-1 と同一の被害面）。

**修正**: `single_user_key` にも `MIN_SECRET_KEY_LENGTH` 以上の床を課し、
この経路にも `LoginThrottle` を適用する。

---

## 2. P1

### P1-1 `DELETE /api/me` が TTS 音声キャッシュを消さない（派生個人データの残存）
`retro_radio/api/me.py:605` の docstring は手順 5 に
「テナントの TTS キャッシュを消す（そのテナントだけ）」と**明記**しているが、
`me.py:618-631` の本体にはその呼び出しが無い。
`TenantTtsCache.purge_tenant`（`retro_radio/services/tenant_cache.py:340`）の
**プロダクション側の呼び出し元は 0 件**（`tests/test_auth_wiring.py:517` のみ。Select-String で確認済み）。

削除請求後も、対象の愛称と生年を含む原稿が **gTTS で合成された音声**として
`<CACHE_DIR>/<tenant>/tts_<sha256>.mp3` に残り（`tts_cache_ttl_days` 既定 7 日）、
`GET /api/audio/{tenant}/{filename}` 経由で**同一テナントの任意の認証済みメンバー**に配信され続ける。
DB の行が残るのは既知CESSとして報告済みだが、これは**ファイルシステム上の派生物で削除経路が皆無**。

### P1-2 Stripe の webhook に冪等性・順序制御が無い → 解約後に永久プレミアムが復活する
- `retro_radio/billing/webhook.py:71-87` — `event.id` を保存も検査もしない
- `retro_radio/billing/webhook.py:166-169` — `user.plan = plan` は `status == "active"` のみの last-write-wins
- Stripe の配信は **at-least-once・順序無保証**（72 時間まで再送）

PRO 契約者が 12:00 に解約 → `deleted` が 12:01 に到着して FREE 化 →
11:00 発行の `subscription.updated`(active) が 12:05 に再送到着 → **PRO に戻る**。
降格する経路は二度と来ない。

**さらに**: `retro_radio/billing/stripe_client.py:59-72` は metadata を
**Checkout Session と Customer** にしか付けておらず、**生成された Subscription には付けない**
（`subscription_data={"metadata": ...}` が無く `client_reference_id` も無い）。
つまり Round 4 で追加した `_resolve_user_id` の**主経路（`metadata.user_id`）は
アプリが作った全てのサブスクで一度も発火しない**。帰属は customer 逆引きだけに依存する。

### P1-3 `customer.subscription.updated` の非 active 状態，降格も取り消しもしない
`retro_radio/billing/webhook.py:146-152` は `status != "active"` で
「plan unchanged」として return する。`past_due` / `unpaid` / `paused` / `incomplete` はすべて無視。
`invoice.payment_failed`（`webhook.py:213-220`）は `logger.error` 1 行のみ。
カードが拒否された PRO 利用者は、Stripe が dunning を終えるまで（数週間、あるいは永遠に）
`audio_quality="premium"` のまま。

### P1-4 メール一致での Stripe Customer 再利用 → 2 アカウントが 1 つの customer を共有
`retro_radio/billing/stripe_client.py:49-57` は `stripe.Customer.list(email=...)` の 1 件目を再利用する。
新規登録した 2 番目のユーザーが既存 customer を引き継ぎ、自分の `stripe_customer_id` を上書きする
（`retro_radio/db/repository.py:197-198`）。結果として 2 つの user 行が同じ `cus_…` を指す。
`find_by_stripe_customer`（`repository.py:183-190`）の `.first()` は**非決定的**に片方を返し、
**別のアカウントの**解約で降格される。再利用された customer の `metadata["user_id"]`
も更新されないので、最初のユーザーを指し続ける。

### P1-5 `GenerationRepository.get_by_id` に所有者の述語が無く、お気に入りに他人の原稿を読める
`retro_radio/db/repository.py:273-277`:
```python
model = self.db.query(GenerationModel).filter(GenerationModel.id == gen_id).first()
```
`FavoriteRepository.add`（`repository.py:304-317`）は `(user_id, generation_id)` を
**所有者を検証せずに**挿入する。`favorites` に owner を縛る FK はない
（`retro_radio/db/models.py:74-75`）。

利用 B が利用 A の `generation_id` をお気に入りに追加すると、A の原稿全文
（`repository.py:218` の `script`）、曲名、アーティスト名を読める。
**現状は潜在**（`FavoriteService` は `services/__init__.py:3` からの輸出のみで、
ルートに配線されていない）。配線された瞬間に P0。

### P1-6 `favorites` 行と `audit_logs` が削除請求を生き延びる
- `favorites` は `PURGE_TABLES`（`retro_radio/db/privacy_models.py:263-268`）に無く、
  `MusicProfileRepositoryImpl.delete_owner`（`privacy_repository.py:638-663`）も触らない。
- `audit_logs` は `privacy_repository.py:956-959` で「消さない」と**方針として明記**されているが、
  TTL も匿名化も無いまま `user_id`（=`users.id`）を保持し続け、
  削除請求のたびに `me.py:623-631` が**行を增加值**する。

### P1-7 全 3030 曲に `preview_url` が無く、停止すると「曲名は言うが一切鳴らない」番組になる
実測: `retro_radio/core/songs/songs.json` は 3030 件すべて
`confidence: "unverified"`、`preview_url` は **0 件**。すべての音源は実行時に iTunes へ取りに行く。

`retro_radio/server.py:1693-1699` は音源が無いスロットを間奏として埋める設計だが、
原稿は生成済み（`GENERATION_STEPS` は音源解決の**前**に原稿を作る:
`server.py:1752-1754`）。iTunes が 429/停止/回路遮断で落ちると、
**司会は 3 曲を名指しし、プレイヤーは間奏だけを出す**。
「鳴らない曲を紹介しない」という本プロジェクトの契約が、外部依存の 1 故障で破れる。

### P1-8 全年の原稿に実在する曲名「また明日遊ぼうね」がハードコードで入る
`retro_radio/core/fallback.py:708` と `:796`（フォールバック原稿・無音楽原稿）:
```
…近所の子どもたちが「また明日遊ぼうね」と元気に手を振り合いながら家路を急いでおりました。
```
「また明日遊ぼうね」は 1974 年の実在曲で**正本カタログにも収録済み**。
1950 年の原稿でもこの曲名を読み上げる（**その年は存在しない曲**）。
しかも `select_program_songs(year, 18)` の選曲集合には含まれないため、
`{t for t,_ in sel}` と原稿の「」集合の差は**常にこの 1 曲**になる。
装飾文として書かれているので、司会が曲カタログ由来でない曲名を口にしたことになる。

**修正**: この文を曲名引用 ditch に書き換える（`「また明日」` 等の時刻表現にする）。

### P1-9 facts の `valid_to: null` で、1950 年の原稿が 1925 年開始の番組と TV 番組を語る
`retro_radio/core/facts/__init__.py:313-319` は `valid_to is None` を
「この先ずっと有効」と解釈し、`programs_for_year`（`:323-325`）は `kind` で**フィルタしない**。

**実測**（`_program_sentence(1975)`）:
```
1970年代には、「NHKラジオ第一」（06:00 放送開始）、「料理教室」（11:30 放送開始） といった番組がありました。
```
- 「NHKラジオ第一」は `valid_from: 1925` — **1925 年開始の放送を「1970年代には」と語る**
- 「料理教室」「スター誕生」は `tv_program` レコード。**ラジオ**回想法台本の中で
  テレビ番組が紹介される

**新証拠**: ソースコード上は 1 ファイル（`scripts/song_source/songs.tsv`）由来の
3030 件で、`retro_radio/core/facts/programs.json` の 17 件だけがこの問題を持つ。
つまり**データの検証は `validate_facts.py` の warn として既に可視化されている**のに、
フィルタは未実装のまま。必要なのは**データ修正 + `kind` フィルタ**の両方。

### P1-10 `audit_coverage` は「生成イベント 1 件あたりのログ行数」を計算していない
`retro_radio/api/audit.py:169` は `coverage_ratio=audit_coverage(total, generation_events)` を渡す。
`total`（`audit.py:147`）は**テナント内の全アクション件数**、`audit_coverage`（`audit.py:80`）は
`(log_count, generation_count)`。同意/再生/エクスポート/認証イベントが混ざると比は恒久的に >1 になり、
**生成監査が壊れても低下しない**。削除 SLA の 24h 監視に使えない。

### P1-11 `X-Forwarded-Proto` を素朴に信頼して Cookie の `Secure` と HSTS を決める
`retro_radio/server.py:160-167` は `x-forwarded-proto` の先頭値が `https` なら True を返す。
`server.py:2103`（Cookie の `secure=`）と `:185`（HSTS）で使われる。
- TLS 終端プロキシが `X-Forwarded-Proto` を立てない構成では、全セッション Cookie が
  `Secure` 無しで平文送信される
- 平文リスナに対して任意のクライアントがヘッダを 1 つ送るだけで HSTS が返り、**HTTPS へ固定される**

同じファイルの `_client_ip`（`server.py:1944-1953`）は「利用者が自由に書けるため」
`X-Forwarded-For` を**明示的に拒否**している。同じファイル内で基準が逆。

### P1-12 削除請求の対象者が「anonymous」に畳まれる（個人モード）
`retro_radio/api/me.py:366, 397` は `principal.user_id or "anonymous"` を使う。
`RETRO_RADIO_REQUIRE_AUTH=0` では全利用者が 1 行を共有し、
`ConsentRepository.withdraw`（`privacy_repository.py:791-799`）は
`(user_id, terms_version)` の**全未撤回行を更新**するため、
**1 人の撤回が全員の同意記録を無効化する**。`history()`（`:825-832`）は他人の同意時刻を返す。

---

## 3. P2

| 場所 | 内容 |
|---|---|
| `server.py:2140-2143` | webhook の「二次防御」が `await request.body()` の**後**。`Transfer-Encoding: chunked`（content-length 無し）で無限にバッファされる。制限付きチャンク読みにすべき |
| `server.py:632-641` | 全エンドポイントにリクエストボディ上限が無い。`POST /api/auth/session` は**未認証**で body を丸ごとメモリに載せる |
| `api/audit.py:188-197` | `POST /api/audit` の `meta: Dict[str, Any]` にサイズ・深さ制限なし。`user_id` をボディから信頼 |
| `jobs.py:264-267` | `GenerationSlots.release()` が `super().release()` の**前**にカウンタを増やす。二重解放で `ValueError`、かつカウンタが壊れる。`generate_radio` の `finally` 内なので**元の例外をマスク**する |
| `song_store.py:269-275` | `_alloc_seqs` が `conn.commit()` を**呼ばない**。`connect()` の `finally: conn.close()` がロールバックする。現在 unreachable（定義のみ）なので罠 |
| `song_store.py:107-108` | `song_store_path` に URL を渡すと `Path(raw).name` に黙って降格する。PostgreSQL 構成で **split-brain** |
| `db/models.py:31,81` vs migration | `unique=True,index=True` と `UniqueConstraint` が**二重**。unique 制約と unique 索引が 1 組ずつ重複して存在する |
| `125758440996.py:33-34` | Postgres で `server_default=CURRENT_TIMESTAMP` が `timestamptz`→`timestamp` 変換され**セッション TZ のローカル時刻**が入る。`utcnow()` 前提（`db/models.py:22-25`）と矛盾し `ORDER BY created_at DESC` を壊す |
| `125758440996.py:69-74` | `downgrade()` がテーブルを消すだけで `DROP TYPE plantypeenum` をしない。Postgres で downgrade→upgrade が失敗する |
| `api/me.py:431-433` | 集計だけのために 5000 件分の**原稿全文**を materialize して `len()` を取る。`COUNT(*)` を使うべき |
| `favorite_service.py:103-114` | `_sleep` の docstring は「ワーカースレッドへ退避」と書いているが `ThreadPoolExecutor().submit().result()` は**呼び出しスレッドをブロック**する。`_retry_db_operation`（3 回×1 秒）と合わせて約 2 秒ブロック |
| `cache_service.py:26` | `cached` のキーに `tenant_id` 軸が無い。`user_id` のみを取る関数に他テナントの値がそのまま返る（現状 test のみで使用） |
| `billing_manager.py:54-56` | `_reset_monthly_counter_if_due` が**2 つ目**の `get_db()` セッションを開いて、呼び出し側が既にメモリに持つ値を再永続化 |
| `async_runner.py:15-19` | `get_executor()` の遅延初期化にロックが無い。2 スレッドが executor を作り 1 つが孤児になる。加えて `_build_generate_response` は完全同期なので `EXECUTOR_MAX_WORKERS=8` はサーバ経路では死んでいる |
| `core/song_selector.py:114` | `fresh_other = [pair for pair in fresh if pair not in fresh_target]` は dict の `==` による O(n²)。正本カタログに重複が無いため今のところ無害（songs.json に完全一致・title/artist 重複とも 0 件を実測）だが、候補が外部 dict のときは**内容一致の別レコードが落ちる** |
| `utils/i18n.py` / `locales/` | README:321 が「どこからも import されていない」と自認している。死コード |
| `webhook.py:171-183` | `_resolve_user_id` が customer 逆引きで「逆引きしました」をログに出すが、`_handle_subscription_updated` は続く `coerce_plan(_field(subscription_metadata,"plan"))` が None になり即 return する。**逆引きはこの経路では無効**で、ログだけが誤解を招く |
| `core/music_profile.py:328-331, 348-350` | `select_by_profile` は 36 曲静态マスターに委譲する**独立した 2 つ目の選曲器**。参照元は `tests/test_music_profile.py` のみで、住人の嗜好プロファイルは実際の番組構成に一切影響しない。「1 本の事実源」の主張は経路が死んでいるから成立している |
| `core/script_generator.py:396` | `_catalog_allowlist` は `Random(int(year))` でシードするため `settings.rng_seed` を無視する。seed が 3 つ（`rng_seed` / `year` / 履歴ストア）に分裂しており、eval が測る許可リストは本番の許可リストではない |
| `core/rng.py:36-39` | `rng_seed=None`（既定、`config.py:175`）のとき `module_rng()` は**グローバル `random` モジュール**を返す。`SongSelector._rng.shuffle` が `ThreadPoolExecutor` 全体で共有状態を書き換える。モジュール直下の `random.*` 呼び出しは 0 件になったが、**並行決定性**は得られていない |
| `server.py:1685-1698` | 窓が足りないとき `elsewhere`（他パスの曲）で埋める。曲の総数が少ないと**パス 1 がパス 0 とバイト単位で同一**になり、1 回の放送で同じ曲が 2 回流れる（`_step_playlist:1648` の docstring が禁じている振る舞い） |
| `server.py:1632-1640` | `ctx.enriched` が空のとき `preview_url: None` にもかかわらず**実在する曲名**（「神田川」等）を `GenerateResponse.songs[0]` に入れる。`_to_song_dicts` を経由しないので `INTERMISSION_TITLE` 契約（`:1123`）を迂回する。またリクエスト経路に残る最後の `module_rng()` 経路なので、`rng_seed` 未設定だと**表示曲名が要求ごとに変わる** |
| `utils/text_cleaner.py:45-47` | `^[ \t]*主要ニュース.*$` / `主要な出来事` / `くらしの風景` の行丸ごと削除。`「主要ニュース: これは短いbodyです。」` や `「くらしの風景は変わりましたが、人々はそれを求めていました。」` という**普通の日本語文**が丸ごと消える。`:181-184` の docstring が「本文を決して失わない」と宣言している内容と矛盾 |
| `core/fallback.py:863` | `f"{decade}年代には、{joined} といった番組がありました。"` に `）` 後の**不要な空白**が入る。TTS が「、.Reader」と読む可能性 |

## 4. P3

- `server.py:2595` — `/health` は `current_settings`（注入）に直したのに、
  `payload["auth_enforced"] = _auth_enforced()`（`server.py:313-326`）だけ
  環境変数→モジュールグローバル `settings` を読む。R3-04 の修正が**半分だけ**適用されている
- `plans.py:31,39,48` — `monthly_generations` が FREE/PREMIUM/PRO すべて `UNLIMITED`。
  加えて `check_generation_limit` は `server.py` から一度も呼ばれていない（`tests/test_billing.py:104` のみ）
- `privacy_repository.py:886,887` — `action` / `resource_type` を `[:64]` で**黙って切り詰める**。
  64 文字接頭辞を共有する別アクションが監査ログ上で区別できなくなる。
  `resource_id`（`:876-881`）は敢えて切り詰めないという方針と矛盾
- `jobs.py:587`（前後）— `job.client_gone`（`server.py:2392,2414`）は 2 経路で set されるだけで**読み出し元が無い**。
  「切断したらジョブも止まる」ように見えるが実際は止まらない（`server.py:2413` が明記）
- `server.py:800-817` — `_tts_last_call_at` / `_tts_gate` がプロセスグローバル。
  1 テナントの gTTS スロットリングが全テナントの TTS を律速する
- `server.py:231-233` — `_is_mp3_header` の ID3 枝と MPEG 同期枝の受理接頭辞が互いに素で**到達不能**
- `README.md:321,423` が `design_tokens/` と `styles/generated.css` をPPER ガードしている
  （`tests/test_ui_ux.py:1104-1124`）が、フロントは実際には読み込んでいない
- `docs/final_review_report_2026-10-01.md` の見出しは「修正した実バグ: 18 件」だが、
  節タイトルは「17 件」。`## 3` と小見出し `### 3.` も番号が重複

---

## 5. 「緑だが何も検査していない」テスト

AST で 93 ファイル・1697 関数を実測した結果。

| 指標 | 件数 |
|---|---|
| `assert` / `pytest.raises` / mock 検査が**ゼロ**のテスト関数 | **23**（15 ファイル） |
| `read_text()` / `inspect.getsource()` で**実装のソース文字列**を検査するテスト | **50**（15 ファイル） |
| `assert True` プレースホルダー | 0（Round 1 で解消済み） |
| `strict=False` の xfail | 3 |

### 5-1【P0】CORS の安全境界テストが**何も assert していない**
`tests/test_server_regression.py:874-887`:
```python
try:
    Settings(cors_origins=["*"], cors_allow_credentials=True)
except Exception:
    return          # ← 必ずここに落ちる
# 拒否されない場合も、起動できない・ヘッダが欠落しないことは別テストで確認済み
```
`retro_radio/config.py:304` の `reject_cors_wildcard_with_credentials` が確かに拒否するので
**常に `return`** する。**バリデータ全体を削除してもこのテストは緑のまま。**
40 行の docstring が約束する「安全境界」は一度も評価されていない。

### 5-2【P0】eval ゲートは**フォールバック生成器だけを**測り、曲照合は**空虚**
3 つの独立した穴が重なっている。

**(a) 本番経路を一度も通らない**
`eval/fixtures.py:55` `DEFAULT_SCRIPT_SOURCE = "deterministic"`、
`eval/fixtures.py:158-177` は `_catalog_allowlist` + `_deterministic_script` を**直接**呼ぶ。
`--offline`（`eval/metrics/__init__.py:538`）がこの source を強制するため、
`generate_radio_script`（＝ Gemini 経路）は eval で一度も実行されない。
`_song_allowance_block` / `enforce_song_allowlist` / `validate_song_pairs` /
`parse_script_segments` のマーカー除去 — **P0-3 と P0-4 はすべてこの経路の中にある**。

**(b) 曲照合の照合先が「カタログ全体」**
`eval/metrics/__init__.py:401`:
```python
checklist=run_checklist(text, year),          # allowed_song_titles を渡していない
```
`eval/metrics/songs.py:174-178`:
```python
def _resolve_allowed(allowed_titles):
    if allowed_titles is None:
        return set(_KNOWN_SONG_TITLES), "catalog"     # ← 3030 件すべて
```
つまり**正本カタログにある曲なら、プレイリストに 1 曲も含まれなくても「一致」**になる。
`song_match_rate` は**カタログに無い捏造曲目でしか失敗しない**。
「原稿が名ざした曲 ⊆ 実際に流れる曲」という契約は** gate として存在しない**。
（対照的に `fact_score` だけは `selection_window_titles(year)` を渡していて正しい。
この非対称が「fact 100.0 / song 100.0」の両方が空虚な理由。）

**(c) 自己参照**
被測定原稿は `_catalog_allowlist`（正本カタログ）で作られ、評価は `build_fact_table`
（同じ facts レジストリ）で行われる。**自己参照**であり「平均 100.0」は tautology。
`eval/fixtures.py:175` は常に**空でない** `_catalog_allowlist(year, 18)` を渡すので、
P0-5 の「音源 0〜2 曲」の経路は**一度も生成されない**。

`.github/workflows/ci.yml:76-79` の「台本生成のリグレッションは このステップで赤くなる」は成立しない。
**この eval ゲートは、今見つかっている P0-1〜P0-5 をすべて通す。**

### 5-3【P0】`/health` のフロント「契約テスト」が**手書きの契約**を固定している
`tests/test_frontend_playback_auth.py:254-256`（および `:266, :286, :305, :337`）は
`auth_ready: True` / `auth_mode: "anonymous"` を**含んだ payload** をリテラルで渡している。
- このファイルから `GET /health` を呼ぶテストは**1 件も無い**
- `retro_radio/server.py:2602` は匿名に対して `auth_mode` を**返さない**（キー自体が無い）。
  つまり**サーバーが決して返さない値**を検証している
- `auth_enforced` を削除しても `auth_ready` を削除しても全 assertion が緑のまま

### 5-4【P1】ステップ順序と TTS レート制限を grep で検証
- `tests/test_no_audio_gaps.py:518-526` — `server.py` を `read_text()` して
  `GENERATION_STEPS = (` のブロックを切り出し、`_step_resolve_previews` と
  `_step_generate_script` の**文字列位置**を比較
- `tests/test_tts_rate_limit.py:48` — `"def _tts_is_network_client" in <server.py のテキスト>`
  関数名を grep するだけ。**呼ばれていない関数でも緑**

どちらも無害なリネームで落ち、何もしない実装で通る。

### 5-5【P1】アサーションのないリーク検出テスト
`tests/test_round4_regression.py:127-139` は 4 枠を取って戻し、1 枠追加で
`if ticket is not None: ticket.release()` のみ。**assert が無い**。
セマフォがリークしても**検出しない**（P0-2 のリークもこのテストでは捕まらない）。

### 5-6【P1】UTM の WCAG AA 契約が `pass`
`tests/test_retro_theme.py:123-130` — docstring は「WCAG AA コントラストを満たすこと」と
書いて body は `pass`。`assert True` ガード（`test_hygiene_regression.py:1257`）も `pass` を捕まらない。

### 5-7【P1】import 時環境変数汚染で DB 分離が壊れる
`tests/test_db_session.py:8`:
```python
os.environ['RETRO_RADIO_DATABASE_URL'] = 'sqlite:///:memory:'
```
`tests/conftest.py:43` の設定を**復元せず**上書きする。
`get_settings()` は `@lru_cache()`（`retro_radio/config.py:431`）、
`get_engine()` は `@lru_cache(maxsize=1)`（`retro_radio/db/session.py:28`）で
両方とも**収集中に**最初に評価される。1 回の実行全体で in-memory DB を共有する。
`conftest.py:303-309` 自身が「一部のテストが `drop_all` する」と記録している。

### 5-8【P2】CI の穴
- `validate_songs.py` / `validate_facts.py` を**呼ぶ CI ステップが存在しない**
  （`.github/workflows/` に `scripts/` を呼ぶ行なし）。`ci.yml:86` が
  「カタログの充足性は `scripts/validate_songs.py` に委ねる」と明記しているが、
  これを呼ぶものは無い。かつ `eval/metrics/fact_score.py:59` は
  `scripts.validate_facts` を import している = **ゲートが未検証ディレクトリに依存**
- lint は 2/5 経路のみ（`ci.yml:52,60` は `tests` と `retro_radio`）。
  `scripts/` `db/` `utils/` は CI で lint されず、`eval/` は CI でも pre-commit でも対象外
- mypy が CI に無い（`requirements-dev.txt:26` にあるだけ）。pre-commit は `v1.5.0` に pin
- `network` マーカー 3 件が恒久 deselect、nightly ジョブが無い
- `requirements-dev.txt` に使われていない pin: `pytest-mock`（`mocker` 0 件）、
  `types-pytz`（`pytz` 自体が無い）、`codecov`（CI は action を使う）
- `tests/test_frontend_playback.py:22` 等の `pytest.importorskip("dukpy"/"esprima")` は
  wheel のビルドに失敗すると **app.js の振舞いテストが全部黙って消える**（CI への信号なし）
- `tests/test_infra_hardening.py:382` が pytest の中から
  `python -m eval --offline --threshold 80`（timeout 600 秒）を再実行 = **ゲートが 1 ジョブで 2 回走る**

---

## 6. デプロイ設定の不整合

| 場所 | 内容 |
|---|---|
| `Dockerfile:73` vs `render.yaml:17` / `railway.json:8` | HEALTHCHECK が `8501` 固定、両者は `--port $PORT`。Render 既定 10000 で**コンテナが unhealthy 判定**される |
| `railway.json:7-16` | `RETRO_RADIO_*` を**1 つも**設定していない。`database_url` が既定 SQLite になり `/data` volume が空、`require_auth` 既定 True + `secret_key` 空で**保護 API が全部 503** |
| `fly.toml:27` vs `fly.toml:137` | `[env] PORT = "8501"` は完全な死設定（起動コマンドが `--port 8501` をハードコード） |
| `server.py:632-641` | ミドルウェアは CORS とヘッダのみ。Reverse proxy 側が本体サイズ制限をする前提だが、`render.yaml` / `railway.json` にその設定が無い |

---

## 7. 着手順（推奨）

**Phase A — 利用者が嘘を聞く状態（利用者を社会经济的に保護する最優先）**

1. **P0-3 + P0-4 + P0-5**（許可リストが空で無防備／位置を見ない差し替え／音源ゼロの偽の約束）
   — いずれも `core/script_generator.py` と `core/fallback.py` の局所修正。
   許可リストを「位置付き」に変え、空のときは原稿を無音楽原稿へ落とす。
   併せて P1-7（音源ゼロでも曲名を出さない）の分岐をreuse する。
2. **P0-1**（周回間の曲/原稿ズレ）— 、利用者が実際に耳にする内容的にもっとも直接的な影響。
   ただし (a)(b)(c) の仕様判断が先。`plans/ui_ux_contract.md` と相談。
3. **5-2(b)**（eval の曲照合を空虚にする `allowed_song_titles` 未指定）
   — 1 行の修正で、以降の全 P0 が eval で赤くなるようにする。**最初にやるべき 1 行**。

**Phase B — 可用性（テナント全体が止まるもの）**

4. **P0-2**（チケットの所有者）+ **P0-6**（`_job_queue_slots` が待機を数えない）
   — チケット解放を `create_job` の `finally` へ移し、入場枠を「待機中も数える」契約に戻す。
   `_jobs` の退避条件（`jobs.py:829-836`）も `is_finished` を見ない问题的。
5. **P0-7**（SSE `last_event_id`）— ループ先頭で終端判定。1 行。
6. **P0-8**（`single_user_key` の長さ＋レート制限）— Round 1 の P0-1 と同じ形。
7. **P1-1 / P1-6**（削除請求の到達範囲）— `purge_tenant` を `me.py` に配線し、
   `favorites` を `PURGE_TABLES` に入れる。
8. **P1-2 / P1-3 / P1-4**（Stripe）— `subscription_data.metadata` を付け、
   `event.id` を記録して冪等化、`past_due/unpaid` で降格、customer の再利用を止める。

**Phase C — 検査の穴（緑の意味を回復する）**

9. **5-1**（CORS テストに assert）— 1 テスト
10. **5-3**（`/health` 契約テストを実 HTTP 呼び出しに）— `/health` "Do not return" を固定する形
11. **5-4 / 5-5 / 5-6**（grep テストと assert 無しテストを実挙動に置換）
12. **5-8**（`validate_songs` / `validate_facts` を CI に追加、lint 対象を 5 経路に揃える、mypy）

**Phase D — データと運用**

13. **P1-9**（facts の `valid_to` と `tv_program` フィルタ）、**P1-8**（「また明日遊ぼうね」）、
    **P1-10**（`audit_coverage`）、**P1-11**（`X-Forwarded-Proto`）
14. **第 6 節**（`Dockerfile` HEALTHCHECK、`railway.json` の env、`fly.toml` の死設定）

---

## 8. 做得よかった点（潰さないこと）

- `.env` は git 管理外（`.gitignore:2`、`git ls-files` に無い）。テストも本番 DB を触らない
  （`conftest.py:40-51` が `%TEMP%` に退避、`.gitignore:56` が `*.db`）
- フロントに **innerHTML シンクは 1 箇所のみ**（`static/app.js:1915`）で、
  `escapeHtml` 契約の背後。`insertAdjacentHTML` / `outerHTML` / `document.write` / `eval` / `new Function` は無い
- トークンを `localStorage` に保存していない。認証は Cookie ベースで、
  全 fetch が `credentials: 'same-origin'`
- `_auth_enforced()` と `Settings.require_auth` の**逆向き矛盾**を fail-closed に落とした
  設計（`server.py:340-352`）は正しい。`_auth_enforced` のような「環境変数と `Settings` が
  別の真実を指す」問題は Decision 済み
- Round 4 で入れた `tts_cache_max_files` / `rng_seed` は、外部 API 依存を局所化する
  方向で正しく機能している
- モジュール直下の `random.*` 呼び出しは 0 件になった（grep で確認）。
  グローバル状態は `core/rng.py` の 1 か所に集約されている

---

## 9. 検証方法

本レビューの P0 は、以下の**実行可能な再現**で確証を採った（推測ではない）:

```
# P0-1 周回間のズレ
python -c "from retro_radio.server import build_playlist; ..."
  → pass1/pass2 でトーク本文が同一・曲が B0-B3 に変わることを確認

# P0-2 チケットのリーク
python -c "import retro_radio.jobs as J; job.request_cancel(); J.start_worker(job, target)"
  → job.state=cancelled / target invoked=False / ticket released=False

# P0-3 許可リストが空だと無防備
enforce_song_allowlist(text_with_invented_song, []) → 入力とバイト単位で一致

# P0-7 SSE の last_event_id
job.wait_for_events(9999, 1.0) → [] が恒久返る（is_finished=True でも）
```

残りはコード経路の読解（file:line 付き）で、P0 については
5 領域のサブエージェント報告と**主担当の再読解が一致したものだけ**を採用した。

---

## 10. ライブデバッグ（実機確認 — uvicorn 起動 + HTTP 実測）

手順: 一時 SQLite に `alembic upgrade head` を適用 → `uvicorn retro_radio.server:app --port 8511`
を起動 → 全 21 ルートを走査 → 5 年分の `/api/generate` を実行 → ジョブ/SSE を stressing。
（本番 DB・本番 `.env` は未変更。一時ファイルは `%TEMP%` 配下。）

### 10-1【P0・新規】fresh clone で `run_retro_radio.bat` が起動失敗する

```
$ uvicorn retro_radio.server:app --port 8511
RuntimeError: アプリケーションの DB スキーマが未準備です。
  `alembic upgrade head`（または `python scripts/init_db.py`）を先に実行してください。
ERROR: Application startup failed. Exiting.
```

これは `server.py:617` の意図的な fail-fast 自体は正しい設計
（README:132-146 に理由が明記され、`scripts/init_db.py` も存在する）。
**問題は起動手段の側**:

| 起動手段 | マイグレーション | 結果 |
|---|---|---|
| README 方式 B（`README.md:133`） | `alembic upgrade head` あり | OK |
| `Dockerfile:77` / `fly.toml:137` / `render.yaml:17` / `railway.json:8` | CMD にあり | OK |
| **README 方式 A（`run_retro_radio.bat`）** | **なし** | **起動失敗** |
| **`debug.bat`** | **なし** | **起動失敗** |

`.gitignore:56` が `*.db` を除外するため fresh clone には DB が無い。
方式 A は README:118 で最初に来る手順で、`run_retro_radio.bat` は 3 秒後に
`start http://localhost:8501` を実行するため、**接続拒否のブラウザを開くだけ**で終わる。
`.bat` 側に起動失敗時の案内が無い。

**修正**: `run_retro_radio.bat` / `debug.bat` の uvicorn 起動直前に
`python scripts/init_db.py`（失敗したら非ゼロで）を 1 行挿入する。

### 10-2【P0・新規】可聴曲を 18 スロット中 6〜14 曲しか用意できておらず、1950 年は**各曲を 3 回再生**する

実測（`/api/generate`、`loop_count=3` → 1 パス 6 曲 × 3 パス = 18 スロット）:

```
year  可聴/必要  distinct  スロット  pass0==pass2  原稿が名指しして実際は流れない曲
1950  18/18      6         18      True         また明日遊ぼうね
1964  18/18      11        18      True         また明日遊ぼうね
1975  18/18      9         18      True         また明日遊ぼうね
1995  18/18      9         18      True         また明日遊ぼうね
2025  18/18      14        18      False        また明日遊ぼうね
```

1950 年の実測内容:
```
pass0: [いい日旅立ち, 幸せの時, 恋のあやとり, おやすみなさい, 花ことばの詩, 都忘れ]
pass1: [内気なあいつ, 木綿のハンカチーフ, いい日旅立ち, 幸せの時, 恋のあやとり, おやすみなさい]
pass2: [いい日旅立ち, 幸せの時, 恋のあやとり, おやすみなさい, 花ことばの詩, 都忘れ]   ← pass0 と同一

total song slots: 18 / distinct: 6
```

**根因（`retro_radio/server.py:1690-1710`）**: `_step_build_passes` の重複排除は
`chunk_keys` で**パス内だけ**。パス間（pass0 と pass2 が同じ 6 曲）は排他になっていない。
`ctx.enriched` には 18 曲分を要求したが、iTunes 解決が 6〜14 曲しか当たらなかった時点で
`server.py:1685-1698` の `elsewhere` フォールバックが「他パスの曲」を经商して埋める。

サーバは**この事実を知っている**（ログは `INFO`）:
```
INFO  音源を取れた曲だけを原稿へ告知します: 9 / 18 曲
INFO  1 パスの 6 スロットを曲で埋められませんでした（採用 6 曲）
```
**でも节目構造を矮小化する分岐が 1 つも無い**。警告して、そのまま 3 周分の枠を同じ曲で埋める。

**これが禁じられていることはコード自身が明記している**:
- `retro_radio/config.py:150-155` — 「曲数が足りないとフロントが同じ曲を繰り回し、
  1 パス内で同じ曲が 2 度流れることになるため、6 曲配信を既定にする」
- `retro_radio/server.py:1648` — 「**1 パス内で同じ曲を 2 回流さない**（書順が最優先）」

**修正**: (1) `_step_build_passes` に**パス間の使用済み集合**を入れる、
(2) `len(playable) < planned_slots` のとき `loop_count` を下げて节目幅を狭める（無音より短い回数が誠実）、
(3) どちらでも足りないなら**生成前に警告を返す**。

### 10-3【P0・実証】P0-1（周回間の曲/原稿ズレ）を HTTP 経路で確認

1975 年の実出力が、本番の台本とパスである:

```
原稿 seg[1]: …それでは、この年のヒット曲、「恋のあやとり」（麻丘めぐみ）をお届けいたします。
原稿 seg[2]: …懐かしい一曲、「おやすみなさい」（木之内みどり）もお届けいたします。
原稿 seg[3]: …この年のもう一曲、「花ことばの詩」（ドド）をどうぞお聞きください。

pass0: [..., 恋のあやとり, おやすみなさい, 花ことばの詩, 都忘れ]   ← 告知と一致
pass1: [内気なあいつ, 木綿のハンカチーフ, いい日旅立ち, 幸せの時, 恋のあやとり, おやすみなさい]
```

pass0 では 3 つとも整合するが、**pass1 は 1 つもanamatch しない**
（「恋のあやとり」は pass1 の 6 スロット中に無い）。
かつ原稿の TTS 音声は**全パスで同一ファイル**なので、
2 周目・3 周目では**同一の音声で 3 つの曲名を告げたまま、別の曲または間奏が鳴る**。

### 10-4【P0・実証】P0-7（SSE `last_event_id` による スロット枯渇）を HTTP で実証

完了済みジョブに対して 8 本の `GET /api/jobs/{id}/events?last_event_id=9999` を張った結果:

```
stream: ('open', 503)                      ← 9 本目は拒否
stream: ('timeout-after-12s', 'STILL OPEN (pinned)')   × 8

during saturation, normal SSE request -> 503
```

**8 本の「何もしない」接続で、正当な進捗 SSE 要求が 503 になった。**
同じテナントの認証済み利用者 1 人で再現する。`_SSE_MAX_CONNECTIONS = 8`。

### 10-5【P1・実証】P1-8（「また明日遊ぼうね」）は 5 年すべてで発生する

```
mentioned: ['恋のあやとり', 'また明日遊ぼうね', 'おやすみなさい', '花ことばの詩']
mentioned ∩ pass0 = ['恋のあやとり', 'おやすみなさい', '花ことばの詩']
mentioned \ pass0 = ['また明日遊ぼうね']
```

`retro_radio/core/fallback.py:708` の装飾文が**実在曲名を常に口にする**。
1950 年原稿でも「また明日遊ぼうね」（1974 年発売）这样说う。
Catalog（3030 曲）に入っている曲名なので、`extract_song_mentions` は
**合奏曲として正しく抽出する**一方、`song_match_rate` は
照合先がカタログ全体（`eval/metrics/songs.py:177`）なので**不一致にならない**。
2 つの評価器が同じ不一致を別々に「正常」と判定している。

### 10-6 実機では正常だったもの（潰さなくてよい — 再現済み）

| 項目 | 結果 |
|---|---|
| 全 21 ルート | 404 / 403 / 409 / 413 はいずれも仕様どおり（`/api/me/export` は REQUIRE_AUTH=0 で 409、`/api/admin/audit*` は member で 403、2MB webhook は 413） |
| フロント資産 | `/static/app.css` / `app.js` / `manifest.json` すべて 200、欠落 0 |
| フロントの API パス | 参照 12 件すべて実在ルートに一致（不一致 0） |
| セキュリティヘッダ | CSP / X-Frame-Options: DENY / X-Content-Type-Options: nosniff / Referrer-Policy 付与済み。HSTS は平文 HTTP では出ない（設計どおり） |
| TTS 出力 | `audio/mpeg` 2,013,696 bytes / magic `\xff\xf3\x84` = 実 MP3 |
| iTunes プレビュー | `audio/x-m4p` 1.1MB。CSP `media-src` に `audio-ssl.itunes.apple.com` を含むため再生可能 |
| ジョブ取消 | 3 件 create→cancel で**全件が即 `cancelled` に到達**。zombie スレッド 0 |
| チケットリーク（P0-2） | HTTP 経路では**到達不能**（job_id は `start_worker` 後にしか公開されない）。潜在バグのままである |
| ログ | 5 年分の生成で **ERROR / 5xx は 0 件**。TTS キャッシュは同一ハッシュを再利用（`tts_7075853c...`） |

### 10-7 実機から見える追加の運用指標（ゲートに無い）

- **生成 1 回の実測 33〜56 秒**（`estimated_ms` は 78,500 ms と 166,852 ms を返した。前者が過大推定）
- **iTunes 解決成功率 約 44%**（1975 年で 18 曲中 8 曲）。
  これが 10-2 の直接原因であり、**どのテストも eval ゲートも測っていない**
- **`/health` は Gemini キー無しで `status: "degraded"`** を返す。
  フロントは `hasKey = api_key_configured !== false && status !== 'degraded'` で判定するため、
キー無しの環境では常に「キーなし」扱いになる。意図通りだが、
  **施設運営の立場では LLM 経路が生きているかをこの 1 フィールドで判断できない**
