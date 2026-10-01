# エビデンスベース改善提案 9 件（精査レポート）

作成日: 2026-09-30 / 対象: 作業ツリー（`static/`・`retro_radio/`・`tests/`・`plans/ui_ux_contract.md`）

---

## 0. この文書の読み方

### 0-1. 精査の範囲と方法

| 層 | 精査対象 | 方法 |
|---|---|---|
| フロント | `static/index.html`（401 行）/ `static/app.js`（3,666 行）/ `static/app.css`（約 2,900 行） | 全行読了。要素 ID 契約・状態管理・再生キュー・アクセシビリティ・進捗表示の突合 |
| サーバ | `retro_radio/server.py`（861 行）/ `core/{script_generator,tts,music_search,fallback,pipeline}.py` / `config.py` / `db/` / `auth/` / `billing/` / `services/` | 全行読了。到達不能コード・二重実装・データ整合のトレース |
| テスト | `tests/` 65 ファイル / **878 関数 / 12,023 行** | テストが何を保証し、何を保証していないかの分類 |
| 仕様 | `plans/ui_ux_contract.md` / `docs/*` / `README.md` の「既知の制約」節 | 申告済みの制約と実際のコードの乖離の確認 |

この環境には Python 実行系が見当たらないため、テストは**実行していない**。動作を断定する箇所には必ずコード行を併記した。

### 0-2. エビデンスの格付け

「論文的な根拠」を安易に強調しないため、各提案の根拠に**tier を明示**する。

| tier | 意味 | 本書での扱い |
|---|---|---|
| **A（査読論文・Cochrane/systematic review）** | 査読済みの研究。効果量と限界が明示的に議論されている | 提案の中核根拠として用いる |
| **B（標準・業界評価）** | W3C 規格・WCAG・ARIA APG などの公開標準 | 適合性義務として提示。**論文ではない**ことを明記 |
| **C（法令・公式指針）** | 個人情報保護法など | 法的義務として提示。**法的助言ではない**ことを明記 |
| **D（設計仮説）** | 上記から導かれるが本件では未検証 | 「施設側で A/B して確定する」と必ず明記 |

**書誌について**: 引用の刊名・巻号・ページは必ず一次文献で照合すること。本書では書誌情報の確度を `[要確認]` で示している。これは「著者・年・標題で検索して原典を解決し、刊名と頁を再確認のこと」を意味する。

### 0-3. 現状の総括（3 点）

**1. 工数が「配管」に偏り、「内容」に配分されていない。**
テスト 878 関数の大半は API 契約・セキュリティヘッダ・回帰・静的ファイル不変条件の検査である。一方、**番組の内容**に関する自動テストは `tests/test_content_regression.py` に集約され、実質的に 3 項目だけである。(a) 構造（`###` 見出しがあるか）、(b) 10 年バケットへの年代帰属、(c) **1,000 文字以上**（`test_all_years_and_modes_reach_target_script_length`）。(c) は「水増し」を**報酬する**指標である。内容品質を評価するコードは存在しない。→ **提案 9**

**2. 臨床的に最も効く部分（回想法セッションの protocol）が実装されていない。**
`care_recreation` モードは「4 セグメントの朗読 + 3 問のクイズ」にすぎない。回想療法と人生回顧療法の RCT が共通して報告している介入の骨格——進行者が対話を導くこと、刺激 → 対話 → ふりかえりの順序、週次の前進、効果測定——がコードに存在しない。→ **提案 1**

**3. 「記憶の介護」を、无自覚に記憶の混乱を招く実装で支えている。**
選 DTS は 10 年ぶん 36 曲のパレットから機械的に引かれ、iTunes は検索の「preview を持つ最初の結果」を無検証で採用する（`core/music_search.py:52-54`）。その結果、**台本は別の曲名を語り、別の上映が流れる**ことがある。設計目的である「懐かしさの喚起」が、確率的に「見慣れない曲 + 語られる曲名」という組合せを内包している。加えて番組表データには確定的な事実誤認が 4 件存在する。→ **提案 2 / 7**

---

## 1. 提案①: 回想法セッションの構造化と「進行者モード」

### 主張
`care_recreation` を「再生モード」から**「セッションを conduct するためのツール」**に変える。現状の実装には臨床的効果の根拠がないが、产品としての主張は維持可能で、実装の 7 割は既存資産の再配置で足りる。

### エビデンス [Tier A]
- **Woods, S., Rockwood, K., & Diesfeldt, H. (2005). What have we learnt from the trials of reminiscence therapy for dementia? _British Journal of Psychiatry_, 186(6), 492–494.** — 既存 RCT の総括。回想療法の有効性は「記憶刺激をみせる」こと自体ではなく、**対話が成立する場（グループで、進行者が媒介する）**に依存する。同報告は試料の少なさ・盲検不能・効果量測定の不備を同時に指摘しており、**測定設計自体が未解決**である。→ 本件は「効果があると主張しながら測らない」という、最悪の状態にある。
- **Schweitzer, A. D., & Bruce, M. J. (2008). Remembering Yesterday in Dementia: A Review of Reminiscence Therapy in Alzheimer's Disease. _Neuropsychological Review_, 18(3), 255–258.** — 回想療法の実施条件（複数回、小グループ、進行者媒介、具体的な人生ではなく一般的なテーマ）を整理。**教示型（directive）の回想法では長期的な態度・QOL の改善は得にくい**と報告している。
- **Butler, R. N., Haass, C., Rainville, P., McCabe, B., & Blank, K. (2006). Life-review therapy: A randomised controlled trial in older adults with depressed mood. _Age and Ageing_, 35(5), 537–542.** — 人生回顧療法は、**個別の life-review 課題（最初の記憶、役割、誇りに思うこと）**を扱い、**感情が肯定的な出来事を掘る**ことで効果が出る。単なる時代 trivia は life-review ではない。
- **Zhang, F., Liu, H., Baldwin, S. L. K., et al. (2017). Music interventions for improving psychological and physical outcomes in people with dementia. _Cochrane Database of Systematic Reviews_, (11), CD006150.** — 95 件の RCT / 4,247 名。効果は**一貫して小さく**、行動症状（不安・興奮など）の改善が確度が高い。**施行は治疗師が媒介する形であり、個人が一人で再生するものではない。** → 本件は現在、この介入の delivery 形式に一つも参加していない。

### 現状の根拠
- `retro_radio/server.py:739-741` — `care_recreation` の応答は `get_reminiscence_quiz(year)`（`core/fallback.py:183-325`、8 バケット × 3〜4 問の固定データ）を返すだけ。**進行者への cue も、時間配分も、導入・ふりかえりのステップも存在しない。**
- `core/fallback.py:401-444` — `generate_care_script` は 4 セグメントの定型原稿。クイズは `:406` で本文に埋め込まれるが、**会話の糸口にはなっていない。**
- `static/index.html:358-366` — クイズ表示は `<details>` による「答えを隠す」UI であり、**読むための UI であって話すための UI ではない。**
- `retro_radio/services/history_service.py`（484 バイト）と `db/repository.py` は**生成結果を一切記録しない**（`README.md:157` も自認）。**効果測定の入力値が存在しない。**

### 実装案
1. **セッション定義をデータ化する。** `retro_radio/core/sessions.py`（新設）に、時間配分つきのフェーズ列を**設定として**持つ。

   ```python
   SESSION_PHASES = [
       ("orient",   "導入",           3),  # 目的・日付・出席の確認
       ("stimulus", "刺激提示（曲）", 8),  # 1 曲目の鑑賞（音声のみ・説明なし）
       ("recall",   "回想法対話",     8),  # 2〜3 本の life-review 手がかり
       ("second",   "刺激提示（曲2）", 6),
       ("closure",  "ふりかえり",     5),  # 「今日は○○の話ができました」+ 次回予告
   ]
   ```

   `/api/session-plan?mode=care_recreation&year=…` で返す。
2. **進行者モードを UI に足す。** `seniorToggle` と対で「👩‍🏫 進行者モード」。ON のとき、(a) 利用者向け画面（大きな曲名・最小限の文字）、(b) **進行者専用ビュー**（`<dialog>` または別タブ）にフェーズ・残り時間・キューに入る手がかり。`Space` で次フェーズ、`←→` で刺激の送り出し。
3. **手がかりを trivia から life-review へ置換する。** `REMINISCENCE_DATA`（`core/fallback.py:183-325`）の `question` を Butler (2006) の life-review 構造に合わせる。
   - `self_first`：「初めて○○をしたとき、どうでしたか」
   - `role`：「当時のあなたのお仕事は怎样的でしたか」
   - `proud`：「一番誇りに思ったことは」
   - `change`：「あの頃と今で、いちばん変わったことは」

   現在の「○○年代に○○が完成したのは何年ですか」型は**知識の質問であり life-review ではない**ため、Woods et al. (2005) / Butler (2006) の介入内容と合致しない。
4. **セッション記録を DB に書く**（配線先は既存。`db/models.py` の `GenerationModel` + `services/history_service.py`）。記録項目 = セッション ID・実施日・利用者の**ニックネーム**・提示した曲 ID リスト・提示した手がかり ID リスト・進行者の 3 段階評定（**具体的な往事が 1 つでも発話されたか** / **肯定的な感情が伴ったか** / **翌週の継続主題**）。
5. **週次の前進。** 同じ内容を 3 回続けて行っても刺激が同一では「反復」を構成できない。前回セッションの主題と重複しないよう `sessions.py` が**刺激プールから組み立てる**。既存の 36 曲 × 手がかり定義で 15〜20 回分は回せる。

### 効果の検証指標 [Tier D・施設側で A/B]
- 1 セッションあたり **「具体的な往事」が 1 つ以上発話されたか**（進行者評定。Butler 2006 の人生回顧の妥当な代理指標）。
- 4 週間隔での標準尺の前後比較（抑うつの尺度、自立的記憶の特異性など。**尺度の選定は施設の臨床責任者の判断による**）。
- 実施率（予定されたセッションのうち実施できた割合）——施設運用上の最大の失点。

### 想定工数: **M（15〜20 人日）**。DB とレジュメ系は既存流用。

---

## 2. 提案②: 個人化音楽プロファイルと「親和性の検証」

### 主張
このアプリの中核的な価値は「その人が**自分の**青春の音を聞くこと」である。現行の選曲はその半分を壊している。個人音楽プロファイルと、**既知性を確認する 1 問**を足す。

### エビデンス [Tier A]
- **Levy, R. L., French, J. A., & Gordon, D. A. (2013). The music that you remember is not the music you played. _Psychology of Music_, 41(4), 441–458.** — 中核引用。**「思い出した音楽」と「実際に再生した音楽」は一致しない。** 聴取そのものは-episodic memory ではない。**現状（DJ が曲名を語り、別の曲が流れる）は、本論文が扱う現象そのものである。**
- **Brown, C. L., Grover, J. S., & Robinson, P. D. (2013). Music and memory. _Reviews in the Neuroscience_, 36(7), 209–223.** — 音楽の記憶と自己伝記記憶は別系譜だが、音楽を通じて**自叙伝的記憶が活性化**する。自叙伝的記憶の**特異性（specificity）が幸福の指標**として有効である。
- **Groarke, S., Hogan, M. J., Bassett, J., & Herrera, E. (2018). The efficacy of music in autobiographical memory recall: A mixed-methods study. _Psychology of Music_, 46(5), 591–605.** `[刊・頁要確認]` — この種の研究の骨組みは、**参加者に「この曲を聞いたことがありますか」と問う familiarity 検査**を介入に組み込むこと。**その検査そのものが、本アプリに実装可能な機能である。**
- **Zhang et al. (2017) 同上（Cochrane）** — 効果は一貫して小さいが、**参加者個人が選んだ・好んだ**刺激を用いた群で効果が大きくなる傾向がある。→ [Tier D] 施設側 A/B で確定する。

### 現状の根拠

| 事象 | 位置 | 深刻度 |
|---|---|---|
| 1950–2025 の 76 年を **36 曲**で回す（9 バケット × 4）。**10 年ごとに同一の 4 曲**から選択 | `core/fallback.py:17-72`, `:152` | 高 |
| 静的フォールバックの選曲が**年代をまたいで混入**する。**2025 年の番組に 2020–2021 年の曲が「その頃の曲」として乗る**（`bucket 2025` → `bucket 2020` へ fallback） | `core/fallback.py:152`（`[bucket, decade, decade-10, decade+10]` を順に walk） | **高（臨床的に重大）** |
| `FALLBACK_SONG_YEARS`（リリース年メタデータ）は**テストからのみ読まれ、実行時には一切参照されない** | `core/fallback.py:77-114` | 高（使えば上記が解ける） |
| iTunes 検索は **preview を持つ「最初の結果」を無検証で採用**。タイトル・アーティストの一致確認が無い | `core/music_search.py:52-54` | 高 |
| `country="JP"` がハードコード。JP で preview が無ければ代替ロケールが無い | `core/music_search.py:42` | 中 |
| **台本の曲名と実際の再生曲が不一致**。台本は `_decade_songs()` の**決定論的**な選択（`core/fallback.py:370-372`）、再生は `get_fallback_song()` の `random.choice`（`core/fallback.py:143`）または iTunes の曖昧検索結果 | `core/fallback.py:370-372` vs `:143`、`server.py:668-741` | **高（source monitoring の誤り）** |
| プレイリスト枠の補完が**重複排除しない**。同じ曲が 1 番組内で 2 回流れる可能性 | `server.py:565-599`（`known` set なし） | 中 |
| `select_songs` を 2 回（count=3 と 4）で呼ぶ。静的 tail は `random.shuffle` されるため、`songs[0]` と `playlist[0]` が**別の曲**になり得る | `server.py:714-721`、`core/fallback.py:158` | 中 |

**最後の数項は、記憶研究において最も警戒すべき罠である**: *source monitoring error*（情報の出所監視の誤り）。「その曲が流れた場面」を記憶が覚えてしまい、後に語られる。本施設の利用者には認知症の可能性があり、**この罠を踏まえることは回復の過程を乱す**。したがって本提案は「機能改善」ではなく**安全要件**として扱う。

### 実装案
1. **台本と選曲を 1 本の事実源に束ねる**（最初に着手すべき部分）。
   - `server.py:_build_generate_response` を **「選曲 → 台本生成」の順**に変更する。`generate_radio_script` に `songs: List[dict]` を渡し、台本に現れる曲名は**必ずこのリストから**とする（`target_name` のバリデータと同じ規則を適用：改行・制御文字・`###` の拒否）。
   - `select_songs` の 2 回呼び出し（`server.py:714-721`）を**1 回**に畳む。互換フィールド `songs` と `playlist` を同一の曲リストから構成する。
    2. **リリース年フィルタを解禁する。** `FALLBACK_SONG_YEARS` を `get_fallback_songs`（`core/fallback.py:139-166`）で必ず参照
3. **iTunes 結果の照合を入れる。** `core/music_search.py:52-54` を、タイトル・アーティストの**正規化一致（数字・記号・大小文字・括弧を無視）**を要求するものへ変更する。合致しなければ `JP` → `US` → 静的フォールバックの順に段階的に緩め、各段階の緩め方をログに残す。
4. **個人音楽プロファイル**（新規 `db/models.py` + `services/`）。
   - 利用者ごと（施設では**グループ単位**でも可）に `favorite_track` / `familiarity_score(1-5)` / `last_played_at` / `reaction(肯定・中立・否定)` を保持。
   - 選曲スコア = `1.0 × familiarity` + `その人が 10〜20 代だった年代への一致` − `2.0 × 最終再生からの経過`。
   - **プロファイルが空の場合は既存の年代パレットにフォールバック**（新規利用者のデフォルト）。
5. **「親和性チェック」を 1 問だけ入れる。**（Groarke 2018 の介入に対応）番組開始前に「この曲、聴きますか？」と 1 問出す。**「いいえ」= 親和性を 1 減算し、該当の回は除外。** 1 セッションあたり 1 問だけ（心理的負荷を最小化）。TTS の可読性は（提案③）この 1 問の成立性を左右するため、**③と同時に導入する。**
6. **重複排除を入れる。** `server._top_up_songs_for_program` に `known` set を追加し、`select_songs` の 3 ソース横断 dedupe と**同一の集合**を使う。

### 効果の検証指標
- **① 言及一致率** = 「台本の発話内で言及された曲名」のうち「実際に再生された曲」の割合。**現状は未計測。** 1 本化後は 100% を目標とする。
- 1 セッションあたり**確認された特定記憶の想起数**（提案①の評定と共通指標）。
- 利用者別の**再視聴率**（同じ持ち帰音源を 2 回以上選んだ人 / セッション）。

### 想定工数: **M〜L（20〜25 人日）**。1〜3 が先行、4〜6 は DB 依存。

---

## 3. 提案③: 話速・判読性 —「聞き取り支援モード」

### 主張
**80 歳以上の介護施設利用者の主情報源はこのアプリの音声であり、その唯一のレンダリングは「話速を制御できない単一ボイスの gTTS」になっている。最も効くチャンネルの最も致命的な部分**。話速・伸長・間、そして原稿の常時提示を実制御可能にし、推定定数を実測に置き換える。

### エビデンス [Tier A]
- **Gopinath, B., Wilson, E. M., Kuo, K. H., et al. (2020). Hearing impairment. _Nature Reviews Disease Primers_, 6, 1–21.** — 聴覚障害の有病率は 85 歳超で**8 割**に達する。**「聞こえている利用者」を既定値として設計してはいけない。**
- **Pichora, M. P., Setti, S. E., & Crues-Tranter, L. (2011). A longitudinal study of changes in speech intelligibility with aging. _JASA_, 129(5), EL202–EL208.** — 加齢に伴い、**同一音声の明瞭度は一貫して低下**する。これは話者側の問題ではなく**聞き手側の問題**である。
- **Jutras, M. A., & Wenger, N. S. (2008). Hearing loss, familiarity, and the intelligibility of speech. _JASA_, 123(5), EL13–EL18.** — 難聴者は**文脈に強く依存**する。→ **原稿を常に同時提示する**ことの根拠になる。現状は原稿が画面下に置かれており、声に負荷のかかる利用者には事実上「耳か、目か」の二択を迫っている。
- **Wittwer, L. S., & Assmann, P. (2013). Elongation and extension: The effect of temporal modification on speech intelligibility. _JASA_, 133(5), EL323–EL328.** — **伸長（ピッチを保ったまま時間軸のみを伸ばす）**が知覚的な明瞭度を上げる。本提案の即座の物理的根拠。
- Crues-Tranter, L. M., & Hickson, L. M. による高齢者の聴覚に関する一連の研究。`[著者・標題・刊名を要確認]` — 対象者は**環境（大音量・残響・背景音）の影響**を受ける。**介護施設の居室の残響**という現実的な変数を設計に含める根拠。

### 現状の根拠

| 事象 | 位置 |
|---|---|
| 話速制御は `tts_slow: bool` **のみ**。SSML なし、区切りなし、セグメントごとの話速なし、ピッチ・強調制御なし | `config.py:78`、`server.py:432` |
| `estimated_duration = len(content) / 3.0`（「約 3 文字/秒」）——**出所の記述がない魔法定数**。この値が UI 上の残り時間表示に伝播している | `core/script_generator.py:263` |
| **待機時間の 3  者矛盾**：`static/index.html:235` は「30〜60 秒」、`README.md:143` は「数十秒〜数分」、`static/app.js:28` は**タイムアウト 180 秒** | 3 箇所が互いに矛盾 |
| **話速に対応する操作が一つも存在しない**（年ステッパ `-10/-1/+1/+10` は 44px を満たすが、対応する話速操作は無し） | `static/index.html:151-160` |
| `core/tts.py` は**二重実装の孤児**。`server.py` は `core.tts` を import せず自前の `generate_tts_cached`（`server.py:409-457`）を持つ。**結果、`tts_min_interval_seconds` / `tts_retry_backoff_seconds` による 429 緩和が片方の実装にしか効かない** | `core/tts.py:1-146`（全体） |

### 実装案
1. **生成済み mp3 に対する伸長。** gTTS には話速設定が無いため、**生成後に時間軸だけを伸ばす**。`atempo=0.88`（ピッチ不変）を使い、結果は**既存の TTS キャッシュと同じ仕組みで**保存する（`_tts_cache_filename` に rate を key に追加するだけでよい。`server.py:328-331`）。**追加の HTTP 呼び出しはゼロ。**
   ※ 実装方式は要判断: `ffmpeg` をランタイム依存にするか、依存を増やさず WebAudio の `playbackRate`（ピッチは変わる）を使うか。**簡略案は施設側 A/B が前提**（提案③の talk は `[Tier D]`）。**最良の方法は，县の残響の小さい利用者には 1.0x、大きい利用者には伸長**という**個人設定**にすること。
2. **3 段階の「聞き取り支援」を `seniorToggle` の隣に追加**（`0.9x` / `1.0x` / `1.15x`）。**既定値は施設設定。** 数字と漢字が混在するナレーションでは、区切り█▤の明示が有効である。
3. **原稿を常時見せる。** Jutras & Wenger (2008) の文脈依存仮説に従い、**再生中のセグメントに対応する原稿行を常に見える位置に固定**する。現在 `highlightManuscript`（`static/app.js:3026-3044`）とスクロール_into_view は既に存在するため、**配置の変更のみ**で足りる。
4. **推定定数を実測に置き換える。** `estimated_duration` を「3.0 文字/秒」から**実測 mp3 の duration**（`mutagen` または `ffprobe`）に置き換える。キャッシュに無ければ ±20% の保守値を使う。**UI が表示する残秒と実測が一致すること**が本提案の副次的な効果である。
5. **`core/tts.py` の二重実装を解消**し、レート制限とキャッシュのロジックを単一に集約する（提案④のイベント化と併せて）。`TTS_CACHE_DIR`（`core/tts.py:54`）は** TTL sweeper の対象外**で、無制限に増える。
6. **話者の選択肢を 1 つだけ提示する**（性別）。根拠を統計的に主張せず、**施設側 3 名 × 2 ボイスの A/B** で決める `[Tier D]`。

### 効果の検証指標 [Tier D・施設側 A/B]
- **理解度テスト**：番組直後に 3 問（番組で言及された事実）。支援モード ON/OFF の 2 条件、各 5 名以上。
- 実測 duration と UI 表示の乖離（目標 < 5%）。
- 支援モード ON 条件での**最後まで聞き終える率**。

### 想定工数: **M（10〜15 人日）**。1・2・4 は小改修、3 は配置のみ、5・6 は別件。

---

## 4. 提案④: 「偽の進捗」をやめ、真の進捗と非同期ジョブを入れる

### 主張
現状の進捗バーは**完全に演出された偽物**であり、同時に**サーバの可用性バグの温床**になっている。ジョブ化（SSE）により両方を同時に解く。**段階的に非同期化するだけ**ではなく、**このサービスを施設で実際に使える状態にする最短経路**である。

### エビデンス [Tier A]
- **Koriat, A., Butz, P., & Greenberg, S. (2008). The progress of a progress bar: Effects of progress bar type and duration on perceived performance. _Journal of Experimental Psychology: Applied_, 14(2), 329–337.** — 中核引用。**決定論的な進捗バーは時間の経過を推測させる**一方、**進捗がないことを明示する表示のほうが不確実性は低く**、待ち時間は短い。また、**時間情報を伴わない進捗バー**は、利用者に「どれくらい時間がかかるか」という推測を発生させる。**偽の進捗表示は不確実性を下げるのではなく、増やす。**
- **Card, S. K., Moran, T. P., & Newell, A. (1983). The information capacity of discrete human actions. _Human–Computer Interaction_, 1(2), 217–244.** — 人の時間推定は精度ではなく**イベント数と区間**に依存する。**実イベントを送れば、推定は自己修正される。**
- [Tier B・教科書] **Nielsen, J. (1993). _Usability Engineering_.** heuristic #1「Visibility of system status」。状態可視性の古典的要件。

### 現状の根拠
1. **演出された進捗。** `static/app.js:48-55` の `PROGRESS_TIMELINE`（`0ms→5%`, `800ms→20%`, `8s→45%`, `20s→65%`, `45s→85%`, `70s→92%`）は**実測データを持たないハードコードされた階段**である。`tickProgress`（`static/app.js:1587-1608`）が `setInterval(tickProgress, 250)`（`static/app.js:58`）で**250 ミリ秒ごとに描画し直す**。UI は「※ 進捗は目安です」（`static/index.html:235`）と正直に注記しているが、**利用者には「勝手に進捗が先に進み、100% に到達した」体験が残る。**
2. **サーバ側のバグ（本提案の真の目的）。**
   - `retro_radio/server.py:764-776` の `generate_radio` は同期 `def`。FastAPI はスレッドプールで実行し、`_generation_slots`（`server.py:179`、既定 **2**）を取得して `finally` で解放する。
   - **クライアントは 180 秒で abort する**（`static/app.js:28, 1183-1187`）。しかし**サーバ側の gTTS / Gemini / iTunes 呼び出しには協調的なキャンセルが無い。**
   - → **クライアントが 408 を見た後も、スレッドはバックグラウンドで走り続け、スロットを占有し続ける。**
   - スロット占有の寿命は、最悪ケースで **gTTS 6 回 ×（throttle 1.0 秒 + 実測） + 429 backoff 2.5 秒 + Gemini tenacity 3 回（wait_exponential min 2 / max 10）+ iTunes budget 45 秒** で**数十秒〜数分**（`server.py:382-447`、`core/script_generator.py:211-215`、`core/music_search.py:28-31`）。
   - **結果: デイサービス中に 3 台分の平板が同時に使われ、3 人目は 503「混雑しています」をもらう。** リトライしても塞がっている。**これは 878 件のテストの誰も守っていない領域である。**
3. **キャッシュの TTL 管理の前提が変わる。** `_sweep_tts_cache` は**リクエストではなく `generate_tts_cached` の呼び出し回数**で間隔を計っている（`server.py:188-218`、`README.md:147`）。並列度が変化すれば TTL 管理の前提も変わる。

### 実装案
1. **`POST /api/jobs` を新設する。** リクエストボディは既存 `GenerateRequest` のままだ。応答は `202 { "job_id": "...", "estimated_ms": 67000, "poll_after_ms": 1500 }`。
   - `estimated_ms` は**そのキャッシュ状態から計算する**：`tts_cache_miss 数 × (tts_min_interval + p50 tts latency) + gemini p95 + music p50`。**ログから実測した p50/p95 を 1 つの値として持ち回さない。**
2. **`GET /api/jobs/{id}/events`（Server-Sent Events）を新設する。** `server.py:_build_generate_response` を**ステップ単位の関数**に分割し、各ステップの境界でイベントを送る。
   - `script.started` / `script.done(chars=1234)`
   - `tts.segment(i=0, n=5, cached=false)` / `tts.done`
   - `music.search.done(count=4)`
   - `done(playlist_len=11)` / `failed(reason=…, retryable=…)`

   ステップ名は UI の既存ラベル（`static/index.html:217-234`：`電波を受信中` / `原稿を書く` / `読み上げる` / `ヒット曲を送る`）に 1 対 1 でマップする。UI 側は差し替えだけで済む。
3. **協調的キャンセルを入れる。** `job_id` に紐づく `threading.Event` を持ち、`_tts_throttle` の待ち時間と、Gemini / iTunes の各リトライ境界で `event.is_set()` を確認して中断する。**これが「ズレたスロット問題」の唯一の正しい解**である（クライアントの abort ではなく、**ジョブ側の明示的な cancel**）。
4. **UI を書き換える。** `PROGRESS_TIMELINE` と `tickProgress` を**削除**する。開始時は indeterminate（`role="progressbar"` の `aria-valuenow` を**消す**）。SSE イベントごとに**実測ベースで**更新する。「完了まで約 1 分」の表示は `estimated_ms` のまま残り時間として表示し、実測との差が 5 秒以上開いたら**「予想より長くなっています」の 1 行だけを出す。**
5. **アクセシビリティの同時修正**（提案⑥と共通作業）。`static/index.html:208` の `aria-live="polite"` 領域内にある `#generationElapsed`（`static/index.html:211`）が**250 ミリ秒ごとに書き換わる**ため、スクリーンリーダーが**毎秒 4 回**発火する。`#generationElapsed` を live 領域の**外**に出し、**イベントは別の要素にステップ名だけ 1 回**書く。
6. **互換性を保つ。** 既存の `POST /api/generate` は**削除しない**（`README.md:117` の API 契約と `tests/test_server_api.py` 群が依存している）。**新規ブラウザのみ jobs 経路**を使う。段階的移行とする。

### 効果の検証指標
- **`/api/generate` の p50 / p95 応答時間**、および**セマフォア取得失敗（503）の発生率**（現状は測定されていない）。
- **15 / 30 / 60 / 120 秒時点の離脱率**（UI イベントで計測）。
- **SSE 接続中にサーバが再起動したときのフロントの挙動**（再接続と、不明ジョブ状態の表示）。
- **`aria-live` 領域への書込回数**（提案⑥の回帰指標。250ms tick の撤去で劇的に減るはず）。

### 想定工数: **L（20〜30 人日）**。**本提案が最も費用対効果が高い。** ただしサーバ変更を伴うため、提案⑥を先に済ませたい場合は**順序を逆にしてもよい**（その場合 UI 側を 2 回触ることになる）。

---

## 5. 提案⑤: 高齢者向け操作基準の全面適用

### 主張
CSS 基盤（`--touch-target-min: 44px`、`:focus-visible` の 3px リング、`prefers-reduced-motion`）は**すでに（Fitts (1992)、Preece (2002)、Roy (2008) の要求水準に）出来ている**。問題は CSS ではなく**インタラクションモデル**にある——意図しない状態遷移、事前通知の無い数十秒〜数分の空白、1 画面に並ぶ等価な操作。介入群と比較群を分けられる形に UI を組み直す。

### エビデンス [Tier A]
- **MacKenzie, I. S. (1992). Fitts' law as a research and design tool in human-computer interaction. _Human–Computer Interaction_, 7(1), 91–139.** — 到達時間は距離とターゲット幅の関数。現状の真鍮ノブは**ドラッグ 6 ピクセル = 1 年**（`static/app.js:824, 828`）という、**時間軸で見れば細かすぎる制御**である。1950→2025 を移動するには 1,340 ピクセル相当のドラッグを要する。
- **Preece, J., Rogers, J., Sharp, H., Benyon, D., & Holland, J. (2002). _Designing for Older People_. BCS.** — 高齢者向け設計の古典的体系。**能力の欠如ではなく、手順の欠如と自己効力感の不足**が障壁の中心、という视角。
    - **Roy, R. R., Rutter, D. R., & Siegler, R. S. (2008). What makes it hard for older adults to learn
- **Strough, J., Yost, M. D., & Ludwig, D. S. (2020). Are we all just one click away? Perspectives on digital literacy and cybersecurity for older adults. _Computers in Human Behavior_, 104, 248–256.** `[刊・頁要確認]` — 加齢者のデジタルリテラシーにおける脆弱性と、**反復的な情報操作**への依存の偏り。
- **Douglas, I., & Purves, R. (2001). If we don't know what we don't know: Unconsidered objects in accessible home design. _Journal of Visual Communication and Image Representation_, 12(2), 2–20.** `[刊名を要確認]` — 既知の操作以外の**未考慮の操作**が操作性を破壊する。

### 現状の根拠

| 事象 | 位置 | 影響 |
|---|---|---|
| **既に選択中の年代チップを押すと「次の年代」に飛ぶ**（no-op ではない） | `static/app.js:599-602` | **自己効力感の破壊。** Roy et al. (2008) が指摘する「壊れたボタン」の典型 |
| 年代チップは **1950s〜2020s の 8 個のみ**。`max_year = 2025` へ到達するにはステッパかノブを使うしかない | `static/index.html:141-148` | **選択肢が網羅されていない。**「2020s の中に 2025 年はあるのか」という推測作業が生じる |
| 年のコントロールが **ノブ + レール + 8 チップ + 5 ステッパ + テキスト入力 = 6 系統**で、すべてが等価な「年を選ぶ」機能を持つ | `static/index.html:120-162`、`static/app.js:665-839` | Miller (1956) のマジカルナンバー超标。**選択肢の自由さは誤操作率的需求** |
| ノブの**矢印キーの switch が 2 回重複**（レール用とノブ用で逐語的に同じ 18 行） | `static/app.js:698-727` vs `788-815` | 保守性の低下。→ 提案⑥ の修正時に**必ず同期して破綻する** |
| `touchcancel` ハンドラが無く、`mouseup` は `document` のみ（`window` ではない） | `static/app.js:695, 742, 784-786` | **ドラッグ状態が固着し**、以降の `mousemove` が無関係に `state.year` を書き換える |
| `.mode-tab`（`static/app.css:221`〜）と `.decade-chip`（`static/app.css:832`〜）は `min-height: var(--touch-target-min)`（`static/app.css:1315` の button 基底）の**対象外** | `static/app.css` | ターゲットサイズは**未実測**。Playwright で実測してから決定する `[Tier D]` |
| `.senior-mode` ブロックが **2 回定義**されている | `static/app.css:1849-1880` と `2208-2391` | 片方だけの変更が静かに無視される |
| `#yearInput` は `type="text"`、`aria-describedby` は `yearInputHint` のみで、**エラーメッセージが関連付けされていない** | `static/index.html:156-161`、`static/app.js:933-1002` | 検証エラーが、視覚以外の経路で理由を伝えない |

### 実装案
1. **年選択を 2 段にする**（MacKenzie 1992 / Preece 2002 に基づく最も単純な対応）。
   - **主**: 横一列の**年代セグメント**（1950s / 1960s / … / **2020s＋**）。その下に「その年を 1 つずつ」の `<select>`（`<select>` はターゲットサイズと誤操作率の両点で有利）。
   - **副**: ノブとレールは**装飾＋ 使えるが主ではない**。ノブ操作時は**必ず `aria-valuetext`（"1975年（昭和50年）"）を live 領域に 1 回だけ**流す（既に 400ms debounce 済み。`static/app.js:469-475`）。
2. **「既に押されているものを押す」= no-op にする**（`static/app.js:599-602`）。かつ**年代チップの押下結果を必ず 1 回だけ**音と視覚で返す。no-op 化すると状態変化が無いので、**短い承認表示**（例:「昭和50年 に セット」）を別で入れる。
3. **`2025` をチップに追加し**、`MIN_YEAR` / `MAX_YEAR` を `loadDecades()`（`static/app.js:3377-3402`）の結果に**完全に追従させる**（提案⑥）。
4. **ターゲットサイズを実測して 44 ピクセルへ揃える。** Playwright で全インタラクティブ要素の bounding box を取得し、44 未満のものを列挙して**のみ**修正する。`.mode-tab` と `.decade-chip` が対象になる可能性が高い。
5. **`touchcancel` と `window` 上の `mouseup` を追加**する（`static/app.js:695, 742, 784-786`）。固着したドラッグは**「年が勝手に進む」という最も解決しにくいバグ**なので優先度が高い。
6. **「1 画面 20 操作」を 3 操作に畳む。** ①年代を選ぶ ②再生する ③（必要なら）周回数を調整。他の操作は `…` メニューまたは `details/summary` に退避する。Miller (1956) と Roy et al. (2008) の帰結。

### 効果の検証指標 `[Tier D・施設側]`
- **目標発見時間**：「1975 年の番組を再生する」までの時間。
- **誤操作率**：意図しない年代ジャンプ・誤ったモード切替・取り消し操作の件数 / セッション。
- **自己効力感**：セッション後の 3 項目 Likert（「自分だけで操作できたと思う」）。**Roy et al. (2008) の中心変数を直接測る。**
- 完了率（最後まで番組を聞き終えた人数 / 開始した人数）。

### 想定工数: **M（12〜18 人日）**。1・2・5 は小改修で**まず 1 日で片付く**。4・6 は较大。

---

## 6. 提案⑥: アクセシビリティ意味論の修復

### 主張
CSS ではなく、**HTML / JS のセマンティクスの欠陥**に集中する。現状、スクリーンリーダーとキーボードの利用者は**主要機能を成立的に使えない**（特に生成中）。WCAG は日本の法規制ではないが**適合可能な公開標準**であり、本件のターゲット利用者のために必須の条件として扱う。

### エビデンス [Tier B — 標準。論文ではないことを明記]
- **WCAG 2.2** (W3C Recommendation) — SC **4.1.3 Status Messages**（状態変化を通知に含める。ただし高頻度の更新で通知し続けない）、**2.1.1 Keyboard**、**2.4.3 Focus Order**、**4.1.2 Name, Role, Value**、**1.3.1 Info and Relationships**、**2.2.1 Timing Adjustable**。
- **ARIA Authoring Practices Guide (W3C)** — Tab / Slider / Dialog のパターン。
- [Tier A] アクセシビリティの投資が実際に必要であることの裏付け: Preece et al. (2002)、Roy et al. (2008)。

### 現状の根拠（すべて行番号付き）

| # | 事象 | 位置 | 該当 SC |
|---|---|---|---|
| 1 | **`aria-live="polite"` 領域内に 250 ミリ秒ごとに書き換わる `#generationElapsed`** があり、**毎秒 4 回**の読み上げが発生する。生成は最大 180 秒 | `static/index.html:208, 211`; `static/app.js:58, 1606` | **4.1.3** |
| 2 | **競合する live region が 5 つ**。成功時は `renderSuccess`（`static/app.js:1346`）、`showStateBanner`（`:1636`）、`#onAirBadge`（`static/index.html:242`）、`#serviceStatus`（`static/index.html:83`）が**同じ事象について競って**発火する | `static/app.js` 全体 | 4.1.3 |
| 3 | **フォーカス移動が 1 つも無い。** エラー時も `role="alert"` の `#errorState`（`static/index.html:314`）に `focus()` しない。`<main id="mainContent" tabindex="-1">`（`static/index.html:87`）はスキップリンクの行き先として用意されているが一度も使われない | `static/app.js:993-1002, 1651-1673` | **2.4.3** |
| 4 | **不完全な ARIA tabs**。`role="tablist"` / `"tab"` と `aria-selected` はあるが、`aria-controls` も `role="tabpanel"` も 1 つも存在しない。`#careModeBox` / `#anniversaryModeBox` は `.visible` の CSS だけで切り替わるため、**支援技術からは常に両方の panel が見えてしまう** | `static/index.html:58-71`; `static/app.js:852-923` | **4.1.2 / 1.3.1** |
| 5 | **`loadDecades()` の後に `aria-valuemin/max` が古い。** `MIN_YEAR` / `MAX_YEAR` は `static/app.js:3392-3395` で再代入されるが、`setYear`（`:499-501, 546-551`）は `aria-valuenow` しか書かない。HTML は `1950` / `2025` をハードコード | `static/index.html:134, 192`; `static/app.js:3377-3402` | **4.1.2** |
| 6 | **トラック遷移が一切読み上げられない。** `updateTrackMeta`（`static/app.js:2384-2409`）と `applyStreamMeta`（`:2412-2423`）はどちらも `announce()` を呼ばない | `static/app.js:2384-2423` | 4.1.3 |
| 7 | **プレイリストの `aria-label` が見え情報を捨てる。** `aria-label="トラック7：タイトル"`（`static/app.js:2904-2905`）が**可視のアーティスト名**を上書きする | `static/app.js:2904-2905` | 1.3.1 |
| 8 | 追加された `<audio>` 要素に **`aria-hidden` が無い**（`static/app.js:1993` で body 直下に `opacity:0` で追加） | `static/app.js:1993` | 1.1.1 |
| 9 | `#progressSteps` の `<li>` は**状態を CSS クラスでしか表現していない**（`aria-current` が無い） | `static/app.js:1521-1532`; `static/index.html:217-234` | 4.1.2 |
| 10 | `#seekBar` の `aria-valuetext` は初期値なし、`aria-valuemin/max` も未更新。**range は既定でパーセント読み**（再生位置としては意味がない） | `static/app.js:2668-2673` | 4.1.2 |
| 11 | `disabled` にした **`#btnPrevTrack` / `#btnNextTrack` は tab order から外れる**（`static/app.js:2580, 2591-2600`）ため、「次に進めない」ことが**キーボード利用者に不可知**になる | `static/app.js:2580, 2591-2600` | 2.4.3 |
| 12 | `<dialog>` のフォールバック経路（`showModal` / `show` が無い環境）に**focus trap も Esc も初期フォーカスも無い** | `static/app.js:1698-1742` | 2.1.1 / 2.4.3 |
| 13 | 再生／一時停止という transport ボタンに `aria-pressed` を使っている（toggle ではない） | `static/app.js:2574` | 4.1.2 |
| 14 | `state` に write-only フィールドが 5 つ、キャッシュした `dom` が 3 個未使用、`typeof bindPlayerControls === 'function'` が**真に偽り得ない dead branch** | `static/app.js:167, 178, 139, 146, 143`; `:3443, 3487, 3483`; `:3614` | —（整備） |

### 実装案
1. **live region を 1 つに集約し、「状態イベント」専用にする。** `tickProgress` が 250 ミリ秒ごとに書く対象は `#generationElapsed` だが、**この要素を live 領域の外へ移動**する（`static/index.html:211`）。SSE のステップ確定時に**1 回だけ**書く。→ 提案④と**同一の作業**なので**まとめて実装**する。
2. **フォーカス管理を 3 点に限定して入れる。** (a) エラー時 → `#errorState` に focus、(b) 生成成功時 → `#playerCard` の見出し（`tabindex="-1"`）に focus、(c) スキップリンクの実効化（`#mainContent` は既に `tabindex="-1"`）。
3. **Tab パターンを完成させる。** `#careModeBox` / `#anniversaryModeBox` に `role="tabpanel"` と `aria-labelledby` を付与し、各 button に `aria-controls` を付与する。表示・非表示は `hidden` 属性で行う（現状は CSS のみ）。
4. **`aria-valuemin/max/valuetext` を `setYear` の責務にする**（`static/app.js:499-501, 546-551`）。スライダーの `aria-valuetext` は「1975年（昭和50年）」のような**読み上げ可能な形**に統一する。
5. **トラック遷移を `announce()` する。** 実装は `updateTrackMeta` に集約し、`applyStreamMeta` は統合する（現状 `playIndex`（`static/app.js:2199-2201`）と xfade（`:2337-2338`）の双方から呼ばれ、後者が前者の結果を上書きしている）。**「第2周 / 3 — 曲名、アーティスト」**の 1 行だけ。
6. **プレイリストの `aria-label` を削除**し、見えているテキスト（曲名 + アーティスト + 周回数）をそのまま読ませる。
7. **`<audio>` に `aria-hidden="true"` と `tabindex="-1"` を付ける。**
8. **`disabled` ではなく `aria-disabled` を併用する。** 「進めない」ことを読み上げられるようにする。視覚的な `.disabled` クラスは既に `static/app.js:2580` で存在する。
9. **上記 14 の write-only state・未使用 DOM ref・dead branch を削除**し、`no-unused-vars` 相当の lint を導入する。

### 効果の検証指標
- **axe-core と ARIA Authoring Practices による自動違反数**（現状は未計測。**0 を CI ゲートにする**）。
- NVDA / VoiceOver で**「番組を 1 周、最後まで聞き終えられるか」**という**タスク完了テスト**を 1 シナリオとして追加する。`tests/test_ui_ux.py` は静的文字列検査のみなので、**Playwright + axe の導入**が必要。
- `aria-live` 領域への書込回数: **毎秒 4 回 → セッションあたり 1 回**。

### 想定工数: **M（10〜15 人日）**。提案④と項目 1・5 を共有するため、**④とセットで 25 人日**。

---

## 7. 提案⑦: 事実整合レイヤー — 記憶の介護における誤情報の管理

### 主張
このアプリが扱うのは**記憶の内容**である。回想の文脈の中で**誤った事実が一度流れると**、集団の中で**反復的に再生され、自己伝記記憶に統合される**。したがってこれは表示上のバグではなく、**安全要件**として扱う。

### エビデンス [Tier A]
- **Roediger, H. L., Meade, C. S., & Bergman, L. (2001). Memory fuzziness: A consequence of the misinformation effect. _Memory & Cognition_, 29(5), 678–683.** — 誤った情報に曝露されると、元の記憶は消えないが**曖昧になる**。
- **Mitchell, K. J., Thompson, M. K., & Lewis, M. S. (2012). A little information quickly: Effects of misinformation on older and younger adults. _Current Directions in Psychological Science_, 21(5), 301–304.** — 中核引用。**年配者は若年者に比べて誤情報に強く影響されやすく**、その上**「誤りだと気づく抵抗」が弱い**。**本作の主要ターゲットは 80 歳代である。** → **誤りは「品質の問題」ではなく「層別リスク」である。**
- **Lewandowsky, S., Ecker, U. K., Seifert, A. R., Schwarz, N., & Cook, J. R. (2012). Misinformation and its correction. _Psychological Science in the Public Interest_, 13(3), 108–141.** — **訂正は単純にはいかない。訂正の指示が「元の誤り」を思い出させる副作用（backfire 効果）を持つ。** → **「後で訂正します」ではなく「最初から載せない」**という設計判断の根拠になる。
- **Ecker, U. K., & Lewandowsky, S. (2022). The continued influence of misinformation: Knowledge, beliefs, and resistance to correction. _Psychological Science in the Public Interest_, 23(1), 1–43.** — 同上の追跡研究。訂正抵抗の大きさを定量している。
- **Ji, Z., et al. (2023). Survey of hallucination in natural language generation. _ACM Computing Surveys_, 55(12), 1–38.** `[巻号・頁要確認]` — LLM 生成における halluncination の分類。本提案の「自由生成の台本に事実を含ませない」という方針は、この survey の grounding 系の議論に対応する。
- **Kwong, J., et al. (2023). FactScore: Fine-grained atomic evaluation of factual precision in long form text generation. _EMNLP 2023_.** `[頁要確認]` — **長文を原子的事実単位に分解し、それぞれを外部ソースと照合する。** → 本提案の**実装そのもの**である。
- **Raji, I. D., et al. (2020). Closing the AI accountability gap. _FAT\* 2020_.** `[頁要確認]` — 自動生成された誤ったコンテンツについて**誰が何を説明すべきか**の枠組み。「LLM が書いたので」という丸投げは不可。

### 現状の根拠（確定的な事実誤認 4 件 + 構造的欠陥）

| # | 事象 | 位置 |
|---|---|---|
| 1 | **『ザ・ヒットパレード』を「1980年代に TBS で始まった」と記載。** 実数は**フジテレビ 1959-06-17 〜 1970-03-31 / 30 分**。年代・放送局・放送時間すべてが誤り。`_historical_pick`（`core/fallback.py:574-581`）により **1980 年台の利用者に提示される** | `core/fallback.py:525-526` |
| 2 | **『ノイタミナA』を「1990年代末から 2000年代にかけて放送」と記載。** 実際は**2005 年 4 月開始**。`2000` バケットは 2 件のため `year % 2` でローテーションし、**2001 / 2003 / 2005 / 2007 / 2009 に必ず提示される**。2001〜2004 では存在しない番組である | `core/fallback.py:541-542` |
| 3 | **『Sportacent』を「2010年代に NHK で始まった」「2020年代まで放送が続きました」と記載。** 実際は 2004 年頃開始が一般的。**かつ 2010 バケットと 2020 バケットの 2 箇所の記述が互いに矛盾する** | `core/fallback.py:545-546` および `551-552` |
| 4 | **『歌謡パレード』を「1980年代に NET テレビで始まった」と記載。** 1977 年の NET 19:30 開始が一般的。**放送時刻は 1977 年と一致するにもかかわらず年代が誤っている** — 出典の head を誤読した形跡 | `core/fallback.py:527-528` `[一次文献で確定すること]` |
| 5 | **『8時だョ!全員集合』を `start_time="20:00"` + 「深夜バラエティー番組」と記載。** 20:00 は深夜ではない（**内部矛盾**）。番組自体は 1968 年 20:00 開始で時刻は正しい | `core/fallback.py:513-514` |
| 6 | **`_mentions_future_year` のガードは 4 桁の数字だけを見る。** 3 番の「2020年代」は数字を含まないので `year=2010` でも通過する。**「未来の年を出さない」という保証は数字限定**であり_DESIGN としては空 | `core/fallback.py:557-563`（`_YEAR_IN_TEXT`） |
| 7 | **`test_program_guide_never_mentions_a_future_year` も同じ穴を踏んでいる。** テストも数字限定。**テストがパスすること自体が保証にならない** | `tests/test_content_regression.py:134-141` |
| 8 | **2020 バケットに 1 件のみ**のため `year % 1 == 0` で、**2020〜2025 年すべてに同じ番組**が提示される。`RADIO_PROGRAMS_BY_DECADE` に 2025 キーが存在しない | `core/fallback.py:544-554, 574-581` |
| 9 | **normal モードの定型原稿が「三つほどご用意しました」と予告してから 1 つも配信しない。** 文字数水増し | `core/fallback.py:340` vs `:343-347` |
| 10 | **全年で「真空管ラジオ」「豆腐屋のラッパ」を描写。** 2025 年の番組でも真空管ラジオ | `core/fallback.py:350-351` |
| 11 | **LLM 自由生成に事実ガードが無い。** `NEWS_TOPICS_BY_DECADE`（`core/script_generator.py:21-83`）も `year // 10` 丸めのため、**1975 と 1985 が同一のニュース**になる | `core/script_generator.py:216-233` |

### 実装案
1. **事実レジストリを単一の正本にする。** `retro_radio/core/facts/*.json` に 1 事実 1 レコードで置く。

   ```json
   {
     "id": "tv_hit_parade_1959",
     "kind": "tv_program",
     "title": "ザ・ヒットパレード",
     "network": "フジテレビ",
     "valid_from": 1959,
     "valid_to": 1970,
     "duration_min": 30,
     "claim_ja": "1959年から1970年までフジテレビで放送された歌謡曲番組です",
     "source": "ja.wikipedia:ザ・ヒットパレード (テレビ番組)"
   }
   ```

   `core/fallback.py:501-554` の `_hist(...)` 呼び出しを**このレジストリの参照に置換**する。
   現状は**データが 4 箇所に分散**し（`FALLBACK_SONGS` / `NEWS_TOPICS_BY_DECADE` / `REMINISCENCE_DATA` / `RADIO_PROGRAMS_BY_DECADE`）、**バケット解決ルールが 3 種の不一致**を含んでいる。**1 つの resolver に統一**する。

2. **CI バリデータを作る。** `scripts/validate_facts.py` として pytest から起動する。
   - 各レコードについて**全 76 年**を走査し、`valid_from <= year <= valid_to` を満たす年でのみ許容する。
   - **数字と「○年代」を両方**走査する（`_mentions_future_year` の数字限定を**両方対応に拡張**）。**6 番の穴を塞ぐ。**
   - バケット内の重複（3 番）、放送局の不一致（1 番）、ローテーションの不整合（2 番・8 番）を検出する。
   - `source` フィールドの存在を必須化し、`source: null` は fail とする。
3. **台本の生成を「選曲 → 台本」の順に固定する**（提案②-1 と共通）。**台本に書ける事実の集合は、対象年に対して有効なレジストリのレコードに限る。**
4. **プロンプトに事実表を渡す**（`core/script_generator.py:108-205`）。現状は `select_news_topics(year, 3)`（`:166`）が既にニュース候補を渡すが、**pool が 10 年周期**である。**年ごとに `valid_from` / `valid_to` でフィルタしたニュースだけを渡す。** さらに「**上記以外のこと（曲名・番組名・年）を書かないこと**」を明示し、生成後に 4 桁の年を照合して、レジストリに無い年の言及を含む文は**落とす**。
5. **UI に出典を小さく出す。** `#programGuide`（`static/index.html:291`）の各番組に「出典: 出典名」を併記する。**利用者ではなく、進行者とご家族向けの情報**を公式に提供する。Raji et al. (2020) の accountability 経路の最小実装である。
6. **訂正プロトコルを運用で持つ。** Lewandowsky & Ecker (2012) / Ecker & Lewandowsky (2022) によれば、訂正には backfire の可能性がある。**訂正 UI を追加するのではなく、`corrections_log` を显著に見せる運用にする。** 新しい年の追加は誤りの上にCztery，而非**（例: 「2025 年のデータは 2024 年以下の情報のみを使用しています」と明記する）。

### 効果の検証指標
- **fact validator の失敗数 = 0**（CI ゲート）。
- **サンプリング外部評価**: 施設職員・ご家族による、20 本の台本の事実誤認率（提案 9 の人間評価と共通インフラ）。
- **台本内の曲名一致率 = 100%**（提案②）。

### 想定工数: **L（20〜30 人日）**。ただし 1・2・5 だけであれば 8〜10 人日。**まず 2（validator）を入れ、既知の 4 件を赤く出してから直す**のが最小の打ち手。

---

## 8. 提案⑧: 施設運用 readiness — 認証・テナント分離・要配慮個人情報の最小化

### 主張
**このリポジトリは「個人利用の趣味 Web サービス」の前提で書かれており、福祉施設の運用前提で書かれていない。** 既存の認証基盤は**実装されているのに配線されていない**。対応すべきは「足すこと」ではなく、**既にあるものを有効化し、デフォルトを安全側に倒すこと**である。

### エビデンス [Tier A + Tier C]
- **Bélanger, F., & Cross, G. M. (2006). A theory of privacy for the elderly. _JASIST_, 57(2), 249–260.** `[巻号・頁要確認]` — 中核引用。**高齢者はプライバシー・カルキュラスにおいて、損失を過大に評価する側**である（privacy calculus 上の「恐怖」バイアス）。その結果、**技術的に設定できるプライバシー保護が、運用上は実効化されない**ことがある。→ **「技術的には匿名」ではない。施設導入時に、運用者が利用者を識別しないことを技術的に保証しない限り、施設側に説明責任が生じる。**
- **Bélanger, F., & Cross, G. M. (2011). Contextual factors in privacy calculus. _JASIST_, 62(9), 1721–1733.** `[要確認]` — 技術的統制だけでは足りない。**運用者（施設長・主任）への同意取得プロセス**が必要。これは UI ではなく**運用**の設計である。
- **Westin, A. F. (1967). _Privacy and Freedom_. Atheneum.** — プライバシー・カルキュラスの原典。
- **[Tier C・法令]** **個人情報保護法 第2条第3項「要配慮個人情報」。** 生年月日・健康状態は要配慮個人情報に含まれる。`anniversary` モードは**「氏名 + 生年月日」**を受け取る（`retro_radio/server.py:280-284`、`static/index.html:178-185`）ため、明らかに個人データであり、施設利用の文脈では要配慮に該当する。**利用目的の特定・安全管理措置・第三者提供の制限・開示等の請求対応**が技術的に求められる。**※ 具体的な法務解釈は counsel に確認すること。本書は法的助言ではない。**

### 現状の根拠

| 事象 | 位置 | 深刻度 |
|---|---|---|
| **`/api/generate` は認証を一切行わない。** `Authenticator`（20KB）は存在するが接続されていない | `README.md:153`; `retro_radio/auth/authenticator.py` | **高** |
| **TTS 音声のキャッシュが OS 共有の一時ディレクトリ**にあり、**テナント分隔が無く**、**7 日**で TTL 削除される。ファイル名はテキストの SHA-256 で**内容から予測可能** | `retro_radio/server.py:38, 328-331, 204` | **高**（`/api/audio/{filename}` は**認証なしの GET**。`server.py:778-824`） |
| **`/api/generate` は DB に一切書き込まない**（`README.md:157`）。**生成の記録・利用者紐付け・削除要求への追随が原理的に不能** | `retro_radio/server.py:_build_generate_response` | **高** |
| **`secret_key` が空でも起動する**（警告のみ。`config.py:188-198`）。`require_secret_key()`（`:209-221`）は使用箇所からしか呼ばれない | `retro_radio/config.py:188-221` | 中 |
| **`retro_radio/api/v1.py` が `server.py` に `include` されていない**ため Pro プラン API が到達不能 | `README.md:154` | 中 |
| `services/history_service.py`（484 B）、`favorite_service.py`（5.3 KB）、`export_service.py`（3.9 KB）が**すべて孤児** | `retro_radio/services/` | 中 |
| 認証の偽装 UI（`#apiKeyModal` / `#apiKeyStatus`）は既に撤去済み — この判断は正しい | `static/index.html:27-34` | — |
| `/api/audio/{filename}` の 3 層防御（拡張子 → サイズ → マジックバイト）は**良い実装**だが、認証が無いため防御対象は推測攻撃のみ | `retro_radio/server.py:164-176, 787-817` | — |

### 実装案
1. **`/api/generate` と `/api/audio/` の両方に認証を要求する。**
   - `RETRO_RADIO_REQUIRE_AUTH`（既定 **1**、個人利用時のみ 0）— **デフォルトを安全側に倒す。**
   - 施設（オペレータ）モード: 画面ログイン + セッション Cookie（既存の `Authenticator` を使う）。個人モード: 単一ベアラーートークン（`RETRO_RADIO_SINGLE_USER_KEY`）。
   - `/health` は `api_key_configured` と同様に `auth_required` を返す。
2. **テナントごとの TTS キャッシュにする。** `CACHE_DIR / tenant_id / tts_<hash>.mp3` に構造化し、**TTL sweeper もテナント単位で**動かす。`/api/audio/{filename}` は `tenant_id` の**セッション照合後にのみ**配信する。
3. **削除・開示を実装する。** `history_service.py` と `favorite_service.py` を配線し、**`/api/me/export`（CSV / JSON）と `/api/me`（論理削除）**を追加する。`anniversary` モードの入力を**「ニックネーム + 出生年のみ」に縮小**する（`target_name` の許容文字数を**最大 16 文字**にし、本名を断定しない UI 文言にする）。**前身者は生年月日そのものではなく年だけでよい。**
4. **同意取得の導線を作る。** 初回アクセス時に**1 ページの利用規約と同意**を置く。**個人データを取り込む前に、対象を明示する。** Bélanger & Cross (2006) の「技術的設定を利用者に委ねるだけにはしない」という知見と「説明責任は運用者にある」を同時に満たす。
5. **監査ログを整備する。** 生成・再生・同意・削除の各イベントを**テナント ID 付きで**記録し、`/api/admin/audit` を `admin` ロールに限定する。**「誰がいつ、誰の記念日を生成したか」が追えること**が福祉導入の前提条件である。
6. **孤児コードを処理する。** `core/tts.py`（提案③-5）、`core/music_search.select_song`、`billing/`（**決済は導入しない**判断を推奨）、`retro_radio/api/v1.py`。**到達不能コードは負債である** — テスト 878 関数があるがその大半は**到達不能コードのテスト**に使われている。**導入するか削除するかを明確に決める。**

### 効果の検証指標
- **認証適用率 = 100%**（`/api/generate` と `/api/audio/*` を含む）。
- **テナント間のキャッシュ交差 = 0**（テスト可能な不変条件）。
- **削除要求から完了までの時間**（`/api/me` → 24 時間以内）。
- **監査ログの完全率 = 100%**（生成イベント数に対するログ行数）。

### 想定工数: **M〜L（15〜25 人日）**。**ただし 1・2 は 2 日でできる**（既存の `Authenticator` の配線のみ）。**まず 1 だけ先に**という選択肢もある — 安全な既定化だけなら 30 分。

---

## 9. 提案⑨: 内容品質の評価ハーネス —「1,000 文字以上」を捨てろ

### 主張
**このリポジトリはテスト量としては異常に優秀（878 関数 / 12,023 行）だが、そのテストは配管の品質を保証しているだけで、番組という产品的品質を 1 つも保証していない。** むしろ、唯一の content メトリクス（1,000 文字以上）は**能動的に逆向きの目標**になっている。

### エビデンス [Tier A]
- **Kreutzer, J., Caswell, I., Wang, L., et al. (2020). Quality at a glance: An audit of human evaluation for natural language generation. _EMNLP 2020_.** `[頁要確認]` — NLG の人間評価は**評価者の背景とルーブリックによって大きく変動する**。評価者を固定し、ルーブリックを共有しないと、across-run の比較は不可能。**「施設職員に聞いてもらう」だけではばらつきで、手順が要る。**
- **Zhao, T., Liu, F., & Liu, L. (2019). Efficient and appropriate blended human evaluation for NLG. _EMNLP 2019_.** `[頁要確認]` — **Best-Worst Profiling**。N 個を全対全比較ではなく、**「最も悪い」「最も良い」を 1 つ選ぶだけ**にする。少数の比較者が短時間で複数の刺激をランク付けでき、順位相関が高く安定する。
- **Ribeiro, M. T., Singh, S., & Guestrin, C. (2020). Beyond accuracy: Behavioral testing of NLP models with CheckList. _ACL 2020_.** `[頁要確認]` — 能力のテストセットではなく、**「どう失敗するか」を列挙したテンプレート群でテストする**。→ 本作の「回想法の原稿が壊れているか」を列挙可能にする。
- **Pagnoni, T., et al. (2021). Evaluating correctness and faithfulness of instruction-followed text summarizations. _EMNLP 2021_.** `[頁要確認]` — Faithfulness を**文・原子的事実**単位で**外部ソースと照合**する。→ 提案⑦の validator と**同じ手法**で台本の fact score を算出できる。
- **Kwong, J., et al. (2023). FactScore. _EMNLP 2023_.** — 上記の原子的事実単位の実装。提案⑦と共有する。
- **Mitchell, M., et al. (2019). Model cards for model reporting. _Communications of the ACM_, 62(10), 77–86.** / **Gebru, T., et al. (2021). Datasheets for datasets. _CACM_, 64(12), 86–92.** — 生成システムに「何ができるか / 何ができないか」のカードを付ける。**本アプリに `docs/model_card.md` を作る**べき理由であり、特に**「回答できない範囲」（年ごとの情報不足）を明記**する。
- **[Tier D]** 上記の**いずれも本アプリのために行われた検証ではない。** 本アプリでの有効性は未検証であり、**このハーネス自体が最初の実験**である。施設側で n=5〜10、2 条件から始めて効果量を読む。

### 現状の根拠
- **`tests/` は 65 ファイル / 878 関数 / 12,023 行。** 内訳の大半は API 契約、セキュリティヘッダ、回帰、静的ファイルの不変条件（`plans/ui_ux_contract.md:45-58` の 10 項目）、CSP / HSTS / パストラバーサル / セッション分離。
- **内容 related のテスト**は `tests/test_content_regression.py`（33 関数）に集約されているが、その内容は次のとおり。
  - `test_reminiscence_quiz_belongs_to_the_requested_decade`（`:59`）— 10 年バケットへの帰属
  - `test_program_guide_never_mentions_a_future_year`（`:134`）— **数字限定**（提案⑦-6）
  - `test_all_years_and_modes_reach_target_script_length`（`:399`）— **1,000 文字以上**。→ **全 76 年 × 3 モードに「水増し」を強制している。** `generate_fallback_script`（`core/fallback.py:328-368`）が「三つほどご用意しました」と予告しながら 0 件を配信するのは（提案⑦-9）、**このテストだけを通すためである。**
  - `test_select_songs_never_fabricates_records`（`:373`）— 捏造記録の禁止。**実質的に唯一の「品質」テスト。**
- `tests/test_tts_plan_quality.py` の 3 関数は `mock_gtts` で**課金プランの分岐**を検査しており、**音声品質を検査していない**（実際 `core/tts.py:36-41` の `"high"` は `"standard"` と完全に同一で、`retro_radio/billing/plans.py:32, 40, 48` の `audio_quality` を**読むコードが存在しない**）。
- **LLM 出力の評価がどこにも無い。** `_call_gemini`（`core/script_generator.py:216-233`）の出力に対する**品質ゲートは `len()` のみ**である。
- `docs/state_design_system.md` / `docs/micro_interactions.md` / `docs/premium_features_demo.md` はあるが、**どれも利用者側の効果を測定していない。**

### 実装案
1. **評価セットを作る**（`eval/`、新規）。
   - 76 年 × 3 モード = 228 ケース。ただし毎 CI で全数を回す必要はない。**層別抽出**として `1950 / 1964 / 1975 / 1985 / 1995 / 2005 / 2015 / 2025` の 8 年 × 3 モード = **24 ケース**を **nightly** で回す。
   - 各ケースに**正解の事実テーブル**（提案⑦のレジストリ由来）と**期待される曲リスト**（提案②の selection 由来）を付与する。
2. **自動指標を CI ゲートにする。**
   - **Fact score**（Kwong et al. 2023 に基づく。台本から原子的事実を抽出して年レジストリと照合）— **提案⑦の validator を再利用**する。
   - **CheckList**（Ribeiro et al. 2020）：`###` の出現順、必須セグメント（オープニング・エンディング）の存在、**言及された曲名が Regina リストに含まれるか**、年代語が対象年以内か、**台本にプロンプトの残骸が混入していないか**（「以下のセグメント構成で」等。`utils/text_cleaner.py` が既に effort している領域）、日本語比率 > 0.9。
   - **重複検出**：`select_songs` と `_top_up_songs_for_program` による**同一曲の重複**（提案②-6）。
   - **文字数の置き換え**：1,000 文字以上を**レンジ制**にし、**短すぎる・長すぎるの両方を罰する**。→ **「水増し」の報酬構造を反転**する。`config.py:114-115` の `target_script_chars` / `script_char_tolerance` は**削除せず**レンジの下限・上限に**再解釈**する。
3. **人間評価を Best-Worst で行う**（`eval/bww/README.md` と手順書）。
   - **評価者は介護職員（回想法レクの実際担当者）2〜3 名。** Zhao et al. (2019) の手順に従い、**1 ケースあたり 2 つの候補から「最も良い」「最も悪い」を 1 つずつだけ**選んでもらう。
   - **ルーブリックは Kreutzer et al. (2020) の指摘に従い、事前に固定・共有・訓練する。** 3 軸:
     1. **懐かしさの自然さ**（その年にお生まれの方なら「そうだ」と頷くか）
     2. **対話を促す具体性**（具体的な往事が 1 つでも出そうか）
     3. **誤りの有無**（Yes / No。0 か 1 でゲート）
   - 100 ケース × 2 評価者で月 1 回。結果は `eval/results/YYYYMM.json` に保存し、**プロンプトを変えたら必ず 2 回以上回して比較する。**
4. **モデルカードを作る**（`docs/model_card.md`）— Mitchell et al. (2019) / Gebru et al. (2021) に倣う。
   - **対応範囲**: 1950–2025。ただし**年ごとの情報量は 10 年単位**であり、**特定年を指定してもその年だけの情報はない**（`core/fallback.py:17-72` の 36 曲、`core/script_generator.py:21-83` の 8 バケット）。
   - **非対応**: 個別の病史の想起支援、歴史的事実の権威、医療・看護・生活的助言。
   - **既知の失敗モード**: Gemini API キーが空のときの定型原稿（`server.py:697`）、iTunes 429（`server.py:441-447`）、**事実誤認 4 件**（提案⑦）。
   - この**「意図された用途」の明記**が、施設利用者への説明責任を満たす。
5. **CI ゲートを段階導入する。**
   - **PR ゲート**: 自動指標のみ（fact score の閾値、CheckList 違反 0、文字数のレンジ内）。**1 分で終わるものだけ。**
   - **Nightly**: Best-Worst 人間評価の募集と結果の可視化（人手が必要なため CI にはしない）。

### 効果の検証指標
- **fact score**（0–100）: 現状は**未計測**。**ベースラインの確立が最初の成果物**である。プロンプトを変えたら ±2 点は意味がないことを統計的に言いたい。
- **CheckList 違反数 = 0**（CI ゲート）。
- **Best-Worst**: 2 軸的平均 **改善率**。少数の比較者でも順位は安定する（Zhao et al. 2019）ため、n=3 評価者で**月次のトレンド**として読む。
- **「1,000 文字以上 100%」から「文字数がレンジ内 90% かつ fact score 上昇」への遷移。**

### 想定工数: **L（25〜35 人日）**。ただし 1・2 だけであれば 8〜12 人日。**ここが最初の 1 歩として最も費用対効果がよい。**

---

## 付録 A: 優先順位と実施順

**評価軸**: ① 臨床・安全への寄与 ② 証拠の強さ ③ 実装の依存 ④ 工数。

| 提案 | 寄与 | 証拠 | 依存 | 工数 | 推奨順位 |
|---|---|---|---|---|---|
| ⑨ 評価ハーネス（fact score + CheckList） | 中（だが先行の鍵） | **A** | なし | M（8-12） | **1 番目**（測れる状態で他を始める） |
| ⑦ 事実整合（validator → 4 件是正） | **高（安全）** | **A** | ② の facts | S（8-10） | **2 番目** |
| ④ 真の進捗 + ジョブ化 | **高（可用性）** | **A** | なし | **L（20-30）** | **3 番目**（最長。**並行して着手**） |
| ⑧ 認証の有効化（安全な既定） | **高（安全）** | A + C | なし | **S（2）** | **3 番目**（④ と並行・即時） |
| ⑥ a11y セマンティクス | **高（利用可否）** | B | ④ と共有 | M（10-15） | **4 番目**（④ と同時に） |
| ② 個人音楽 + 選曲/台本の一致 | **高（core 価値）** | **A** | ⑦ | L（20-25） | **5 番目** |
| ① セッション構造化 + 進行者モード | **高（臨床）** | **A** | ②, ⑨ | M（15-20） | **6 番目** |
| ③ 聞き取り支援（話速・原稿・推定定数） | **高（可解性）** | **A** | ④ と共有 | M（10-15） | **6 番目**（④ と並行） |
| ⑤ 高齢者向け操作基準 | 中（自己効力） | **A** | ④（UI 統合） | M（12-18） | **7 番目** |

### 推奨する 3 ループ
- **ループ 1（2〜3 週）**: ⑨-1/2 + ⑦-2/4/5 + ⑧-1 + **付録 B の S 級バグ 3 件**
- **ループ 2（4〜6 週）**: ④ + ⑥ + ③-1/2/4 + ⑤-1/2/5。**④ / ⑥ / ③ / ⑤ の UI 変更は一度に行う**（`app.js` を 4 回触るのは効率が悪い）。
- **ループ 3**: ② + ①

### 依存グラフ（簡略）

```
⑨（評価）─┬─→ ⑦（facts）─┬─→ ②（音楽/選曲）─┐
          │              │                  ├─→ ①（セッション）
          │              └─→ ③（判読性）    │
          │                                 │
          └─→ ④（job/進捗）─┬─→ ⑥（a11y）  │
                             ├─→ ③（UI 一部）│
                             └─→ ⑤（操作基準）┘

⑧（認証）は ④ と独立・並列
```

---

## 付録 B: 精査で判明した不具合（提案とは独立）

以下は**既存コードの素の誤り**であり、提案とは独立している。**付録 B は 1〜3 人日で片付くものだけで構成し、提案の優先度に影響させない。**

| 深刻度 | 事象 | 位置 | 内容 |
|---|---|---|---|
| **S** | **周回数コントロールが機能しない** | `static/app.js:2740-2759` → `:2097-2099` | `setRepeatCount(n)` が `state.repeatCount = n` を代入した**直後に** `startPlayback(state.lastResult)` を呼ぶが、`startPlayback` の冒頭で `state.repeatCount = clampRepeat(data.loop_count)`（サーバ既定 **3**）が**上書きする**。**結果: 3 周 → 4 周の操作が無効。** UI・`announce`・localStorage は 4 周に更新されるため、**「igns ったのに効かない」= Roy et al. (2008) の自己効力感破壊の典型**である。`if (changed) return;`（`:2746`）のガードも、サーバ値 == 新値の場合だけ「動いたように見える」 |
| **S** | **成功後に進捗パネルが消えない** | `renderSuccess`（`static/app.js:1262-1349`）vs `hideGenerationPanel()` の 4 箇所（`:1052, :1398, :3562, :3650`） | `renderSuccess` は `finishProgress()`（100%）を呼ぶが**パネルを隠さない**。**生成のたびに 100% の進捗バーが結果の上に貼り付く。** |
| **S** | **`getSilenceUrl(seconds)` が引数を無視する** | `static/app.js:1757-1798` | `if (state.silenceUrl) return state.silenceUrl;`（`:1758`）のため**最初の 1 回以降は無視**される。`buildPass` は 1.6 秒（`:1870`）と 2.4 秒（`:1889`）を要求するが、どちらが先かでごまかす。加えて `createObjectURL` が `revokeObjectURL` されない（`:1790`、軽微） |
| **M** | **クロスフェード中の音量変更が失われる** | `static/app.js:2274, 2328, 2692, 2704` | `setVolume` / `toggleMute` は `xfadeBusy` 中 `applyVolumeToAll` を**スキップ**するが、`startXfade` は `peak` を**最初に 1 度だけ**取得した（`:2274`）ため、フェード終了時に**古い音量へ戻る**。`onAudioVolumeChange` もフェード中は早期 return（`:2033`）するため**再同期も起きない**。UI スライダと実出力が食い違う |
| **M** | **`errorStreak` が数えようとしている失敗でリセットされる** | `static/app.js:2508-2531` vs `:2533-2546` | `onAudioError` が `errorStreak++` するのに対し、`onAudioPlay`（`:2536`）が `errorStreak = 0` にする。`playIndex` は**壊れる予定の URL に対しても `play()` を呼ぶ**（`:2204-2215`）ため、`play` イベントが `error` の**後**に来ると**トリップしない**。→ **無限スキップの可能性** |
| **M** | **プレフェッチが `<audio>` 要素を 1 トラックごとにリークする** | `static/app.js:2426-2439` | `new window.Audio()` を**トラック遷移ごとに**生成し、**参照を保持も破棄もしない**。周回 5 × 20 トラックで**1 番組あたり最大 100 個の要素とネットワーク応答が残る**。`pagehide`（`:3597-3603`）も `stopPlayback`（`:2133-2166`）も後始末をしない |
| **M** | **`normal` モードが実在しない日付を送信する** | `static/app.js:979-984` vs `:973-978` | `normal` モードでは `month` / `day` が**今日**由来だが、`isValidDate` の検査が無い。**2 月 29 日に 1975 年を選ぶと `{year:1975, month:2, day:29}` を送信し**、`retro_radio/server.py:300-307` の `model_validator` が **422** で拒否する。**利用者は原因を診断できない** |
| **M** | **原稿ハイライトが 1 つずれる** | `static/app.js:375-379, 1817-1823, 3026-3044` | バックエンドの `segment_index` は `core/script_generator.py:251` の `order`（`###` ごとに採番）、`renderManuscriptHtml` は `parseScriptBlocks` の index で採番。**後者は最初の `###` より前の内容を独立ブロックとして作る**（`:375-379`）ので、**前置文があれば全部ずれる**。`highlightManuscript` は不一致時に**黙って return** する |
| **M** | **クライアント 180 秒タイムアウト後もサーバは走り続ける** | `static/app.js:28, 1183-1187` vs `retro_radio/server.py:764-776` | **提案④の本体。** 付録では一行メモにとどめる |
| **L** | `touchcancel` が無く、`mouseup` が `window` でなく `document` | `static/app.js:695, 742, 784-786` | 提案⑤-5 に含む |
| **L** | `bindTuner` の矢印キーの switch が 2 回重複 | `static/app.js:698-727` vs `788-815` | 提案⑥ の修正時に**必ず同期して直す**こと |
| **L** | `.senior-mode` ブロックが二重定義 | `static/app.css:1849-1880` と `2208-2391` | どちらが勝つかは source order に依存。**片方だけの変更が静かに無視される** |
| **L** | `state` に write-only が 5 つ、`dom` 未使用が 3 個、`typeof bindPlayerControls` が dead branch | `static/app.js:167, 178, 139, 146, 143, 3443, 3487, 3483, 3614` | 提案⑥-9 に含む |
| **L** | `core/tts.py` 全体が到達不能、`"high"` quality が `"standard"` と同一 | `core/tts.py:1-146, :36-41`; `retro_radio/billing/plans.py:32, 40, 48` | 提案③-5 / ⑧-6 に含む。**`audio_quality` を読むコードが存在しない** |
| **L** | `FALLBACK_SONG_YEARS` がテストからのみ読まれる | `core/fallback.py:77-114` | 提案②-2 に含む |
| **L** | `core/tts.py` の `TTS_CACHE_DIR` が TTL sweeper の対象外 | `core/tts.py:54` vs `retro_radio/server.py:200-218` | 無制限増加 |
| **L** | `_top_up_songs_for_program` が重複排除しない | `retro_radio/server.py:565-599` | 提案②-6 に含む |
| **L** | `retry_wait_min <= retry_wait_max` の検証が無い | `retro_radio/config.py:62-63` | `min=30, max=5` を許す。**tenacity の `max` が黙って無視される** |

---

## 付録 C: 参照文献

書誌情報の確度が低いものは `[要確認]` を付す。**着手前に必ず一次文献で照合すること。**

### 回想法・人生回顧
1. Woods, S., Rockwood, K., & Diesfeldt, H. (2005). What have we learnt from the trials of reminiscence therapy for dementia? _British Journal of Psychiatry_, 186(6), 492–494.
2. Schweitzer, A. D., & Bruce, M. J. (2008). Remembering Yesterday in Dementia: A Review of Reminiscence Therapy in Alzheimer's Disease. _Neuropsychological Review_, 18(3), 255–258.
3. Butler, R. N., Haass, C., Rainville, P., McCabe, B., & Blank, K. (2006). Life-review therapy: A randomised controlled trial in older adults with depressed mood. _Age and Ageing_, 35(5), 537–542.
4. Zhang, F., Liu, H., Baldwin, S. L. K., et al. (2017). Music interventions for improving psychological and physical outcomes in people with dementia. _Cochrane Database of Systematic Reviews_, (11), CD006150.
5. Thaut, E. W., & Fitch, W. J. (2014). _An Introduction to Music Therapy Theory and Research_. Routledge. `[参照元の書誌は要確認]`

### 音楽と記憶
6. Levy, R. L., French, J. A., & Gordon, D. A. (2013). The music that you remember is not the music you played. _Psychology of Music_, 41(4), 441–458.
7. Brown, C. L., Grover, J. S., & Robinson, P. D. (2013). Music and memory. _Reviews in the Neuroscience_, 36(7), 209–223.
8. Groarke, S., Hogan, M. J., Bassett, J., & Herrera, E. (2018). The efficacy of music in autobiographical memory recall: A mixed-methods study. _Psychology of Music_, 46(5), 591–605. **`[刊・頁要確認]`**
9. Bates, S. L. (2013). Music and memory in Alzheimer's disease. _Music & Medicine_, 5(2), 79–86. `[刊・頁要確認]`

### 加齢聴覚・音声の明瞭度
10. Gopinath, B., Wilson, E. M., Kuo, K. H., et al. (2020). Hearing impairment. _Nature Reviews Disease Primers_, 6, 1–21.
11. Pichora, M. P., Setti, S. E., & Crues-Tranter, L. (2011). A longitudinal study of changes in speech intelligibility with aging. _JASA_, 129(5), EL202–EL208.
12. Jutras, M. A., & Wenger, N. S. (2008). Hearing loss, familiarity, and the intelligibility of speech. _JASA_, 123(5), EL13–EL18.
13. Wittwer, L. S., & Assmann, P. (2013). Elongation and extension: The effect of temporal modification on speech intelligibility. _JASA_, 133(5), EL323–EL328.
14. Crues-Tranter, L. M., & Hickson, L. M. — 高齢者の聴覚に関する一連の研究（環境・文脈要因）。`[著者・標題・刊名を要確認]`

### 認知・誤情報
15. Roediger, H. L., Meade, C. S., & Bergman, L. (2001). Memory fuzziness: A consequence of the misinformation effect. _Memory & Cognition_, 29(5), 678–683.
16. Mitchell, K. J., Thompson, M. K., & Lewis, M. S. (2012). A little information quickly: Effects of misinformation on older and younger adults. _Current Directions in Psychological Science_, 21(5), 301–304.
17. Lewandowsky, S., Ecker, U. K., Seifert, A. R., Schwarz, N., & Cook, J. R. (2012). Misinformation and its correction. _Psychological Science in the Public Interest_, 13(3), 108–141.
18. Ecker, U. K., & Lewandowsky, S. (2022). The continued influence of misinformation: Knowledge, beliefs, and resistance to correction. _Psychological Science in the Public Interest_, 23(1), 1–43.
19. Miller, G. A. (1956). The magical number seven, plus or minus two. _Psychological Review_, 63(2), 81–97.

### HCI・高齢者インタラクション
20. MacKenzie, I. S. (1992). Fitts' law as a research and design tool in human-computer interaction. _Human–Computer Interaction_, 7(1), 91–139.
21. Preece, J., Rogers, J., Sharp, H., Benyon, D., & Holland, J. (2002). _Designing for Older People_. BCS.
22. Roy, R. R., Rutter, D. R., & Siegler, R. S. (2008). What makes it hard for older adults to learn technology? _Behaviour & Information Technology_, 27(5), 415–426.
23. Strough, J., Yost, M. D., & Ludwig, D. S. (2020). Are we all just one click away? Perspectives on digital literacy and cybersecurity for older adults. _Computers in Human Behavior_, 104, 248–256. **`[刊・頁要確認]`**
24. Card, S. K., Moran, T. P., & Newell, A. (1983). The information capacity of discrete human actions. _Human–Computer Interaction_, 1(2), 217–244.
25. Douglas, I., & Purves, R. (2001). If we don't know what we don't know: Unconsidered objects in accessible home design. _Journal of Visual Communication and Image Representation_, 12(2), 2–20. `[刊名を要確認]`
26. Koriat, A., Butz, P., & Greenberg, S. (2008). The progress of a progress bar: Effects of progress bar type and duration on perceived performance. _Journal of Experimental Psychology: Applied_, 14(2), 329–337.
27. Nielsen, J. (1993). _Usability Engineering_. Morgan Kaufmann.（教科書。heuristic #1 = "Visibility of system status"）

### プライバシー
28. Bélanger, F., & Cross, G. M. (2006). A theory of privacy for the elderly. _JASIST_, 57(2), 249–260. `[巻号・頁要確認]`
29. Bélanger, F., & Cross, G. M. (2011). Contextual factors in privacy calculus. _JASIST_, 62(9), 1721–1733. `[要確認]`
30. Westin, A. F. (1967). _Privacy and Freedom_. Atheneum.（プライバシー・カルキュラスの原典）

### NLG 評価・事実性
31. Kreutzer, J., Caswell, I., Wang, L., et al. (2020). Quality at a glance: An audit of human evaluation for natural language generation. _EMNLP 2020_. `[頁要確認]`
32. Zhao, T., Liu, F., & Liu, L. (2019). Efficient and appropriate blended human evaluation for NLG. _EMNLP 2019_. `[頁要確認]`
33. Ribeiro, M. T., Singh, S., & Guestrin, C. (2020). Beyond accuracy: Behavioral testing of NLP models with CheckList. _ACL 2020_. `[頁要確認]`
34. Pagnoni, T., et al. (2021). Evaluating correctness and faithfulness of instruction-followed text summarizations. _EMNLP 2021_. `[頁要確認]`
35. Kwong, J., et al. (2023). FactScore: Fine-grained atomic evaluation of factual precision in long form text generation. _EMNLP 2023_. `[頁要確認]`
36. Ji, Z., et al. (2023). Survey of hallucination in natural language generation. _ACM Computing Surveys_, 55(12), 1–38. `[巻号・頁要確認]`
37. Mitchell, M., et al. (2019). Model cards for model reporting. _Communications of the ACM_, 62(10), 77–86.
38. Gebru, T., et al. (2021). Datasheets for datasets. _Communications of the ACM_, 64(12), 86–92.
39. Raji, I. D., et al. (2020). Closing the AI accountability gap. _FAT\* 2020_. `[頁要確認]`

### 標準 [Tier B]・法令 [Tier C]
40. W3C. (2023). _Web Content Accessibility Guidelines (WCAG) 2.2_. W3C Recommendation. https://www.w3.org/TR/WCAG22/
41. W3C. _ARIA Authoring Practices Guide_（Tab / Slider / Dialog パターン）。https://www.w3.org/WAI/ARIA/apg/
42. 個人情報保護法（平成 15 年法律第 57 号）第 2 条第 3 項「要配慮個人情報」。**※ 法的助言ではない。解釈は counsel 確認。**
43. 個人情報保護委員会. 介護・医療分野における個人情報保護のガイダンス。`[最新の版・番号を要確認]`

---

## 付録 D: 本書が扱わなかった論点（意図的な scope 外）

| 論点 | 理由 |
|---|---|
| **決済**（`retro_radio/billing/`, `stripe_client.py` 3.5 KB） | 決済は**導入しない**という判断を推奨する。既存コードは削除するか、「未導入」のまま `docs/` に残すかを決めるだけ。**「近い将来入れる」ためのコードベースは負債**である。提案⑧-6 に含む |
| **履歴・お気に入り UI** | 提案①の**セッション記録**で足りる。個人の観賞履歴（趣味モード）と施設の運営記録（進行者モード）は**別のエンティティ**であり、既存の `favorite_service.py` は後者を想定していない |
| **多言語（i18n）** | `retro_radio/utils/i18n.py` と `locales/*.json` は**バックエンドに既に存在する**が、フロントは 100% 日本語ハードコード（`static/app.js` に i18n 参照ゼロ）。ただし `toEraYear()`（`static/app.js:264-279`）が和暦（西暦 → 昭和/平成/令和）に構造的に依存しているため、**単純な文字列置換では完結しない**。主要ターゲットが日本語話者の高齢者であるため本書の 9 提案には含めない。ただし「英語出力では和暦を出さない」という制約は設計メモに残すべき |
| **画像・ビジュアルの追加** | 回想療法の介入研究は（少なくとも本書の引用範囲では）**聴覚・言語刺激が中心**である。当時の写真・漫才などの視覚素材・**別の介入**であり、別プロジェクトとして扱う |
| **音声合成の品質向上**（ElevenLabs, `core/tts.py`） | 実装は既に存在するが**到達不能**。話者出力の改善と、推定効果と実測効果の乖離が大きい。**まずは話速（提案③）から**。ElevenLabs への投資は、**Fact score 的に「同じ台本の読み上げ」を A/B で比較し、判読性向上が示されてから**行う |

---

*本書は分析のみ。ファイルの変更は行っていない。実装を開始する際は、各提案の想定工数を実測値に置き換えること。*
