"""デプロイ関連ドキュメントのテスト。

旧テストは Streamlit 時代の見出し（"ストリームlitクラウドデプロイ" 等）を
文字列としてハードコードしていたため、A8 が uvicorn 前提へ更新した時点で壊れた。
見出しの文言ではなく「運用上必須の事実が記載されているか」を検証する。
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read(name: str) -> str:
    path = ROOT / name
    assert path.exists(), f"{name} が存在しない"
    return path.read_text(encoding="utf-8")


def test_deployment_md_exists():
    assert (ROOT / "DEPLOYMENT.md").exists()


def test_dockerfile_template_exists():
    assert (ROOT / "Dockerfile.template").exists()


def test_dockerfile_exists():
    assert (ROOT / "Dockerfile").exists()


def test_deployment_md_is_utf8():
    """cp932 誤読されないこと（A8 が CRLF + UTF-8(BOMなし) に統一済み）"""
    content = _read("DEPLOYMENT.md")
    assert content
    assert "�" not in content, "文字化け（Unicode 置換文字）が残っている"


def test_deployment_md_required_sections():
    content = _read("DEPLOYMENT.md")
    for heading in ("前提条件", "ローカルデプロイ", "Dockerデプロイ", "モニタリング項目"):
        assert heading in content, f"DEPLOYMENT.md に「{heading}」が無い"


def test_deployment_md_uses_uvicorn_not_streamlit():
    """起動手順が uvicorn 前提であること（Streamlit 廃止の反映）

    散文・見出しでの Streamlit 言及は移行説明として許容し、
    実際に叩かれるシェルコマンド（```bash ブロック内）だけを検査する。
    """
    content = _read("DEPLOYMENT.md")
    assert "uvicorn" in content
    assert "retro_radio.server:app" in content

    in_bash_block = False
    for line in content.splitlines():
        if line.strip().startswith("```"):
            in_bash_block = line.strip() == "```bash"
            continue
        if not in_bash_block:
            continue
        command = line.strip().lstrip("$ ").strip()
        if not command or command.startswith("#"):
            continue
        assert not command.lower().startswith("streamlit"), (
            f"起動コマンドに Streamlit が残存: {line}"
        )


def test_deployment_md_mentions_health_endpoint():
    content = _read("DEPLOYMENT.md")
    assert "/health" in content


def test_deployment_md_mentions_alembic():
    content = _read("DEPLOYMENT.md")
    assert "alembic" in content.lower()


def test_dockerfile_uses_uvicorn():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "uvicorn" in dockerfile
    assert "retro_radio.server:app" in dockerfile
    assert "streamlit" not in dockerfile.lower()


def test_dockerfile_healthcheck_uses_health_endpoint():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "/health" in dockerfile


def test_deploy_configs_do_not_run_streamlit():
    """デプロイ設定が Streamlit を起動していないこと（言及は移行説明として許容）"""
    for name in ("render.yaml", "fly.toml", "railway.json", "Dockerfile", "Dockerfile.template"):
        path = ROOT / name
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        for line in content.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            assert "streamlit" not in stripped.lower(), f"{name} に streamlit の起動指定が残存: {line}"


def test_deploy_configs_start_uvicorn():
    """デプロイ設定の起動コマンドが uvicorn であること"""
    for name in ("render.yaml", "fly.toml", "Dockerfile"):
        path = ROOT / name
        if not path.exists():
            continue
        content = path.read_text(encoding="utf-8", errors="replace")
        assert "uvicorn" in content, f"{name} に uvicorn 起動コマンドが無い"
        assert "retro_radio.server:app" in content, f"{name} が ASGI アプリを指していない"
