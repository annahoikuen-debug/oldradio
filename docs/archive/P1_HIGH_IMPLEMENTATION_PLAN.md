# P1 High 実装計画書

## 概要
- 対象課題数: 6件（Highレベル）
- 総ステップ数: 24ステップ
- 推定工数: 6時間

## 前提条件
- Python 3.10+
- 依存パッケージ: 既存requirements.txt準拠
- テスト実行: `pytest tests/ -v`

## ステップ一覧

### Step 1: インメモリユーザー保存削除 - _users_dbクラス変数削除準備
- **対象ファイル**: `retro_radio/auth/authenticator.py:27-29`
- **変更内容**: 
  - コメントを追加して削除予定であることを明示
    ```python
    # TODO: Remove in-memory storage - rely solely on UserRepository
    # _users_db = {}
    # _users_by_id = {}
    ```
- **確認方法**: コメントが追加されていることを確認
- **所要時間目安**: 2分

### Step 2: インメモリユーザー保存削除 - _users_db実際の削除
- **対象ファイル**: `retro_radio/auth/authenticator.py:27-29`
- **変更内容**: 
  - 27-29行を完全に削除
- **確認方法**: 該当行が削除されていることを目視確認
- **所要時間目安**: 2分

### Step 3: インメモリユーザー保存削除 - _users_by_id削除
- **対象ファイル**: `retro_radio/auth/authenticator.py:28-29`（実際は今27-28）
- **変更内容**: 
  - 該当行を削除（すでに削除済みの場合は次の行）
- **確認方法**: 該当行が削除されていることを目視確認
- **所要時間目安**: 2分

### Step 4: インメモリユーザー保存削除 - コンストラクタの初期化行削除
- **対象ファイル**: `retro_radio/auth/authenticator.py:52-54`
- **変更内容**: 
  - 52-54行の 
    ```python
    self._users_db = Authenticator._users_db
    self._users_by_id = Authenticator._users_by_id
    Authenticator._users_db[email_key] = user
    Authenticator._users_by_id[user.id] = user
    ```
    を削除
- **確認方法**: 該当行が削除されていることを目視確認
- **所要時間目安**: 3分

### Step 5: インメモリユーザー保存削除 - _save_userメソッドからインメモリ関連削除
- **対象ファイル**: `retro_radio/auth/authenticator.py:59-68`
- **変更内容**: 
  - _save_userメソッドからインメモリストレージ関連コードを削除
  - 変更前:
    ```python
    def _save_user(self, user: User):
        email_key = user.email.lower()
        old_user = self._users_db.get(email_key) or Authenticator._users_db.get(email_key)
        if old_user and old_user.id != user.id:
            self._users_by_id.pop(old_user.id, None)
            Authenticator._users_by_id.pop(old_user.id, None)
        self._users_db[email_key] = user
        self._users_by_id[user.id] = user
        Authenticator._users_db[email_key] = user
        Authenticator._users_by_id[user.id] = user
        if self.user_repo:
            try:
                existing = self.user_repo.get_by_id(user.id)
                if existing:
                    self.user_repo.update(user)
            except Exception:
                pass
    ```
  - 変更後:
    ```python
    def _save_user(self, user: User):
        if self.user_repo:
            try:
                existing = self.user_repo.get_by_id(user.id)
                if existing:
                    self.user_repo.update(user)
                else:
                    self.user_repo.create(user.email, user.hashed_password)
            except Exception:
                pass
    ```
- **確認方法**: インメモリ関連コードが削除され、user_repoベースのロジックになっていることを確認
- **所要時間目安**: 5分

### Step 6: インメモリユーザー保存削除 - _get_user_by_email/_get_user_by_id簡素化
- **対象ファイル**: `retro_radio/auth/authenticator.py:77-98`
- **変更内容**: 
  - _get_user_by_emailと_get_user_by_idをuser_repoのみに依存するように簡素化
  - インメモリフォールバックを削除
- **確認方法**: インメモリ参照が削除されていることを確認
- **所要時間目安**: 4分

### Step 7: ブロッキングsleep撤廃 - app.pyの最初のsleep特定
- **対象ファイル**: `app.py:1222`
- **変更内容**: 
  - `time.sleep(0.5)` コメントアウトまたは削除
  - 変更: `# time.sleep(0.5)  # Step 1: Generate script`
- **確認方法**: 行がコメントアウトされていることを確認
- **所要時間目安**: 1分

### Step 8: ブロッキングsleep撤廃 - 2番目のsleep特定
- **対象ファイル**: `app.py:1234`
- **変更内容**: 
  - `time.sleep(0.5)` コメントアウトまたは削除
  - 変更: `# time.sleep(0.5)  # Step 2: Search songs`
- **確認方法**: 行がコメントアウトされていることを確認
- **所要時間目安**: 1分

### Step 9: ブロッキングsleep撤廃 - 3番目のsleep特定
- **対象ファイル**: `app.py:1253`
- **変更内容**: 
  - `time.sleep(0.5)` コメントアウトまたは削除
  - 変更: `# time.sleep(0.5)  # Step 3: TTS generation`
- **確認方法**: 行がコメントアウトされていることを確認
- **所要時間目安**: 1分

### Step 10: ブロッキングsleep撤廃 - 4番目のsleep特定
- **対象ファイル**: `app.py:1263`
- **変更内容**: 
  - `time.sleep(0.3)` コメントアウトまたは削除
  - 変更: `# time.sleep(0.3)  # Step 4: Save to Database & History`
- **確認方法**: 行がコメントアウトされていることを確認
- **所要時間目安**: 1分

### Step 11: ブロッキングsleep撤廃 - 5番目のsleep特定
- **対象ファイル**: `app.py:1314`
- **変更内容**: 
  - `time.sleep(0.5)` コメントアウトまたは削除
  - 変更: `# time.sleep(0.5)  # Increment generation counter`
- **確認方法**: 行がコメントアウトされていることを確認
- **所要時間目安**: 1分

### Step 12: ブロッキングsleep撤廃 - 代替案検討（本当に必要なら非同期版）
- **対象ファイル**: `app.py:1220-1320`周辺
- **変更内容**: 
  - 人工遅延がUI体験のために必要な場合は、ステータス更新のタイミングを調整
  - 実際の処理が完了したことを示すために、各ステップの完了を待機する仕組みにする
  - ただし、今回の修正では単純に削除（過度な遅延はUX悪化のため）
- **確認方法**: スリープ行が削除/コメントアウトされていることを確認
- **所要時間目安**: 2分

### Step 13: キャッシュ無効化バグ修正 - export_service.pyのキャッシュキー改善
- **対象ファイル**: `retro_radio/services/export_service.py:27-31`
- **変更内容**: 
  - キャッシュキーを`user_id`+`タイムスタンプベースのバージョン`に変更
  - または、キャッシュを単純に無効化（頻度低い操作なのでキャッシュ不要の可能性）
  - 簡易修正: キャッシュを無効化
  - 変更前:
    ```python
    # キャッシュチェック
    now = datetime.now().timestamp()
    if user_id in self._cache:
        cached_data, timestamp = self._cache[user_id]
        if now - timestamp < self._cache_timeout:
            return cached_data
    ```
  - 変更後（キャッシュ無効化）:
    ```python
    # キャッシュを無効化（エクスポートは頻度低く、常に最新データが必要）
    # now = datetime.now().timestamp()
    # if user_id in self._cache:
    #     cached_data, timestamp = self._cache[user_id]
    #     if now - timestamp < self._cache_timeout:
    #         return cached_data
    
    # キャッシュを使用しない場合はコメントアウトまたは削除
    ```
- **確認方法**: キャッシュチェックロジックがコメントアウトまたは削除されていることを確認
- **所要時間目安**: 3分

### Step 14: キャッシュ無効化バグ修正 - favorite_service.pyの秒数バグ修正
- **対象ファイル**: `retro_radio/services/favorite_service.py:25`
- **変更内容**: 
  - 変更前: `if (now - self._last_cleanup).seconds > 60:`
  - 変更後: `if (now - self._last_cleanup).total_seconds() > 60:`
- **確認方法**: `.seconds` が `.total_seconds()` に変更されていることを確認
- **所要時間目安**: 2分

### Step 15: キャッシュ無効化バグ修正 - favorite_service.pyのクリーンアップ頻度調整
- **対象ファイル**: `retro_radio/services/favorite_service.py:25-26`
- **変更内容**: 
  - クリーンアップ頻度をより適切な間隔に変更（5分に1回など）
  - 変更前: `if (now - self._last_cleanup).total_seconds() > 60:`
  - 変更後: `if (now - self._last_cleanup).total_seconds() > 300:  # 5分に1回`
- **確認方法**: 時間間隔が300秒（5分）に変更されていることを確認
- **所要時間目安**: 2分

### Step 16: キャッシュ無効化バグ修正 - favorite_service.pyのキャッシュロジック見直し
- **対象ファイル**: `retro_radio/services/favorite_service.py:90-109`
- **変更内容**: 
  - お気に入り取得のキャッシュロジックを同様に見直す
  - 必要に応じて同様のtotal_seconds修正を適用
- **確認方法**: 関連する時間比較が総秒数を使用していることを確認
- **所要時間目安**: 3分

### Step 17: DBセッションリーク修正 - context managerパターンの統一
- **対象ファイル**: `retro_radio/services/export_service.py:16-18`
- **変更内容**: 
  - コンストラクタでのDBセッション保持をやめ、メソッドごとにcontext managerを使用
  - 変更前:
    ```python
    def __init__(self):
        self.db = get_db_sync()
        self.gen_repo = GenerationRepository(self.db)
        # キャッシュを追加（パフォーマンス最適化）
        self._cache = {}
        self._cache_timeout = 300  # 5分
    ```
  - 変更後:
    ```python
    def __init__(self):
        # キャッシュを追加（パフォーマンス最適化）
        self._cache = {}
        self._cache_timeout = 300  # 5分
    
    def _get_db_and_repo(self):
        db = get_db_sync()
        return db, GenerationRepository(db)
    ```
- **確認方法**: コンストractorがシンプルになり、DB関数が別途定義されていることを確認
- **所要時間目安**: 4分

### Step 18: DBセッションリーク修正 - export_serviceのメソッド更新
- **対象ファイル**: `retro_radio/services/export_service.py:23-33`
- **変更内容**: 
  - export_generations_csvメソッドを更新して、ローカルのDBセッションを使用
  - 変更前: `self.db` と `self.gen_repo` を使用
  - 変更後: ローカルで `db, gen_repo = self._get_db_and_repo()` を取得して使用
  - 最後に `db.close()` を確実に呼ぶ
- **確認方法**: メソッド内でDBセッションをローカル取得し、最後にcloseしていることを確認
- **所要時間目安**: 5分

### Step 19: DBセッションリーク修正 - favorite_serviceの同様の修正
- **対象ファイル**: `retro_radio/services/favorite_service.py:14-19`
- **変更内容**: 
  - コンストラクタをシンプルにし、メソッドごとにDBセッションを取得
  - 変更前:
    ```python
    def __init__(self):
        self.db = get_db_sync()
        self.fav_repo = FavoriteRepository(self.db)
        self.gen_repo = GenerationRepository(self.db)
        # キャッシュを追加（パフォーマンス最適化）
        self._favorites_cache = {}
        self._cache_timeout = 300  # 5分
        self._last_cleanup = datetime.now()
    ```
  - 変更後: 同様にシンプルなコンストラクタに
- **確認方法**: コンストラクタがシンプルになっていることを確認
- **所要時間目安**: 4分

### Step 20: DBセッションリーク修正 - favorite_serviceのメソッド更新
- **対象ファイル**: `retro_radio/services/favorite_service.py:34-50` (_retry_db_operationなど)
- **変更内容**: 
  - 各メソッドでローカルDBセッションを取得して使用
  - 確実にfinallyブロックでdb.close()を呼ぶか、context managerスタイルに
  - ただし、get_db_sync()は手動close必要なので、try/finallyパターンを使用
- **確認方法**: メソッド内でDBセッションを取得し、finallyでcloseしていることを確認
- **所要時間目安**: 6分

### Step 21: ElevenLabs実装 - 依存関係確認・追加
- **対象ファイル**: `requirements.txt`
- **変更内容**: 
  - `requests` が含まれていることを確認
  - 含まれていない場合は追加
- **確認方法**: 
  ```bash
  grep -i requests requirements.txt || echo "requests==2.31.0" >> requirements.txt
  ```
- **所要時間目安**: 2分

### Step 22: エラーハンドリング統一 - パターン確認
- **対象ファイル**: `retro_radio/utils/errors.py`
- **変更内容**: 
  - 既存の`with_error_handling`デコレータと`handle_error`関数を確認
  - P0で実装したElevenLabsなどでこのパターンに統一できるか検討
  - 今回は確認のみ（P2で統一作業を行う）
- **確認方法**: エラーハンドリングユーティリティが存在することを確認
- **所要時間目安**: 2分

### Step 23: インポート整理 - 未使用インポート削除
- **対象ファイル**: 全変更ファイル
- **変更内容**: 
  - ruffまたは手動で未使用インポートを削除
- **確認方法**: 
  ```bash
  ruff check --select F401 .  # 未使用インポートのみ表示
  ```
  警告が出ないことを確認
- **所要時間目安**: 5分

### Step 24: 最終動作確認・まとめ
- **対象ファイル**: 全変更ファイル
- **変更内容**: 
  - 変更後の動作を確認
- **確認方法**: 
  1. アプリ起動確認
  2. ユーザー登録・ログインフロー動作確認（インメモリ削除後も動作すること）
  3. エクスポート機動作確認
  4. お気に入り機能動作確認
  5. 音声生成機能動作確認（ElevenLabsが設定されていればそれによる）
  6. 既存テスト実行 (`pytest tests/ -v` - 可能な範囲で)
- **所要時間目安**: 10分

## 依存関係マトリクス
| Step | 依存先 | 備考 |
|------|--------|------|
| 2    | 1      | コメント追加後の実際の削除 |
| 3    | 2      | クラス変数削除後のインスタンス変数削除 |
| 4    | 2,3    | コンストラクタ初期化行削除 |
| 5    | 2,3,4  | _save_userメソッド修正 |
| 6    | 2,3,4,5| ユーザー取得メソッド簡素化 |
| 8    | 7      | 順次スリープ削除 |
| 9    | 7,8    |  |
| 10   | 7,8,9  |  |
| 11   | 7,8,9,10|  |
| 13   | -      | export_service独立修正 |
| 14   | -      | favorite_service独立修正 |
| 15   | 14     | キャッシュ修正の改良版 |
| 16   | 14,15  | favorite_serviceの関連修正 |
| 17   | -      | DBセッションパターン変更の基礎 |
| 18   | 17     | export_serviceの具体的実装 |
| 19   | 17     | favorite_serviceの具体的実装 |
| 20   | 18,19  | 各サービスのメソッドレベル修正 |
| 22   | -      | エラーハンドリング確認（独立） |
| 23   | 1-21   | すべての変更後のクリーンアップ |
| 24   | 1-23   | 最終動作確認 |

## 完了定義
- [x] 全24ステップ実装完了
- [ ] 既存テスト全パス (`pytest tests/ -v`) 
- [ ] 新規テスト追加（該当する場合は別途）
- [ ] リンター/型チェック通過 (`ruff check .`, `mypy .`)
- [x] 手動動作確認完了（ユーザーフロー・エクスポート・お気に入り・DBセキュリティ）