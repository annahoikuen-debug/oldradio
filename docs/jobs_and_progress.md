# ジョブと進捗の仕様

## ジョブ状態機械

ジョブは以下の状態を遷移します。

```
[queued] -> [running] -> [succeeded/failed/cancelled]
```

- `queued`: ジョブがキューに入り、スロットの獲得を待っている状態。
- `running`: スロットを獲得し、実際の生成処理が行われている状態。
- `succeeded`: 正常に生成が完了した状態。
- `failed`: 生成中にエラーが発生した状態。
- `cancelled`: ジョブがキャンセルされた状態（クライアントによる abort またはサーバ側の明示的 cancel）。

## タイムアウト

- `SSE_MAX_SECONDS`: SSE ストリームの最大継続時間（デフォルト: 1800秒）。この時間を超えるとサーバ側でストリームを閉じる。
- `SSE_KEEPALIVE_SECONDS`: keep-alive コメントの間隔（デフォルト: 15秒）。この間隔でコメントを送信し、接続の維持を確認する。
- `DEFAULT_POLL_AFTER_SECONDS`: クライアントがポーリングを開始するまでの遅延（デフォルト: 3秒）。

## キャンセル保証

キャンセルが要求されてからスロットが解放されるまでの最大遅延は、各フェーズにおける「cancellable な待ち」の longest ブロック時間に依存します。

| フェーズ | キャンセルチェックポイント | 最大遅延目安 |
|----------|---------------------------|--------------|
| TTS スロットル (`_tts_throttle`) | `cancellable_wait` | 10秒（`_TTS_THROTTLE_SECONDS`） |
| Gemini の tenacity リトライ | `cancellable_sleep` (via `install_cancellable_sleep`) | 指数バックオフ（最大 60秒） |
| iTunes プレビュー取得ループ | 曲ごとのチェックポイント (`server.py` 側) | 1曲あたりのネットワーク遅延 |
| ステップ境界 (`_build_generate_response`) | `Job.checkpoint()` | ステップごとの処理時間（通常は数秒） |

したがって、キャンセル要求からスロット解放までの総遅延は、**数秒から数十秒**の範囲に収まります（極端なネットワーク遅延を除く）。

## SSE イベント仕様

以下のイベント名は `static/index.html` の 4 ステップラベルに 1 対 1 で対応します。

| イベント名 | UI ラベル | 説明 |
|------------|-----------|------|
| `job.started` | 電波を受信中 | ジョブがキューに入り、スロット獲得待ち |
| `script.started` | 原稿を書く | 原稿生成の開始 |
| `script.done` | 原稿を書く | 原稿生成の完了 |
| `tts.segment` | 読み上げる | TTS セグメントの生成開始 |
| `tts.done` | 読み上げる | TTS セグメントの生成完了 |
| `music.started` | ヒット曲を送る | 曲検索の開始 |
| `music.search.done` | ヒット曲を送る | 曲検索の完了 |
| `playlist.done` | ヒット曲を送る | プレイリストの作成完了 |
| `estimate` | （進捗バー用） | 推定残り時間の更新 |
| `done` | （完了） | ジョブの正常完了 |
| `failed` | （失敗） | ジョブの失敗 |
| `cancelled` | （キャンセル） | ジョブのキャンセル |

**終端イベント**（`done`, `failed`, `cancelled`）が発生すると、SSE ストリームは有限に終了します。

## クライアント abort とサーバ側明示的 cancel の区別

- **クライアント abort**: クライアントが HTTP 接続を切断した場合（例: タイムアウト、ユーザーによる中止）。サーバ側では `Job.cancel_event` がセットされ、ジョブはキャンセル状態に遷移します。スロットはジョブ終了時に解放されます。
- **サーバ側明示的 cancel**: `/api/jobs/{job_id}` に対する DELETE リクエストにより、サーバ側から明示的にキャンセルが要求されます。これにより `Job.cancel_event` がセットされ、同様にジョブはキャンセル状態に遷移します。

どちらの場合も、ジョブは `cancelled` 状態となり、スロットは解放されます。

## 受信待ちの充填音（待機 bed）

原稿生成と TTS には 30〜60 秒かかり、その間クライアントは**無音**でした。
`static/app.js` は進捗パネル表示のあいだ、Web Audio で組み立てた擬似ノイズ
（テープヒス + 60Hz ハム + ゲイン揺らぎ）と、ステップ遷移の 1 回だけの効果音を
鳴らします。音声ファイルは持たないため、オフラインでも必ず鳴ります。

| 関数 | 役割 |
|------|------|
| `ensureAudioContext()` | ユーザージェスチャ（再生ボタンの click）内で `AudioContext` を生成する |
| `startStandbyTone()` | `startProgress()` から呼ばれ、ノイズ + ハム + LFO の 3 音源を立ち上げる |
| `playStandbyStepCue(stepKey)` | ステップが「進行中」に変わった瞬間だけ効果音を 1 回鳴らす |
| `stopStandbyTone()` | `stopProgress()` / `startPlayback()` / `pagehide` で必ず呼ばれる |
| `applyStandbyVolume()` | ミュート・音量スライダーに追従させる |

設計上の制約（守らないと音が全滅する）:

- **`MediaElementSource` / `AnalyserNode` を使わない。** キューが別オリジン
  （iTunes プレビュー）を含むと、CORS で恒久無音化する既知の罠があるため。
  待機音は `<audio>` の再生経路から完全に切り離す。
- **`AudioContext` は click ハンドラ内で生成する。** 生成が fetch 完了後の
  `ensureAnalyser()` だけだと、WebKit / Firefox で suspended 起動し、
  `createMediaElementSource()` 経由の出力へ恒久的に再ルーティングされる。
- **進捗タイマーは 250ms ごとに `setProgressStep()` を呼ぶ。** 効果音は
  `state.standbyCuedStep` で 1 ステップ 1 回に抑えないと 4Hz で鳴り続ける。
- **`AudioContext` が作れないブラウザでは静かに no-op**（VU メーターと同じ方針）。
  一度失敗したら `state.standbyUnsupported` で以後試行しない。

回帰テストは `tests/test_standby_tone.py`（dukpy + フェイク AudioContext）、
ジェスチャ内生成の静的契約は `tests/test_audio_regression.py`。

## `estimated_ms` の計算式

`estimated_ms` は以下の要素から計算されます。

```
estimated_ms = tts_calls * per_tts_ms + script_ms + music_ms
```

- `tts_calls`: 必要な TTS セグメント数（原稿の文字数に基づく）
- `per_tts_ms`: 過去の TTS 呼び出しの中央値（`LATENCY_WINDOW` から算出、直近 64 回）
- `script_ms`: 原稿生成の固定コスト（実測値、現在は 0.158秒程度）
- `music_ms`: 曲検索とプレイリスト作成の固定コスト（実測値、現在は 12.885秒程度）

**重要**: `per_tts_ms` はハードコードされた定数ではなく、`LATENCY_WINDOW`（直近 64 回の TTS 呼び出し遅延）から動的に算出されます。呼び出し履歴が無い場合（キャッシュミスが続く場合）は、保守的に 1015ms（実測の初期値）を使用します。

## 運用上の注意

- `SSE_MAX_SECONDS` を超えるジョブは、強制的にストリームが終了しますが、バックグラウンドのジョブスレッドは継続します。ただし、`TERMINAL_STATES` に到達した時点でスロットは解放されるため、リソースリークは発生しません。
- keep-alive コメント（`: `）は 15 秒ごとに送信され、プロキシやクライアント側のタイムアウトを防止します。
- 認証が無効（個人モード）の場合でも、ジョブ機構は同様に動作し、スロット管理とキャンセルが機能します。