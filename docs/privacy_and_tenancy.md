# プライバシーとテナント分離の設計（提案⑧・S4）

> **本書は法的助言ではない。**
> 個人情報保護法の適用範囲・同意取得の適法性・安全管理措置の妥当性は、
> **弁護士による確認を要する**。ここに書かれたのは
> 「技術的に何を実装したか」と「運用で何をする必要があるか」だけである。
> 法的解釈と技術的な実装は、**一致しないことがある**。

## 1. 何が問題だったか

このリポジトリは「個人利用の趣味 Web サービス」の前提で書かれていた。
福祉施設の運用前提で書かれていなかった。根拠は
`plans/evidence_based_improvement_proposals.md` の「8. 提案⑧」。

| 事象 | 深刻度 |
|---|---|
| `/api/generate` が認証を一切行わない（`Authenticator` 20KB は未配線） | 高 |
| TTS キャッシュが OS 共有の一時ディレクトリ 1 つ。テナント分隔が無い | 高 |
| `/api/generate` が DB に一切書かない → 削除要求を追跡できない | 高 |
| `secret_key` が空でも起動する | 中 |
| `api/v1.py` が include されていない | 中 |
| `history_service` / `favorite_service` / `export_service` が孤児 | 中 |

**足りないものを足すのではなく、既にあるものを有効化し、
既定を安全側に倒す**ことが本提案の核心。

## 2. 実装したものの全体像

### 2.1 認証（タスク1）

| 環境変数 | 既定 | 意味 |
|---|---|---|
| `RETRO_RADIO_REQUIRE_AUTH` | **1（安全側）** | `/api/generate` と `/api/audio/*` の認証要否 |
| `RETRO_RADIO_SECRET_KEY` | `""` | 画面ログイン + セッション Cookie の署名鍵 |
| `RETRO_RADIO_SINGLE_USER_KEY` | `""` | 個人モードの単一ベアラートークン |

4 つのモード（`Settings.require_auth_config()`）:

| モード | 条件 | 保護対象への応答 |
|---|---|---|
| `session` | `secret_key` あり | 署名付き Cookie で通過 |
| `bearer` | `secret_key` 無し / `single_user_key` あり | `Authorization: Bearer` で通過 |
| `disabled` | `require_auth=0` | **意図的に**保護を切って通す（`default` テナント） |
| `unavailable` | `require_auth=1` かつ資格情報なし | **503（fail-closed）** |

**`unavailable` について**: 「認証を有効にしたつもりだが誰も通れない」は
「認証が無い」と同じ危険を持つ。起動を止めずに全保護を拒否する。
503 の本文には原因と対処の両方を入れる（運用者向け）。

**鍵分離**: セッションとベアラーは PBKDF2 の salt ドメインを分けて導出する。
同じ鍵だと「ベアラートークンを Cookie として注入する」攻撃が通る。

**既定を 0 にしない理由**: 既定 0 だと「施設にデプロイしたのに認証が無い」状態が
何も言わずに成立する。`require_auth=0` は**個人利用者が明示的に選ぶ**状態。

### 2.1.1 サポートする 2 つの構成

実際に動いて保護も効くのは、次の 2 つだけである。
`require_auth=1` かつ鍵なし（= `unavailable`）は 3 つ目として成立しない。

| 構成 | 必須の設定 | `require_auth_config()` | `/api/generate` |
|---|---|---|---|
| **個人利用（認証なし）** | `RETRO_RADIO_REQUIRE_AUTH=0` | `disabled` | 200 |
| **施設利用（認証あり）** | `RETRO_RADIO_REQUIRE_AUTH=1` + `SECRET_KEY` または `SINGLE_USER_KEY` | `session` / `bearer` | 200 |
| （成立しない組み合わせ） | `REQUIRE_AUTH=1` かつ鍵なし | `unavailable` | **503** |

**`.env.example` が出荷しているのは「個人利用（認証なし）」**、
つまり `RETRO_RADIO_REQUIRE_AUTH=0` である。これは README のクイックスタートが
`cp .env.example .env` だけで実際に動くようにするためであり、
**施設公開の推奨設定ではない**。施設へデプロイする時は必ず
`RETRO_RADIO_REQUIRE_AUTH=1` に戻し、鍵を設定すること。
コード側の既定値（`config.py` の `require_auth: bool = True`）は
安全側のまま変更していない。

鍵の生成: `python -c "import secrets; print(secrets.token_urlsafe(32))"`

### 2.2 テナント（タスク1/2）

**テナント = デプロイ単位 = 施設単位**。個人利用では `default` の 1 行だけ。

| テーブル | 役割 |
|---|---|
| `tenants` | テナント本体（1 施設 = 1 行） |
| `user_security` | 既存 `users.id` を主キーとしてロール / 所属テナント / 削除フラグを持つ |
| `music_profiles` | 個人音楽プロファイル（`owner_id` UNIQUE、`group_id` で施設共有可） |
| `favorite_tracks` | favorite 1 曲（`(owner_id, title, artist)` UNIQUE） |
| `consents` | 同意記録（**版単位**） |
| `audit_logs` | 生成 / 再生 / 同意 / 削除の各イベント（`tenant_id` NOT NULL） |

**既存テーブルを一切拡張していない**。`tests/test_db_models.py:11-23` が
テーブル集合と `users` の列集合の**完全一致**を検証しているため。
ロールとテナントは `user_security` に持たせている。

**外部キー**: `tenants` へのものは張る（同一 MetaData 内）。
`users` / `generations` へのものは**張らない**（別 MetaData であり、
匿名化＝行は残る・消える の二択と `ON DELETE CASCADE` は相性が悪い）。

### 2.3 要配慮個人情報の最小化（タスク3）

`anniversary` モードは「氏名 + 生年月日」を TEC 受けていた。
SUR 最小化の方針は **2 段**:

1. ** Siegel 什么叫「年だけでよい」**: `validate_anniversary_input()` が
   `month` / `day` を**受け取って捨てる**。黙って無視するので不自然ではなく、
   返却値の `dropped` で「破棄した」と明示する。
2. **Mexico ニックネーム**: `target_name` は**最大 16 文字**。
   制御文字・`###` 等の構造マーカーは拒否。
   UI 文言の変更は S9 の管轄（サーバ側はここで用意済み）。

   > **R2-07 による改訂（2026-10-02）**
   > `api.me.MAX_TARGET_NAME_LENGTH`（16）と `server.GenerateRequest` の
   > `max_length`（プラットフォームは 64）が競合しており、**実際に 64 文字まで
   > 受理していた**（＝本名をそのまま書ける長さ）。`GenerateRequest` 側が
   > `normalize_target_name` を呼ぶ形に一本化し、上限を 16 にした。
   > 空文字・空白のみの `target_name` は `None` に正規化される。

**年だけで十分な理由**: 曲を選ぶのは 10 年単位の年代選択であり、
生月日はRecall の質にどの程度寄与するかが**このアプリでは検証されていない**。
入力喘息を最小化しても、节目的効果の測定値は変わらない（測定はすべき）。

### 2.4 開示と削除（タスク3）

| エンドポイント | 形式 | 保護 |
|---|---|---|
| `GET /api/me/export` | `format=json`（構造を保つ）/ `format=csv`（BOM 付き） | `require_consent` |
| `DELETE /api/me` | 論理削除 | `require_consent` |

**削除は論理削除**:

1. `user_security.deletion_requested_at` に受付日時
2. `favorite_tracks` / `music_profiles` / `consents` を**削除**
3. `generations` も**削除**（**原稿全文**が入っているため。件数は応答の
   `purged.generation_rows_removed` に出して利用者へ説明できる）
4. `users.email` → `deleted+<user_id>@invalid.example`、`hashed_password` → `!`
   （**行は消さない**。削除処理の証拠と監査ログの整合のため）
5. `audit_logs` に削除の事実を**残す**
6. その**テナントだけ**の TTS キャッシュを削除

> **R2-08/DB-01 による改訂（2026-10-02）**
> 旧版は手順 3 を「`generations` は消さない（`generations.user_id` の FK が
> 宙に浮くため）」としていた。その前提は「`generations` が恒久的に空」という
> 想定だったが、実態は **`record_generation` が未配線で 1 件も記録されて
> いなかった**ことだった。記録が埋まったため、原稿全文が残存する結果に
> なるため、削除対象に含める。FK は張られておらず、宙に浮く行も無い。

**監査ログを消さない理由**: 削除請求があったという事実は
利用者のデータではなく**処理の証明**。
ただし「監査ログ自体の長期保持が開示対象になるか」は弁護士の確認が必要。

### 2.5 同意（タスク5）

同意は **版（`RETRO_RADIO_TERMS_VERSION`）単位**。
版を上げると過去の実諾では**現在の版に同意していない**と判定される。

```
GET  /api/terms              # 未ログインでも条文を読める（同意の前に内容を知る）
GET  /api/me/consent         # 自分の同意状態
POST /api/me/consent         # 同意 / 明示的拒否を記録（監査ログに残る）
POST /api/me/consent/withdraw
```

`RETRO_RADIO_REQUIRE_CONSENT=1` にすると、未同意の利用者は
個人データを取り込む API（`/api/me/export`, `DELETE /api/me`）を叩けない
（403 + `consent_url`）。

    **拒否も記録する**。「提示した条件に拒否した」という事実が残るため。

### 2.6 監査ログ（タスク6）

`GET /api/admin/audit` は **`admin` ロール限定**、しかも
**テナントをまたいだ検索 (`scope=global`) を提供しない**。
施設管理者は自分のテナントしか見えない。

完全率（効果指標「監査ログの完全率 = 100%」）は
`audit_coverage(log_count, generation_count)` で計算し、
`tests/test_me_api.py::test_audit_log_completeness_is_100_percent` で固定。

**`meta` に個人データを入れない**（氏名・生年・原稿本文）。
監査ログは長期保存されるため、ここに識別情報を書くと
削除請求時に消す対象が広がる。`tests/test_me_api.py` で固定済み。

### 2.7 テナント別 TTS キャッシュ（タスク2）

```
CACHE_DIR / <tenant_id> / tts_<sha256>.mp3
```

**不変条件（テナント交差 = 0）**を 4 段で担保する:

1. ディレクトリが物理的に別
2. 同じテキストでも別テナントは別ファイル
3. `resolve(tenant, name)` は他テナントを解決しない
4. `sweep(tenant)` は他テナントを消さない

配信 URL は `/api/audio/{tenant_id}/{filename}` となり、
**テナント ID がセッション照合の材料**になる。

## 3. 運用でやること（技術では代替できない）

以下は **このリポジトリの実装では足りない**。施設側の運用設計が必要。

### 3.1 同意取得プロセス

Bélanger & Cross (2011) の指摘に従い、**技術的統制だけでは足りない**。

- [ ] 同意の**取得者**を決める（施設長か、職員か、本人か）
- [ ] 同意サンプルを 1 ページで**読み上げられる**ものにする
- [ ] -proxy 同意した場合、**_ops 是不参加の記録**を残す
- [ ] 同意の**撤回**手順を職員向けに用意する
- [ ]  guardianship（法定代理人）からの同意も想定する

** Giacomo 通`:公開の同意画面（S9 の UI 実装）`**

### 3.2 削除請求の運用

- [ ] `RETRO_RADIO_DELETION_SLA_HOURS`（既定 24 時間）を**運用目標**として宣言する
- [ ] `GET /api/admin/audit/stats` の `pending_deletions` を**日常的に監視**する
- [ ] 削除が**匿名化で済んでいる**ことを、利用者へどう説明するかを用意する
- [ ] **監査ログの保持期間**を定めて弁護士と確認する

### 3.3 管理者運用

- [ ] 初回デプロイ時に `RETRO_RADIO_ADMIN_EMAILS` へ bootstrap admin を設定
- [ ] 管理者アカウントの `user_security.role` を `admin` に設定
- [ ] 管理者が**交代したら** `RETRO_RADIO_ADMIN_EMAILS` から外す
  （`user_security.role` が恒久的な正）
- [ ] セッションの TTL（既定 8 時間）をシフト長に合わせる

### 3.4 テナント運用

- [ ] 1 施設 = 1 テナント。**共有ホストで 2 施設は-separate**
- [ ] テナントの追加は `TenantRepository.ensure()` で**冪等**
- [ ] テナントの削除は**行を消さない**（`is_active=False` で論理削除）
  監査ログとの整合のため

### 3.5 監視すべき観測点

| 指標 | 観測点 | 目標 |
|---|---|---|
| 認証適用率 | `/health` の `auth_required` | 100% |
| テナント交差 | `TenantTtsCache` の不変条件 | 0 |
| 監査ログ完全率 | `/api/admin/audit/stats` の `coverage_ratio` | 1.0 |
| 削除 SLA | `pending_deletions` | 0（24 時間以内） |

`/health` は `auth_required` / `auth_ready` / `auth_mode` / `auth_enforced` /
`secret_key_configured` を返す（実装済み）。

## 4. 同意の仕様判断（P1-2 で決着・実装済み）

`require_consent` 系の論点は次のように決めた。

1. **同意は個人単位**。`users` / `user_security` に紐づく
   `user_id` を正とする（施設側の契約単位では、テナント内の
   利用者個々の意思が反映できないため）。
2. **同意前後で許す操作を分ける**:
   - **許す（同意不要）**: 番組生成（`/api/generate`）。生成は
     放送を流すだけで個人データを残す操作ではない（監査ログの
     `meta` には個人データを入れない）。
   - **許さない（同意必須）**: データエクスポート（`/api/me/export`）・
     削除（`DELETE /api/me`）・音楽プロファイル（`/api/me/music-profile*`）。
     これらは `require_consent` 依存で、未同意なら
     **403 + `consent_required`** を返す（実装済み、`api/deps.py`）。
3. **版（`terms_version`）を跨いだら再取得が必要**。
   `ConsentRepository.has_consented(user_id, terms_version)` が
   現行版との一致を見るため、規約改訂時に再同意を促す。
4. **SPA の導線（実装済み、`static/app.js`）**:
   - 起動時に `GET /api/me/consent` を呼ぶ。
     `required && !consented` なら**同意モーダルを開く**。
   - 同意しない（`accepted=false`）を選んだら**生成ボタンを無効化**する
     （`applyConsentGate(true)`）。生成は許す操作だが、
     拒否の意思表示を尊重して放送の新規受信は止める。
   - 同意したら無効化を解除する（`applyConsentGate(false)`）。
5. `/api/admin/audit` は**管理者専用**。SPA 側は 403 時に
   「管理者ではない」旨を表示する（`describeError` の既存経路）。

## 5. 未解決・要弁護士確認

以下は **技術的な判断で決着不起来**。

1. **監査ログ自体の開示対象性** — 「削除請求があった」という事実は
   利用者のデータか、処理の証拠か。保持期間はどうべきか。
2. **`music_profiles.group_id` による共有の適法性** —
   施設グループで選好を共有する場合、本人の同意で足りるか。
3. **`users` を anonymize して残すことの適法性** —
   匿名化した `email`（`deleted+<id>@invalid.example`）が
   個人データに該当するか。
4. **要配慮個人情報の範囲** — 出生年のみでも要配慮個人情報に該当するか。
   「年だけ」という最小化が実際に十分かの確認。
5. **同意の撤回と利用停止の関係** — 撤回後に生成済みの原稿を
   どこまで遡って削除すべきか。
6. **`RETRO_RADIO_REQUIRE_CONSENT` を既定 1 にすべきか** —
   S4 では既定 `False`（既存デプロイを壊さないため）。施設導入時に 1 へ。

## 6. 関連ファイル

| ファイル | 役割 |
|---|---|
| `retro_radio/config.py` | `require_auth` / `auth_ready` / `require_auth_config()` / `retry_wait` 検証 |
| `retro_radio/auth/tokens.py` | 署名付きセッション / ベアラートークン |
| `retro_radio/api/deps.py` | `require_tenant()` / `require_admin()` / `audio_tenant()` |
| `retro_radio/db/privacy_models.py` | 6 テーブルの定義（**別 MetaData**） |
| `retro_radio/db/privacy_repository.py` | `MusicProfileRepositoryImpl` ほか |
| `retro_radio/api/me.py` | 開示・削除・同意 |
| `retro_radio/api/audit.py` | 監査ログ（`admin` 限定） |
| `retro_radio/services/tenant_cache.py` | テナント別 TTS キャッシュ |
| `db/migrations/versions/2c1f5a9b3d47_add_privacy_and_tenancy_tables.py` | マイグレーション |
| `tests/test_auth_wiring.py` | 既定の安全性 / fail-closed / テナント交差 0 |
| `tests/test_me_api.py` | Repository 契約 / 開示 / 削除 / 完全率 / 16 文字制約 |
