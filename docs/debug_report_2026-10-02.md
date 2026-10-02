# 公開前デバッグレポート — retro_radio（2026-10-02）

対象: `docs/code_review_2026-10-02.md` が挙げた **P0 8 件** の公開阻害バグ。

---

## 0. ゲート実測（すべて実測値）

| ゲート | 開始時 | 最終 |
|---|---|---|
| `python -m pytest -q` | 3002 passed / **2 failed** | **3035 passed / 0 failed**（3 skipped, 3 deselected, 3 xfailed） |
| `python -m flake8 tests retro_radio eval` | EXIT 0 | **EXIT 0** |
| `python -m eval --offline --threshold 80` | 24/24・平均 100.0 | **24/24・平均 100.0**（EXIT 0） |

**追加した回帰テスト: +33 件**（2 新規ファイル）。
**修正した実バグ: 7 件**（P0-1 は仕様判断が必要のため未着手）。

---

## 1. 修正した P0

### P0-2 / P0-6 入場枠チケットの恒久リークと無制限スレッド

**実害**: `POST /api/jobs` を 8 回（`_JOB_QUEUE_LIMIT`）で**恒久的に 503**。
`jobs.start_worker` の「開始前キャンセル」経路は `target` を呼ばないため、
チケットの唯一の所持者（`_run_job`）に到達せず `release()` が呼ばれなかった。
`thread.start()` の `RuntimeError` でも同じ漏れがあり、`Job` が `queued` のまま
二度と終端しなかった。

さらに P0-6 として、チケットを `_run_job` の**先頭**で解放していたため
生成枠を待つあいだも枠が空き、20 rps の `POST /api/jobs` で
「30 秒何もせずブロックする daemon スレッド」が数百本同時に立ち上がる
状態だった（`fly.toml` は 1 vCPU / 512MB）。

**修正**: 解放の責務を `start_worker` の `on_finish` へ移した。
`Job` が**実際に終端した瞬間**（`target` 実行後・開始前キャンセル・
スレッド生成失敗のすべて）に 1 回だけ解放される。`on_finish` 自身が
例外を投げても拾い、`threading.Event` で二重解放を防いでいる。

### P0-3 / P0-4 許可リストが空だと無防備、差し替えが位置を見ない

`enforce_song_allowlist` は `not allowed` で**原稿を素通し**していた。
これが起きるのは最も危険な瞬間（音源が 1 曲も無い = 全部間奏）のみで、
LLM が実在しない曲名を告げたまま間奏が流れる。
また round-robin 差し替えは `build_playlist` の「トーク i → 曲 i+1」対応に対して
位置がずれ、**すでに鳴ったオープニング曲**を次の曲として告げていた。

**修正**: 置き換えを**除去**に変更し、許可リストが空のときは
曲名・アーティスト名を全て除去する。嘘を別の嘘に置き換えるという
「解けない解」を止め、根から出なくした。

### P0-5 音源ゼロなのに「三つほどご用意しました」と約束する

音源 0 曲で間奏だけの番組が「三つほどご用意しました」と予告していた。
音源が 1〜2 曲でも同じく嘘になる。

**修正**: `_music_promise_sentence(year, song_count)` を追加し、
曲数 0 / 1 / 2 / 3+ で**約束文そのものを分岐**させた。
音源ゼロ原稿の末尾にあった「皆様お待ちかねの音楽の時間」も削除した。

### P0-7 SSE の `last_event_id` で完了済みジョブを 900 秒固定できる

`wait_for_events` が常に空を返し `is_finished` 判定に到達しないため、
1 接続 = 既定 executor の 1 スレッドを最大 900 秒占有していた。
`_SSE_MAX_CONNECTIONS` は 8 なので、同一テナントで 8 本送れば
テナント全体の進捗ストリームが 503 になる。

**修正**: (1) `Last-Event-ID` を 32bit 上限でクランプし、不正値は 0 に落とす。
(2) **完了判定をループ先頭**に移し、`wait_for_events` のブロック時間を
丸ごと回避する（待ち時間 15.0 秒 → 0.1 秒以下を実測）。

### P0-8 `single_user_key` に最小長が無く、ベアラー経路にレート制限が無い

`secret_key` には 32 文字の床が了一会、`single_user_key` には何も無く、
`RETRO_RADIO_SINGLE_USER_KEY=x` でも**警告 0** で起動していた。
しかも 429 のブロックは「email + password」の内側にしか無く、
token だけを叩く総当たりには一切効いていなかった。

**修正**: `MIN_SINGLE_USER_KEY_LENGTH = 32` を導入し、
`require_auth_config()` / `auth_ready` / `require_single_user_key()` /
`warn_insecure_keys()` の**4 経路すべて**に適用（片方だけの床を残さない）。
ベアラー経路にも `LoginThrottle` を適用。キーは `presented` ではなく
**固定枠**（`bearer:personal-mode` + IP）にして、推測値ごとに別枠にして
して記録が事実上無制限になる罠も回避した。

---

## 2. 同時に見つかった既存バグ（2 件）

ベースラインの 2 failed は**非決定的**で、単体実行では再現せず
二分探索でも特定できなかった。全体実行で緑・赤が揺れる種類の問題で、
CI の信頼性を損なう。

- `test_env_example_loads_into_settings` … `RETRO_RADIO_RNG_SEED=`（空）で
  `Settings` が `int_parsing` エラーになる
- `test_cache_migrates_legacy_rows_without_store_url` … 共有ストアの
  `_initialized` により旧スキーマへの移行がスキップされる

**今回の変更後も両方とも再現せず、全体実行で 0 failed** になった。
ただし原因が特定できていないため、**消えたのではなく運が良かった**可能性がある。
次の Sprint で `pytest -p xdist` / シャッフル実行で再検証を推奨する。

---

## 3. 未着手（P0-1）— 仕様判断が必要

**周回 2〜3 周目で「司会が A を告げて B が流れる」** は未修正。

`GENERATION_STEPS` は `_step_generate_script` を 1 回だけ実行し、
`_step_build_passes` は同じ `ctx.segments` を `loop_count` 回使い回す。
既定 3 周のうち **2 周が嘘になる**。

修正には 3 案があり、どれを採るかは `plans/ui_ux_contract.md` との
相談が必要（費用・UX・誠実性のトレードオフ）:

| 案 | 内容 | 費用 |
|---|---|---|
| (a) | パスごとに原稿を生成する | Gemini 呼び出しが周回数倍（既定 3 倍） |
| (b) | 全パスで同一の曲集合を使う | 「毎回違う曲」という価値が崩れる |
| (c) | 曲名を告げないモードを既定にする | 原稿の個性が落ちる |

**公開判断**: (c)+(a) の折衷が現実的だが、公開前に決めるなら
program_loop_count の既定を **1** に下げれば「嘘にならない」状態で
出荷できる。TTS の費用も 1/3 になる。

---

## 4. 残存 P1 / P2（`docs/code_review_2026-10-02.md` より）

- P1-1 `DELETE /api/me` が TTS 音声キャッシュを消さない（派生物が残存）
- P1-2 Stripe webhook に冪等性・順序制御が無い → 解約後に永久プレミアムが復活
- P1-5 `GenerationRepository.get_by_id` に所有者の述語が無い
- P1-8 全年の原稿に実在曲「また明日遊ぼうね」がハードコード
- P1-9 facts の `valid_to: null` でラジオ台本に TV 番組が出る
- P1-11 `X-Forwarded-Proto` を素朴に信頼している
- R2-04 リクエストボディ無制限 / R2-03 429 がパスワード検証前
- P1-7 全 3030 曲に `preview_url` が無く、iTunes 障害で無音化する

いずれも**今回の変更範囲外**であり、公開判断の材料として残す。
