# レトロラジオ・タイムマシン 実装計画書（24ステップ）

この計画書は、コードレビューで指摘された問題を修正し、リグレッションを防止するためのテストを組み込んだ詳細な実装計画です。  
各ステップは小さく、独立して実行・検証可能な単位に分割しています。

---

## Step 1: フロントエンド統一の方針決定
- Streamlit（app.py）とFastAPI+Pure SPA（静的ファイル）の両方を維持するのではなく、**Pure SPA（FastAPIバックエンド + 静的HTML/CSS/JSフロントエンド）** に統一することを決定。
- これにより、状態管理の二重化を避け、デプロイをシンプルにする。

## Step 2: Streamlit関連コードの削除／無効化
- `app.py` をバックエンド専用のマイクロサービスとして残すか、または完全に削除し、FastAPI サーバー（`retro_radio/server.py`）のみを起動ポイントとする。
- ここでは、`app.py` は残すが、`streamlit run` ではなく FastAPI 経由でのみアクセス可能とするようルーティングを調整（後で静的ファイルをマウント）。

## Step 3: 静的ファイルディレクトリの整備
- `static/` ディレクトリ内の `index.html`, `app.js`, `app.css` がフロントエンドのエントリポイントになることを確認。
- 必要に応じて、画像やフォントなどのアセットを `static/assets/` 以下に配置。

## Step 4: static/app.js の修復
- 現在破損している `static/app.js` （末尾のみ）を、以下の機能を含む完全な実装に置き換える：
  - ページロード時の初期化（年選択スライダー、モードタブ、シニアトグル）。
  - API キー入力モーダルの表示・保存（localStorage へ保存）。
  - 「ラジオを再生する」ボタンのハンドラ：/api/generate エンドポイントへ POST、結果取得。
  - 原稿表示エリアへのスクリプト注入。
  - TTS 音声の自動再生と、メドレー楽曲のプレビューURLを使った自動連続再生（Web Audio API）。
  - エラーハンドリング（API キー未設定、ネットワークエラー等）。
  - アクセシビリティ（キーボードナビゲーション、フォーカス管理）。
- コードは ES6 標準、読みやすさのためにコメントを適切に付与。

## Step 5: ui/components.py の文字列リテラルエスケープ修正
- ファイル内の f''' ... ''' 形式でのダブルクォート重複を修正。
  - 例：`f'''<div class=""main-title"">` → `f'<div class="main-title">'` または `f'''<div class="main-title">'''`
- 全ての同様なパターンを検索し、正しいクォートエスケープに置き換える。

## Step 6: セキュリティ向上 – API キー保存方法の変更
- `.gemini_api_key` ファイルへの書き込みを廃止し、API キーは**環境変数 GEMINI_API_KEY** からのみ読み込むように変更。
  - `retro_radio/config.py` の `gemini_api_key` フィールドは、環境変数から取得し、デフォルトは空文字列のままにする（ただし必須とするバリデーションを追加）。
- `.gemini_api_key` ファイルを `.gitignore` に追加し、誤ってコミットされないようにする。

## Step 7: CORS 設定の見直し
- `retro_radio/server.py` の CORS ミドルウェア設定を変更：
  - 開発環境では `allow_origins=["*"]` のままでもよいが、本番環境では明示的に許可するオリジンリスト（例: `["https://yourdomain.com"]`）を環境変数で設定可能にする。
  - 最低でも `allow_credentials=True` は必要に応じてだけにし、不要なリスクを低減。

## Step 8: アプリケーションシャットダウン時のリソース解放
- `retro_radio/utils/async_runner.py` の `shutdown_executor()` を呼び出す仕組みを追加。
  - FastAPI の `@app.on_event("shutdown")` デコレータを使用し、アプリケーション終了時にスレッドプールを graceful にシャットダウン。
- 同様に、起動時イベントでロガーや他のリソースの初期化も明示的に行う（現状でも行われているが、整合性を取る）。

## Step 9: 依存関係の整理（requirements.txt）
- フロントエンドが Pure SPA に統合されたため、**streamlit** は不要になる（バックエンドのみで使われている場合は除く）。
  - 必要に応じて、`requirements.txt` から `streamlit>=1.35.0` を削除。
  - 代わりに、開発用に `python-multipart`（FastAPI フォーム用）などが必要なら追加。

## Step 10: OpenAPI (Swagger) ドキュメントの有効化
- `retro_radio/server.py` の FastAPI インスタンス生成時に、`docs_url="/docs", redoc_url="/redoc"` を明示的に有効化（デフォルトで有効だが、明示する）。
- エンドポイントごとに説明（description）、レスポンスモデル、例外レスポンスを追記し、ドキュメントの質を向上。

## Step 11: 環境変数のサンプルファイル作成
- プロジェクトルートに `.env.example` を作成し、以下のような内容を記載：
  ```
  GEMINI_API_KEY=your_gemini_api_key_here
  # オプション
  ELEVENLABS_API_KEY=your_elevenlabs_api_key
  DATABASE_URL=sqlite:///./retro_radio.db
  ```
- これにより、新規開発者が環境構築しやすくなる。

## Step 12: ロギング設定の環境変数対応
- `retro_radio/utils/logging_config.py` の `setup_logging` 関数を修正し、ログレベルを環境変数 `LOG_LEVEL`（デフォルト: INFO）から取得可能にする。
- これにより、本番では WARNING 以上、開発では DEBUG などと切り替えやすくなる。

## Step 13: データベースマイグレーション手順のドキュメント化
- Alembic を使ったマイグレーションフローを `docs/migrations.md` （または README に追記）に記載：
  - 初期化: `alembic init alembic`
  - モデル変更後のマイグレーションスクリプト生成: `alembic revision --autogenerate -m "describe change"`
  - マイグレーション適用: `alembic upgrade head`
- 現在のコードでは `init_db()` がありますが、本番運用では Alembic が望ましいため、その旨を明示。

## Step 14: API エンドポイント例外ハンドリングの統一
- `retro_radio/server.py` に、カスタム例外ハンドラ (`@app.exception_handler(AppError)`) を追加し、`AppError` サブクラスを適切な HTTP ステータスコード（400, 403, 503 など）に変換。
- これにより、エラー時にHTMLではなくJSONエラーレスポンスが返り、フロントエンドでのハンドリングがしやすくなる。

## Step 15: ユーティリティモジュールの単体テスト追加
- `tests/test_utils_validators.py`, `tests/test_utils_errors.py` などを新規作成し、以下を検証：
  - バリデータ（年月日の組み合わせチェック、API キー形式チェック）の正常・異常ケース。
  - エラーハンドリングデコレータが例外を適切にラップし、ユーザーメッセージを返すこと。

## Step 16: フォールバック機構のテスト追加
- `tests/test_fallback_mechanism.py` を作成し、以下をシナリオテスト：
  - 環境変数 `GEMINI_API_KEY` が空または無効な場合でも、`/api/generate` エンドポイントがフォールバック原稿・フォールバック楽曲を返却し、`audio_url` が null またはフォールバック音声になることを確認。
  - 各モード（normal, care_recreation, anniversary）でのフォールバック挙動。

## Step 17: TTS キャッシュ機構のテスト追加
- `tests/test_tts_cache.py` を作成し、以下を検証：
  - 同じテキストに対する TTS リクエストが2回目以降はキャッシュから返される（ファイルシステム上のキャッシュファイルが再利用される）。
  - キャッシュディレクトリが適切に作成され、ファイル名が SHA-256 ハッシュであること。

## Step 18: 性能テスト（レスポンス時間）の追加
- `tests/test_performance_response_time.py` を作成し、以下を測定：
  - `/api/generate` エンドポイントの平均レスポンス時間が、ネットワーク遅延を除いて 5秒以内（フォールバック時は 2秒以内）であることを確認（モックを使用して外部API呼び出しを省略してもよい）。
- CI で基準を超えた場合は失敗とする。

## Step 19: セキュリティテストの追加
- `tests/test_security.py` を作成し、以下を検証：
  - CORS ヘッダーが設定通りに返されること（特定オリジンの場合はそのオリジンのみ許可、 credentials の扱い）。
  - API キーが漏洩していないか（エラーメッセージにキーが含まれていないか）。
  - 入力バリデーションによって、年月日の不正な組み合わせ（例: 2月30日）が 400 エラーで返されること。

## Step 20: ドキュメントの充実（README の見直し）
- `README.jp.md`（または README.md）を更新し、以下の項目を詳細に記載：
  - クイックスタート（Windows バッチファイル、または `uvicorn retro_radio.server:app --reload` の手順）。
  - 開発環境の構築手順（リポジトリクローン → `pip install -r requirements.txt` → `.env` 作成 → DB マイグレーション → アプリ起動）。
  - テストの実行方法（`python -m pytest`、`python -m pytest tests/test_performance_*.py` など）。
  - デプロイガイド（Dockerfile の例、Systemd サービスファイルの例、クラウドデプロイのポイント）。
  - API キーの取得方法（Google AI Studio）と、環境変数での設定方法。

## Step 21: 起動スクリプトの見直し
- `run_retro_radio.bat` を開発用と本番用に分割、または環境変数の設定例を含む形に更新：
  - バッチファイル内で `set GEMINI_API_KEY=...` のプレースホルダーを残し、ユーザーが自分で設定するよう促すコメントを追加。
  - 必要に応じて、PowerShell 版やシェルスクリプト版も用意。

## Step 22: CI/CD パイプラインのサンプル追加（GitHub Actions）
- `.github/workflows/ci.yml` を作成し、以下を自動実行：
  - `pytest` によるテストスイート実行。
  - `flake8` または `pylint` による lint チェック（Step 23参照）。
  - ビルド確認（`pip install -r requirements.txt` が成功すること）。
  - 必要に応じて、セキュリティスキャン（bandit）も追加。

## Step 23: コーディングスタイルリンターの導入
- `flake8`（または `pylint`）を `requirements-dev.txt`（または `requirements.txt` の dev セクション）に追加。
- `.flake8` 設定ファイルを作成し、最大行長、無視するエラーコードなどをプロジェクトに合わせて調整。
- CI ステップで lint を実行し、コード品質を維持。

## Step 24: 最終検証 – E2E ライフサイクルテストの実行
- 既存の `scratch_test_e2e_flow.py` を拡張または新規作成し、以下のフローを自動テストとして実行：
  1. アプリ起動（バックエンドのみ）。
  2. ダミーの API キー（またはモック）を環境変数に設定。
  3. 正常なリクエスト（例: 1980年9月24日、モード normal）を送信し、原稿、楽曲リスト、audio_url が返却されること。
  4. audio_url に GET リクエストを送り、音声ファイルが取得できること（200 OK、Content-Type: audio/mpeg）。
  5. 各モード（care_recreation, anniversary）でも同様に基本的なレスポンスが返ること。
  6. エラーケース（API キー未設定）でもフォールバックレスポンスが返ること。
- 全テストがパスし、警告がゼロであることを確認して完了とする。

---

以上が、**24ステップ**に分割された詳細実装計画およびリグレッション防止のためのテスト計画です。  
各ステップを順に実行し、完了したら次のステップに進むことで、品質を保ちながら着実に改善を進められます。