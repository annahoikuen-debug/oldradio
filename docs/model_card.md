# モデルカード: レトロラジオ（Retro Radio）

**対象**: 介護施設・デイサービス回想法レクリエーション向けラジオ番組生成システム
**版**: S2（提案⑨ 評価ハーネス導入時点）
**作成日**: 2026-09-30
**形式**: Mitchell et al. (2019) *Model cards for model reporting* /
Gebru et al. (2021) *Datasheets for datasets* に倣う

> **このカードの読み方**
>
> 「対応範囲」に書いたことだけが、このアプリが語ってよい内容です。
> **非対応**に書いたことに気づかずに利用しないことが、このカードの第一の目的です。
> 特に **年ごとの情報量がない**（10 年単位である）ことを、ご検討の
> 方と施設のご家族に**必ず説明してください**。

---

## 1. モデルの概要

| 項目 | 内容 |
|---|---|
| 種別 | ルールベースの原稿生成（定型）＋ 任意で LLM（Gemini）による原稿生成 |
| 入力 | 対象年（1950〜2025）・日付・モード（`normal` / `care_recreation` / `anniversary`）・お祝い対象者名（`anniversary` の場合） |
| 出力 | 日本語の読み上げ原稿（`###` 見出し付きセグメント構造）と番組表 |
| 外部依存 | Gemini API（任意。キー未設定なら定型経路）、iTunes Search（任意。429 なら静的マスターへ） |
| 学習 | **しない**。このシステムに学習済みのパラメータはない |
| 正本 | 事実レジストリ `retro_radio/core/facts/programs.json`（1 事実 1 レコード、17 件） |

---

## 2. 対応範囲（意図された用途）

### 2.1 対応: 1950〜2025

- 対象年は `retro_radio/config.py:101-103` の `min_year` / `max_year` で 1950〜2025 に固定。
- 3 つのモードを用意している。

  - `normal` … 一般的なラジオ番組の原稿。
  - `care_recreation` … 介護施設・デイサービスの回想法レク用原稿（クイズ付き）。
  - `anniversary` … 誕生日・記念日の祝福原稿（対象者名を入れる）。

### 2.2 ⚠️ 不利な truths（必ず把握して使うこと）

> **年ごとの情報量は 10 年単位であり、特定年を指定してもその年だけの情報はない。**

これは設計上の制約ではなく、**データの粒度**に由来する実在の限界である。

- 回想法クイズは **10 年バケット**（`REMINISCENCE_DATA`）で 8 個分しかない。
  1975 年を指定しても、1970 年代のクイズが 3 問出るだけである
  （`retro_radio/core/fallback.py`）。
- ニュースの種も **10 年バケット + 2025 専用**の 9 バケット
  （`retro_radio/core/script_generator.py:21-83`）。
- **`normal` モードの原稿は年によって文字数が 1 文字も変わらない**（実測 1339 字で一定）。
  これは内容が変わっていないためではなく、`{year}年{month}月{day}日` の桁数が
  4 桁で固定されているからである（`python -m eval.metrics.length --measure` で再現できる）。
- **`normal` モードは番組を 1 つも言及しない**（fact score の coverage が 0.0%。
  `tests/test_eval_harness.py::test_normal_scripts_mention_no_program_at_all` が固定）。

したがって **「1975 年について聞けば 1975 年のことが分かる」わけではない**。
年を指定できるのは「その時代の話題に寄せる」ためであり、
「その年だけの記録を取り出す」ためではない。

### 2.3 対応する用途

- 集団での回想法レクの**補助**（司会者が読み上げる台本の下書き）。
- その年の番組・曲が手がかりになり、記憶を呼び起こすこと。
- 会話のきっかけ（選曲・番組表の提示）。

---

## 3. 非対応（このアプリはできない。使うべきではない）

以下は**意図的に範囲外**であり、実装予定はない。

| 非対応 | 理由 |
|---|---|
| **個別の病史の想起支援** | 病歴は個人データであり、本アプリはその情報を一切持たない。想起を支援する機能は提供しない。 |
| **歴史的事実の権威（史料として引用する用途）** | 事実レジストリの全 17 レコードが `confidence: "unverified"`（一次文献未照合）。**検証済みの出典は 1 件もない**。史料として引用してはならない。 |
| **医療・看護・生活助言** | 生成物に助言・指示・診断は含まれない。読み上げ者が助言として解釈しないこと。 |
| **特定の年・月・日の事実の質問への回答** | 日付は**場面設定**であって情報源ではない。「1975 年 5 月 15 日に放送された番組は？」には答えられない。 |
| **人格としての会話** | 固定の定型原稿であり、対話機能はない。 |
| **事実の訂正 UI** | 誤りは**生成前の正本側**で防ぐ設計（`docs/facts_registry.md` 6 章）。訂正は `note_ja` への追記運用で行う。 |

---

## 4. 既知の失敗モード

### 4.1 Gemini API キーが空のときの定型原稿

- `retro_radio/core/script_generator.py:267-274` で、キー未設定なら**必ず**
  `generate_fallback_script` / `generate_care_script` / `generate_anniversary_script` に落ちる。
- 結果: **「予告したのに配信しない」が常に生じる**。
  `core/fallback.py:341` が「本章では、{年}年のニュースと、当時のくらしの風景を
  三つほどご用意しました」と予告するが、その後に続くトーク 1〜3 には
  **具体的な項目が 1 件も無い**。
  `eval/metrics/preannounce.py::detect_unfulfilled_preannounce` がこれを検出する
  （現状: 24 ケース中 `normal` モードの 8 ケースすべてで検出）。
- 利用者はこれを知らずに「3 つのはなしが来る」と誤解しうる。

### 4.2 iTunes Search の 429（レート制限）

- iTunes が 429 / 失敗すると静的フォールバック曲（`FALLBACK_SONGS`、36 曲）に落ちる。
  結果、**その年にふさわしい曲が流れない**ことがある
  （バケット単位の静的な選択であり、年ごとの回転ではない）。
- 検知方法: `core/music_search.py` の WARNING ログ。

### 4.3 事実誤認

- S1（提案⑦）で是正した確定的な誤認 4 件
  （ザ・ヒットパレード / ノイタミナA / Sportacent / 歌謡パレード）と
  内部矛盾 1 件（8時だョ!全員集合）は `tests/test_facts_registry.py` で再発を止める。
- **検出できているが放置している警告 5 件**（`docs/facts_registry.md` 191〜198 行）:

  | 対象 | 内容 |
  |---|---|
  | `care_recreation/1970` | オールナイトニッポン（1971 年開始） |
  | `care_recreation/2000` | 「2001年」 |
  | `care_recreation/2010` | 「2011年」「2012年」 |
  | `care_recreation/2011` | 「2012年」 |

  これらは `REMINISCENCE_DATA`（クイズ自由文）由来で、レジストリの管轄外。
  **台本由来のため warn であり、CI ゲートは落とさない。**

### 4.4 年と代表曲のずれ

- `care_recreation/1950` は 1950 バケットの代表曲 2 件が **1951 年リリース**のため、
  fact score が **76.9**（閾値 80.0）でゲート落ちる。
  これは実在の欠陥であり、
  `tests/test_eval_harness.py::test_care_1950_is_the_known_low_score_case` が記録している。

### 4.5 発話・テキストの混入

- LLM 経路ではプロンプトの残骸（`以下のセグメント構成で` / `Lag:` / `レスポンス:` 等）が
  原稿に混ざる可能性がある。`retro_radio/utils/text_cleaner.py` が除去するが、
  **行内混入や API 応答ラベルは取り逃す**。検出は
  `eval/metrics/checklist.py::check_prompt_leftover`。

### 4.6 ルーブリック未検証（重要）

> **このモデルカードに書いた「対応範囲」「非対応」は、本アプリのために
> 行われた検証に基づくものではない。**
> 施設側でのサンプリング評価は**まだ 1 回も行っていない**。
> 根拠になっているのは実装の読み解きと `eval/` の自動指標のみである。

---

## 5. 評価方法

### 5.1 自動指標（`eval/` ハーネス）

| 指標 | 概要 | 現状（2026-09-30 実測） |
|---|---|---|
| **FactScore**（0〜100） | 原稿を原子的事実に分解し、事実レジストリと 1 件ずつ照合した支持率 | 24 ケース平均 **97.88** / 最小 **76.92** |
| **CheckList 違反**（8 項目） | 見出し順・必須セグメント・曲名一致率・年代語・プロンプト残骸・日本語比率・重複・未履行予告 | **8 件**（すべて `normal` モードの未履行予告） |
| **文字数の帯** | 800〜1500 字（実測 p5=1058 / p50=1339 / p95=1476 より決定） | **228 サンプルすべて帯内** |

### 5.2 人間評価（Best-Worst）

- 手順書は `eval/bww/README.md`（Zhao et al. 2019 相当 / Kreutzer et al. 2020 に倣う）。
- 評価者は**介護職員 2〜3 名**。ルーブリックは事前に固定・共有・訓練する。
- 結果は `eval/results/YYYYMM.json`。**人手が必要なため CI には載せない。**

### 5.3 CI ゲート

`eval/README.md` の「CI ゲートの整理」節を参照。

- **PR ゲート**: 自動指標のみ、1 分以内。
- **Nightly**: 24 ケース全体 + Best-Worst 人間評価の募集。

---

## 6. データと出典

| データ | 規模 | 出典の状態 |
|---|---|---|
| 事実レジストリ（番組） | 17 レコード | **全件 `unverified`**。`source_url` は全件 `null`。文献名のみ。 |
| 静的マスター（曲） | 36 曲 | リリース年メタデータつき（`FALLBACK_SONG_YEARS`）。出典は未記載。 |
| 回想法クイズ | 8 バケット × 3 問以上 | 出典なし。**自由文**。 |
| ニュースの種 | 9 バケット × 4〜5 件 | 出典なし。**自由文**。 |

### 6.1 未検証の書誌（`[要確認]` が残るもの）

> **ネットアクセスができない環境で作業したため、以下の書誌は
> 「未検証の書誌」として記録する。引用・転用・根拠の主張に使ってはならない。**

- 事実レジストリの 17 レコードすべての `source` フィールド（`[要確認]`）。
  特に `japan_countdown_1984`（開始年・終了年・放送時間が一次文献で確認できていない）、
  `sportacent_2004`（開始年が「2004 年頃」で未確定）。
  詳細は `docs/facts_registry.md` 第 7 章。
- `plans/evidence_based_improvement_proposals.md` 付録 C の書誌のうち
  `[要確認]` / `[頁要確認]` が付くもの（Kreutzer et al. 2020、Zhao et al. 2019、
  Ribeiro et al. 2020、Pagnoni et al. 2021 の**巻・号・頁**）。
  着手前に必ず一次文献で照合すること。
- **URL は推測で埋めていない。** `source_url` は全件 `null` であり、
  `tests/test_facts_registry.py::test_no_fabricated_source_urls` が固定している。

### 6.2 典拠（文献名のみ。巻・号・頁は未検証）

本カードの記述は以下に依拠する。**いずれも本アプリのために行われた検証ではない。**

- Mitchell, M., et al. (2019). Model cards for model reporting.
- Gebru, T., et al. (2021). Datasheets for datasets.
- Zhao, T., Liu, F., & Liu, L. (2019). Efficient and appropriate blended human
  evaluation for NLG. `[要確認: 巻・号・頁]`
- Kreutzer, J., Caswell, I., Wang, L., et al. (2020). Quality at a glance:
  An audit of human evaluation for natural language generation. `[要確認: 巻・号・頁]`
- Ribeiro, M. T., Singh, S., & Guestrin, C. (2020). Beyond accuracy:
  Behavioral testing of NLP models with CheckList. `[要確認: 巻・号・頁]`
- Pagnoni, T., et al. (2021). Evaluating correctness and faithfulness of
  instruction-followed text summarizations. `[要確認: 巻・号・頁]`
- Kwong, J., et al. (2023). FactScore. `[要確認: 巻・号・頁]`

---

## 7. 改訂履歴

| 日付 | 改訂 | 担当 |
|---|---|---|
| 2026-09-30 | 初版作成。S2（提案⑨ 評価ハーネス） | サブエージェント S2 |
