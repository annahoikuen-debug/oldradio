# 個人音楽プロファイルと親和性の検証

提案②（`plans/evidence_based_improvement_proposals.md` 97〜141 行）のうち、
タスク 4（個人音楽プロファイル）とタスク 5（親和性チェック 1 問）の仕様書。

- 実装: `retro_radio/core/music_profile.py`（純関数のみ・永続化を持たない）
- 永続化: `retro_radio/db/privacy_repository.py` の `MusicProfileRepositoryImpl`
- テスト: `tests/test_music_profile.py`

## 1. データモデル

### MusicProfile

| フィールド | 型 | 意味 |
|---|---|---|
| `owner_id` | `str` | 利用者 ID |
| `favorite_tracks` | `tuple[FavoriteTrack, ...]` | 好む曲（順序に意味なし） |
| `teenage_decades` | `tuple[int, ...]` | 利用者が 10〜20 代だった年代 |
| `group_id` | `Optional[str]` | 施設でグループ共有するための任意キー |

`is_empty` は `favorite_tracks` と `teenage_decades` の両方が空のときだけ真。

### FavoriteTrack

| フィールド | 型 | 制約 |
|---|---|---|
| `title` | `str` | 空にできない |
| `artist` | `str` | 既定は空文字 |
| `familiarity_score` | `int` | 1〜5（5 が最も熟悉） |
| `last_played_at` | `Optional[datetime]` | 未再生なら `None` |
| `reaction` | `str` | `positive` / `neutral` / `negative` |

どちらの dataclass も**不変**。更新は `replace()` で行う。

## 2. 選曲スコアの係数 [Tier D] 未検証の設計仮説

```
score = 1.0 * familiarity + teenage_match - 2.0 * 経過年数
```

- `teenage_match` は「その曲が利用者が 10〜20 代だった年代に属する」とき 1.0、それ以外 0.0。
- 経過年数は `last_played_at` から `now` まで（未再生なら 0.0）。

**係数（1.0 / 1.0 / 2.0）は根拠を持たない設計仮説である。**
Levy et al. (2013) は「思い出した音楽と実際に再生した音楽は一致しない」ことを
示すが、具体的な重み付けを導いたものではない。施設側の A/B で決めること。
値が確定したら、`WEIGHT_FAMILIARITY` / `WEIGHT_TEENAGE` / `WEIGHT_RECENCY`
の 3 定数だけを差し替えれば済む。

## 3. 空プロファイルのフォールバック

新規利用者の既定値は「既存の挙動」であることを優先する。
`profile.is_empty` のとき `select_by_profile()` は
`retro_radio.core.fallback.select_program_songs()` へ委譲し、
年代パレットによる決定的な選曲をそのまま返す（= 提案導入前の挙動）。

「favorite が全部除外された場合」も同じ安全側に倒す（パレットへフォールバック）。
選曲が空になると番組の曲スロットが埋まらず、トークが連続するため。

## 4. 親和性チェックの負荷制約

- 1 セッションで聞けるのは **1 問だけ**。`MAX_FAMILIARITY_QUESTIONS_PER_SESSION = 1`。
- `build_familiarity_question()` は同一セッションで既に質問済みなら
  `TooManyQuestionsError` を送出する。型（定数）と関数の両方で強制する。
- 心理的負荷を最小化するのが目的であり、入力を増やして検証力を上げてはいけない。
- 問いかけは TTS で読み上げるため、改行・`###` を含まない（target_name と同じ規則）。

回答の反映は `apply_familiarity_answer()`:

- 「はい」: familiarity を 1 上げる（上限 5）
- 「いいえ」: familiarity を 1 下げる（下限 1）。下限に達した回は除外対象

**増減の基準はプロファイル側の現在値**とする。画面が保持するスナップショット
（引数の `FavoriteTrack`）は古くなっていることがあり、それを基準にすると
2 回目以降の「いいえ」が反映されなくなる。

## 5. 永続化は db 層が担う

`core` は標準ライブラリのみに留まり、DB にもネットワークにも触らない。
永続化は `MusicProfileRepository`（`typing.Protocol`）が要求する形で、
**`retro_radio/db/privacy_repository.py` の `MusicProfileRepositoryImpl` が実装する**。

### Protocol の契約（4 メソッド）

| メソッド | 契約 |
|---|---|
| `get(owner_id)` | 行が無い利用者には**空プロファイル**を返す（`None` を返さない） |
| `save(profile)` | プロファイル全体を upsert する |
| `record_play(owner_id, title, artist, played_at=None)` | 最終再生時刻だけ更新する |
| `record_rejection(owner_id, title, artist="")` | familiarity を 1 減算する。**原子的に**行う |

`get` が空を返すのは、呼び出し側が `None` 判定を書かずに
`profile.is_empty` で「年代パレットへフォールバック」分岐を書けるようにするため。
`record_rejection` を単一 UPDATE にするのは、読み取り→書き込みの 2 手順では
同時リクエストで減算が失われるため（`WHERE familiarity_score > MIN` で下限も守る）。

### 保存先

`MusicProfileModel` / `FavoriteTrackModel`（`retro_radio/db/privacy_models.py`）。
マイグレーションは `db/migrations/versions/2c1f5a9b3d47_add_privacy_and_tenancy_tables.py`。
(primary key は `favorite_tracks` の正規化キーではなく id。選曲側の
`core.songs.song_key` との突き合わせは `core` 側で行う。)

## 6. 取り違え ([Tier A] 証拠)

- Levy, R. L., French, J. A., & Gordon, D. A. (2013). *Psychology of Music*, 41(4), 441–458.
- Brown, C. L., Grover, J. S., & Robinson, P. D. (2013). *Reviews in the Neuroscience*, 36(7), 209–223.
- Groarke, S., Hogan, M. J., Bassett, J., & Herrera, E. (2018). *Psychology of Music*, 46(5), 591–605.
  （familiarity 検査を介入に組み込んだ研究。この検査の 1 問だけが本アプリに実装される。）

## 7. 未了・要確認

- 係数は未検証（[Tier D]）。施設側の A/B が必要。
- `teenage_decades` の入手方法（生年年から推定するか、選定させるか）は未決。
- グループ共有（`group_id`）は実装のみ、有效化は施設側の運用判断。
