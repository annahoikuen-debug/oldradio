"""運用ドキュメント（OPERATIONS.md）のテスト。

見出し文言への硬编码をやめ、「TJ して困るpayer 事实上が書かれているか」を検証する。
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _read_operations() -> str:
    path = ROOT / "OPERATIONS.md"
    assert path.exists(), "OPERATIONS.md が存在しない"
    return path.read_text(encoding="utf-8")


def test_operations_md_exists():
    assert (ROOT / "OPERATIONS.md").exists()


def test_operations_md_is_utf8():
    content = _read_operations()
    assert "�" not in content, "文字化け（Unicode 置換文字）が残っている"


def test_operations_md_required_topics():
    content = _read_operations()
    for heading in (
        "トラブルシューティングガイド",
        "Gemini APIキー",
        "音声が再生されない",
        "ページが読み込み途中で止まる",
        "運用チェックリスト",
    ):
        assert heading in content, f"OPERATIONS.md に「{heading}」が無い"


def test_operations_documents_cors_settingserror():
    """CORS 許可オリジスは JSON 配列でないと起動しない（既知の落とし穴を記載）"""
    content = _read_operations()
    assert "cors_origins" in content
    assert "RETRO_RADIO_CORS_ORIGINS" in content
    assert "json" in content.lower() or "JSON" in content


def test_operations_documents_foreign_key_migration():
    """FK 制約対応としてマイグレーションが必要であることを記載"""
    content = _read_operations()
    assert "FOREIGN KEY" in content or "外部キー" in content or "foreign key" in content.lower()
    assert "alembic" in content.lower()


def test_operations_documents_health_endpoint():
    content = _read_operations()
    assert "/health" in content


def test_operations_documents_rate_limit_503():
    """同時実行上限（503）が運用上想定内の挙動であることの記載"""
    content = _read_operations()
    assert "503" in content
