"""`retro_radio.api.v1.get_current_user` のトークン解決のテスト。

既存の `test_api_access_control.py` は「解決された用户が Pro かどうか」だけを
見ており、`Authorization` ヘッダーから用户を引く手順（12〜31行）は
未実行だった。ここは認証の入口なので、
- ヘッダー書式が崩れている場合に必ず None（未認証）になる
- 検証器が無い場合に None になる（fail-closed）
という2点を中心に固定する。
"""

import asyncio
import logging
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from retro_radio.api import v1
from retro_radio.api.v1 import get_current_user


def _run(**kwargs):
    """`generate_radio` は未 include のスタブなので HTTP 経由で到達できない。"""
    return asyncio.run(v1.generate_radio(**kwargs))


@contextmanager
def patched_verifier(return_value):
    """`Authenticator.verify_api_token` を差し切って、呼出を観測できるようにする。"""
    from retro_radio.auth import Authenticator

    with patch.object(
        Authenticator, "verify_api_token", return_value=return_value, create=True
    ) as verifier:
        yield verifier


def remove_verifier(monkeypatch):
    """`Authenticator.verify_api_token` を未実装の状態にする。"""
    from retro_radio.auth import Authenticator

    monkeypatch.delattr(Authenticator, "verify_api_token", raising=False)


# --- ヘッダーなし / 書式違い ------------------------------------------------------
@pytest.mark.parametrize(
    "authorization",
    [None, "", "   ", "Basic dXNlcjpwYXNz", "Token abc123", "Bearer", "bearer"],
)
def test_non_bearer_or_empty_header_is_unauthenticated(authorization):
    assert get_current_user(authorization=authorization) is None


def test_bearer_with_blank_token_is_unauthenticated():
    assert get_current_user(authorization="Bearer ") is None
    assert get_current_user(authorization="Bearer      ") is None


def test_scheme_match_is_case_insensitive():
    """RFC 7235 の auth-scheme は大文字小文字を区別しない。"""
    with patched_verifier("user-1"):
        assert get_current_user(authorization="bEaReR token-abc") == "user-1"
        assert get_current_user(authorization="BEARER token-abc") == "user-1"


def test_token_is_trimmed_before_verification():
    with patched_verifier("user-1") as verifier:
        get_current_user(authorization="Bearer   spaced-token  ")

    verifier.assert_called_once_with("spaced-token")


# --- 検証器の導入 -----------------------------------------------------------------
def test_resolved_user_is_passed_through():
    user = object()
    with patched_verifier(user):
        assert get_current_user(authorization="Bearer tok") is user


def test_verifier_receives_only_the_token():
    with patched_verifier("user-1") as verifier:
        get_current_user(authorization="Bearer my-token")

    verifier.assert_called_once_with("my-token")


def test_invalid_token_resolves_to_none():
    """検証器が None を返したら未認証として扱う（例外を投げない）。"""
    with patched_verifier(None):
        assert get_current_user(authorization="Bearer bad-token") is None


# --- 検証器が未実装の場合（fail-closed） -----------------------------------------
def test_missing_verifier_falls_back_to_unauthenticated(monkeypatch, caplog):
    """`Authenticator.verify_api_token` が無い状態では必ず None を返す。

    警告は出すが、この API は Pro 専用なので「検証できない = 通さない」が
    唯一の安全な既定になる。
    """
    remove_verifier(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=v1.logger.name):
        result = get_current_user(authorization="Bearer some-token")

    assert result is None
    assert any("未実装" in record.message for record in caplog.records)


def test_missing_verifier_does_not_short_circuit_blank_token(monkeypatch, caplog):
    """トークンが空なら検証器の有無に関係なく None（警告も出さない）。"""
    remove_verifier(monkeypatch)

    with caplog.at_level(logging.WARNING, logger=v1.logger.name):
        result = get_current_user(authorization="Bearer ")

    assert result is None
    assert caplog.records == []


def test_unverifiable_token_cannot_reach_pro_endpoint(monkeypatch):
    """検証器が無い状態で Pro 専用エンドポイントまで到達しないこと。

    偽造トークンを渡しても user は None に解決され、403 で止まる。
    """
    remove_verifier(monkeypatch)

    user = get_current_user(authorization="Bearer forged-token")

    assert user is None
    assert v1._is_pro_plan(user) is False

    with pytest.raises(HTTPException) as exc:
        _run(year=2020, current_user=user)

    assert exc.value.status_code == 403
    assert "API access requires PRO plan" in exc.value.detail
