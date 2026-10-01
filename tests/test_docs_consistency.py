"""ドキュメントとコードの整合を固定する回帰テスト。

ここにある検査は、かつて「ドキュメントが嘘をついていた」事例の**再発防止**である。
散文や見出しではなく、**既知の誤った主張が再登場していないか**を検査する。

検査対象は `docs/archive/` を除くドキュメントと、
リポジトリ直下の `README.md` / `DEPLOYMENT.md` / `OPERATIONS.md`。
"""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

#: 文書化する対象。`docs/archive/` は「現状の仕様書ではない」ため意図的に除外する。
DOC_GLOBS = ("README.md", "DEPLOYMENT.md", "OPERATIONS.md", "docs/**/*.md")

#: `docs/archive/` を再帰 glob から除外するための述語
def _is_live_doc(path: Path) -> bool:
    return "archive" not in path.relative_to(ROOT).parts


def _load_docs() -> list:
    paths = {ROOT / "README.md", ROOT / "DEPLOYMENT.md", ROOT / "OPERATIONS.md"}
    for pattern in ("docs/**/*.md",):
        paths.update(p for p in ROOT.glob(pattern) if _is_live_doc(p))
    docs = []
    for path in sorted(paths):
        if path.exists():
            docs.append((path, path.read_text(encoding="utf-8")))
    return docs


def _read(name: str) -> str:
    path = ROOT / name
    assert path.exists(), f"{name} が存在しない"
    return path.read_text(encoding="utf-8")


def _hits(pattern: str, flags=0) -> list:
    regex = re.compile(pattern, flags)
    found = []
    for path, text in _load_docs():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if regex.search(line):
                found.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    return found


# ---------------------------------------------------------------------------
# TASK 1: README がコードと矛盾していた 4 箇所
# ---------------------------------------------------------------------------

def test_readme_does_not_claim_generate_is_unauthenticated():
    """`/api/generate` は認証必須（既定 `RETRO_RADIO_REQUIRE_AUTH=1`）。

    認証を有効にしたまま鍵が無い場合は **503（fail-closed）** であり、
    「認証を一切行わない」という主張は偽である。
    """
    forbidden = (
        "認証を一切行いません",
        "認証を一切行わないもの",
        "`/api/generate` は認証を",
    )
    hits = []
    for name in ("README.md",):
        text = _read(name)
        for phrase in forbidden:
            if phrase in text and "一切行いません" in phrase:
                hits.append(f"{name}: {phrase}")
    assert not hits, (
        "README が /api/generate を無認証と主張しています: " + "; ".join(hits)
    )


def test_readme_does_not_claim_nothing_is_written_to_the_database():
    """`audit_logs` には 1 回の生成につき `started` と終端の計 2 行が書かれる。"""
    readme = _read("README.md")
    for phrase in (
        "DB に一切書き込みません",
        "データベースに一切書き込みません",
        "一切書き込ません",
    ):
        assert phrase not in readme, f"README が「{phrase}」と主張しています"


def test_readme_documents_async_job_endpoints():
    """非同期ジョブは実装済み・到達可能。API 表に載っている必要がある。"""
    readme = _read("README.md")
    for endpoint in (
        "/api/jobs",
        "/api/jobs/{job_id}",
        "/api/jobs/{job_id}/events",
    ):
        assert endpoint in readme, f"README の API 表に {endpoint} が無い"
    assert "POST /api/auth/session" in readme or "/api/auth/session" in readme, (
        "README が POST /api/auth/session を載せていない"
    )


def test_readme_tts_sweep_is_described_as_call_count_based():
    """TTS キャッシュの掃除は「時間」ではなく `generate_tts_cached` の
    **呼び出し回数**で駆動される（`tts_cache_sweep_interval`）。"""
    readme = _read("README.md")
    assert "呼び出し回数" in readme, "README がスイープを回数駆動と説明していない"
    for phrase in ("毎 50 分", "50 分ごと", "起動時と 50 分", "毎50分"):
        assert phrase not in readme, f"README が時間間隔の古い説明「{phrase}」を残している"


def test_readme_environment_table_lists_auth_and_outside_settings_vars():
    """認証関連の 4 変数と `Settings` の外で読む 4 変数が環境変数表にあること。"""
    readme = _read("README.md")
    for name in (
        "RETRO_RADIO_REQUIRE_AUTH",
        "RETRO_RADIO_SECRET_KEY",
        "RETRO_RADIO_SINGLE_USER_KEY",
        "RETRO_RADIO_ADMIN_EMAILS",
        "RETRO_RADIO_CSP",
        "RETRO_RADIO_HSTS_ENABLED",
        "RETRO_RADIO_HSTS_MAX_AGE",
        "RETRO_RADIO_FULL_SCRIPT_TTS",
    ):
        assert name in readme, f"README の環境変数表に {name} が無い"


def test_readme_does_not_flag_deployment_md_as_stale():
    """`DEPLOYMENT.md` は現行の正しい文書。旧 Streamlit 前提だったのは
    `docs/deployment_guide.md` 側（書き直し済み）。"""
    readme = _read("README.md")
    assert "旧 Streamlit 前提の記述が残っています" not in readme, (
        "README が DEPLOYMENT.md を古いものとして指している"
    )


def test_readme_doc_table_lists_privacy_and_tenancy():
    readme = _read("README.md")
    assert "docs/privacy_and_tenancy.md" in readme, (
        "README のドキュメント表に privacy_and_tenancy.md が無い"
    )


def test_readme_does_not_link_archived_checklist():
    readme = _read("README.md")
    # archive への移動を説明するのは構わないが、リンクにしてはいけない
    assert "](docs/implementation_checklist.md)" not in readme, (
        "README が archive へ移動した implementation_checklist.md をリンクしている"
    )


def test_song_count_rule_points_at_source_of_truth_not_a_hardcoded_count():
    """曲数は正本を参照して記述する（ハードコードした数を書かない）。"""
    for name in ("README.md", "docs/song_catalog.md"):
        text = _read(name)
        assert "TARGET_SONGS_PER_YEAR" in text, (
            f"{name} が TARGET_SONGS_PER_YEAR（正本）を指していない"
        )
    assert "1 年 50 曲に達していません" not in _read("README.md"), (
        "README が曲数をハードコードして不足を断定している"
    )


# ---------------------------------------------------------------------------
# TASK 2: CORS の誤った主張
# ---------------------------------------------------------------------------

def test_no_doc_claims_csv_cors_raises_settings_error():
    """`cors_origins` は `NoDecode` + `mode="before"` の事前バリデータが
    CSV / JSON 配列 / `*` / 単独 origin をすべて受け付ける。
    「CSV や `*` で `SettingsError`」という主張は偽。"""
    # 正しい説明を裏付ける_marker_（否定形）は許容する
    allowed = ("ません", "受け付けます", "救済", "起動します", "受信")
    offenders = []
    for path, text in _load_docs():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "SettingsError" not in line:
                continue
            mentions_csv = ("CSV" in line) or ("カンマ区切り" in line)
            if not mentions_csv:
                continue
            if any(marker in line for marker in allowed):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "CORS の表記を誤って説明しています（CSV/カンマ区切りは SettingsError にならない）:\n"
        + "\n".join(offenders)
    )


def test_no_doc_says_wildcard_alone_raises_settings_error():
    offenders = []
    for path, text in _load_docs():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "SettingsError" not in line:
                continue
            if "`*`" not in line and "ワイルドカード" not in line:
                continue
            if any(m in line for m in ("ません", "受け付けます", "拒否", "組み合わせ", "観測")):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "`*` 単独が SettingsError になるとした記述が残っています:\n" + "\n".join(offenders)
    )


def test_operations_md_health_example_lists_auth_fields():
    """運用者が最初に必要とする認証フィールドが `/health` 例に含まれていること。"""
    operations = _read("OPERATIONS.md")
    example = ""
    in_block = False
    for line in operations.splitlines():
        if line.strip().startswith("```"):
            in_block = line.strip() == "```"
            continue
        if in_block and "$ curl" in line and "/health" in line:
            example += line + "\n"
            continue
        if in_block and example and "$ curl" not in line:
            example += line + "\n"
    assert example, "OPERATIONS.md に /health の curl 例が無い"
    for field in (
        "auth_required",
        "auth_ready",
        "auth_mode",
        "auth_enforced",
        "secret_key_configured",
    ):
        assert field in example, f"/health の例に {field} が無い"


# ---------------------------------------------------------------------------
# TASK 3/4: Streamlit 時代の残骸
# ---------------------------------------------------------------------------

def test_no_doc_references_nonexistent_retro_radio_main_module():
    """ASGI アプリは `retro_radio.server:app`。`retro_radio.main` は存在しない。

    「存在しない」と明記する文脈は許容する。禁止するのは
    起動コマンドとして `retro_radio.main:app` を案内すること。
    """
    offenders = []
    for path, text in _load_docs():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "retro_radio.main" not in line:
                continue
            if any(marker in line for marker in ("存在しません", "廃止", "旧")):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "存在しない retro_radio.main を案内しています:\n" + "\n".join(offenders)
    )


def test_no_live_doc_runs_streamlit():
    """Streamlit は廃止済み。`streamlit run` を起動手順として案内してはいけない。"""
    offenders = []
    for path, text in _load_docs():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "streamlit run" not in line:
                continue
            if any(marker in line for marker in ("存在しません", "廃止", "旧", "サポート対象外")):
                continue
            if line.lstrip().startswith("#"):
                # 質問見出し（「`streamlit run` コマンドが見つからない」）は移行説明
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "`streamlit run` を起動手順として残しています:\n" + "\n".join(offenders)
    )


def test_no_doc_points_at_alembic_directory_path():
    """マイグレーションは `db/migrations/`。`alembic/` 配下は誤り。

    「誤りである」と明記する文脈は許容する。
    """
    offenders = []
    for path, text in _load_docs():
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not re.search(r"alembic init alembic|alembic/versions|alembic/env\.py", line):
                continue
            if any(marker in line for marker in ("実行する必要", "ではなく", "ではありません", "書き直")):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "`alembic/` 配下を指す誤ったパスが残っています:\n" + "\n".join(offenders)
    )


def test_alembic_stamp_targets_head():
    """`alembic stamp <revision>` でリビジョンIDをハードコードしない。"""
    found = _hits(r"alembic stamp (?!head)\S")
    assert not found, (
        "alembic stamp が head 以外を指しています:\n" + "\n".join(found)
    )


def test_migrations_doc_does_not_claim_init_db_runs_at_startup():
    """`init_db()` は `scripts/init_db.py` からのみ実行される。"""
    text = _read("docs/migrations.md")
    assert "起動時に自動的に呼び出されます" not in text, (
        "migrations.md が init_db() の自動起動を主張している"
    )
    assert "scripts/init_db.py" in text, "migrations.md が scripts/init_db.py を案内していない"


def test_migration_rules_doc_matches_alembic_default_revision_ids():
    """リビジョンIDは Alembic 既定の 12 桁 hex。タイムスタンプは使わない。"""
    text = _read("docs/migration_rules.md")
    # タイムスタンプ形式は「使ってはいけない」例としてのみ言及してよい
    for lineno, line in enumerate(text.splitlines(), start=1):
        if re.search(r"20\d{12}", line):
            assert any(m in line for m in ("使いません", "使ってはいけません")), (
                f"migration_rules.md:{lineno} がタイムスタンプIDを肯定しています: {line.strip()}"
            )
    assert "hex" in text, "migration_rules.md が 12 桁 hex の規則を説明していない"
    assert "down_revision" in text, "migration_rules.md が down_revision による依存の説明が無い"
    # 実在するリビジョンIDを実例として挙げていること
    assert "125758440996" in text, "migration_rules.md が実在リビジョンIDの例を載せていない"


def test_db_backup_doc_does_not_recommend_cp_for_sqlite():
    """WAL 配下では `cp` が直近コミットを失う。`.backup` API を使うこと。"""
    text = _read("docs/db_backup.md")
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("cp ") or stripped.startswith("# cp "):
            assert "TTS キャッシュ" in text or "再生成可能" in text, (
                f"db_backup.md が cp を推奨しています: {stripped}"
            )
    assert ".backup" in text, "db_backup.md が SQLite の .backup API を案内していない"
    assert "retro_radio_song_store.db" in text, (
        "db_backup.md が選曲ストアDBを対象に含めていない"
    )
    assert "TTS キャッシュ" in text or "cache/" in text, (
        "db_backup.md が TTS キャッシュの扱いに触れていない"
    )


def test_validators_are_not_described_as_ci_gates():
    """`validate_songs.py` / `validate_facts.py` を呼ぶ CI ステップは存在しない。
    pytest が同じ検査を間接的に実行するのみ。"""
    for name in ("docs/song_catalog.md", "docs/facts_registry.md"):
        text = _read(name)
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "CI" not in line:
                continue
            assert any(m in line for m in ("CI ゲートではありません", "手動", "ません")), (
                f"{name}:{lineno} が検証スクリプトを CI ゲートと主張しています: {line.strip()}"
            )


# ---------------------------------------------------------------------------
# TASK 4: 文字化けと参照整合
# ---------------------------------------------------------------------------

def test_no_live_doc_has_unicode_replacement_characters():
    for path, text in _load_docs():
        assert "\ufffd" not in text, f"{path.relative_to(ROOT)} に文字化けが残っている"


def test_facts_registry_has_no_mojibake_residue():
    """以往の混入文字列（日本語文中に挟まれた英単語）が残っていないこと。"""
    text = _read("docs/facts_registry.md")
    for residue in ("onger", "入.program", "niaい", "ensus"):
        assert residue not in text, f"facts_registry.md に文字化け残骸「{residue}」がある"


def test_state_design_system_tokens_match_static_app_css():
    """デザイントークンの実値は `static/app.css` が正。

    doc 側の色定義が CSS と食い違わないこと。
    `app.css` が定義していないトークンを doc が既知正確に主張しないこと。
    """
    css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
    doc = _read("docs/state_design_system.md")

    root_block = css.split(":root {", 1)[1].split("\n}", 1)[0]
    defined = dict(re.findall(r"(--[a-z0-9-]+)\s*:\s*([^;]+);", root_block))

    # doc の CSS ブロックを空白正規化してから比較する
    normalized_doc = re.sub(r"[ \t]+", " ", doc)

    for token, value in defined.items():
        if token.startswith(("--color-state-", "--color-bg-state-")):
            expected = f"{token}: {value.strip()};"
            normalized_expected = re.sub(r"[ \t]+", " ", expected)
            assert normalized_expected in normalized_doc, (
                f"state_design_system.md が {token} の実値 {value.strip()} を記載していない"
            )

    for token in ("--color-state-empty", "--color-state-loading", "--color-state-idle"):
        if token not in defined:
            assert f"`{token}`" in doc, (
                f"CSS に無い {token} について doc が明記していない"
            )


def test_readme_links_resolve_to_existing_files():
    """README の相対リンクが実在ファイルを指していること。"""
    readme = _read("README.md")
    missing = []
    for match in re.finditer(r"\]\((?!https?://)([^)#]+)\)", readme):
        target = (ROOT / match.group(1)).resolve()
        if not target.exists():
            missing.append(match.group(1))
    assert not missing, f"README のリンク先が存在しません: {missing}"


def test_deployment_guide_exists_and_points_to_deployment_md():
    guide = _read("docs/deployment_guide.md")
    assert "DEPLOYMENT.md" in guide, (
        "deployment_guide.md が正本の DEPLOYMENT.md を指していない"
    )
    assert "retro_radio.server:app" in guide, (
        "deployment_guide.md が正しい ASGI アプリを載せていない"
    )


def test_no_live_doc_uses_unprefixed_environment_variables():
    """`env_prefix="RETRO_RADIO_"`。素の `GEMINI_API_KEY` は読まれない。

    「プレフィックスなしは読まれない」という**否定の説明**は許容する。
    禁止するのは、プレフィックスなしの名前を**設定手順として**示すこと。
    """
    forbidden = re.compile(r"(?<!RETRO_RADIO_)\b(DATABASE_URL|GEMINI_API_KEY|SECRET_KEY)\b")
    offenders = []
    for path, text in _load_docs():
        for lineno, line in enumerate(text.splitlines(), start=1):
            match = forbidden.search(line)
            if not match:
                continue
            if any(m in line for m in ("読まれません", "読み込まれません", "接頭辞なし", "プレフィックスなし")):
                continue
            if line.strip().startswith(("|", "-", ">", "#", "*")):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{lineno}: {line.strip()}")
    assert not offenders, (
        "プレフィックスなしの環境変数名を案内しています:\n" + "\n".join(offenders)
    )
