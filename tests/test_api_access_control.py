"""`retro_radio.api.v1` のアクセス制御の検証。

注意: `api/v1.py`（Pro プラン API）は `server.py` に include されていない
（未実装スタブ）。そのため HTTP 経由では到達できず、関数を直接呼んで検証する。

Wave 1 で:
  - `token` クエリパラメータ → `authorization` ヘッダー
  - `current_user.get("plan")` → オブジェクト / dict の両方に対応
"""

import asyncio
from unittest.mock import patch

import pytest
from fastapi import HTTPException

from retro_radio.api.v1 import generate_radio
from retro_radio.models.user import PlanType


def _run(**kwargs):
    return asyncio.run(generate_radio(**kwargs))


# --- プラン判定 ----------------------------------------------------------------
def test_free_user_is_rejected():
    with patch("retro_radio.api.v1.get_current_user", return_value={"plan": "free"}):
        with pytest.raises(HTTPException) as exc:
            _run(year=2023, current_user={"plan": "free"})
    assert exc.value.status_code == 403
    assert "API access requires PRO plan" in exc.value.detail


def test_anonymous_user_is_rejected():
    with patch("retro_radio.api.v1.get_current_user", return_value=None):
        with pytest.raises(HTTPException) as exc:
            _run(year=2023, current_user=None)
    assert exc.value.status_code == 403


def test_premium_user_is_rejected():
    """PREMIUM は PRO ではない"""
    with patch("retro_radio.api.v1.get_current_user", return_value={"plan": "premium"}):
        with pytest.raises(HTTPException) as exc:
            _run(year=2023, current_user={"plan": "premium"})
    assert exc.value.status_code == 403


# --- dict / オブジェクト両対応 --------------------------------------------------
def test_plan_from_user_object():
    """User オブジェクト（PlanType enum）でも PRO と判定できる"""
    from retro_radio.models.user import User

    user = User(id="pro", email="pro@example.com", hashed_password="h", plan=PlanType.PRO)
    with patch("retro_radio.api.v1.get_current_user", return_value=user):
        result = _run(year=2023, current_user=user)
    assert result["status"] == "accepted"


def test_plan_from_dict():
    with patch("retro_radio.api.v1.get_current_user", return_value={"plan": "pro"}):
        result = _run(year=2023, current_user={"plan": "pro"})
    assert result["status"] == "accepted"


def test_plan_from_object_with_string_attribute():
    """`plan` が文字列を持つオブジェクトにも対応"""
    class _User:
        plan = "pro"

    user = _User()
    with patch("retro_radio.api.v1.get_current_user", return_value=user):
        result = _run(year=2023, current_user=user)
    assert result["status"] == "accepted"


def test_plan_comparison_is_case_insensitive():
    class _User:
        plan = "PRO"

    user = _User()
    with patch("retro_radio.api.v1.get_current_user", return_value=user):
        result = _run(year=2023, current_user=user)
    assert result["status"] == "accepted"


def test_unknown_plan_is_rejected():
    with patch("retro_radio.api.v1.get_current_user", return_value={"plan": "god"}):
        with pytest.raises(HTTPException) as exc:
            _run(year=2023, current_user={"plan": "god"})
    assert exc.value.status_code == 403


def test_missing_plan_attribute_is_rejected():
    with patch("retro_radio.api.v1.get_current_user", return_value=object()):
        with pytest.raises(HTTPException) as exc:
            _run(year=2023, current_user=object())
    assert exc.value.status_code == 403


# --- 未 include であること ------------------------------------------------------
def test_v1_router_is_not_mounted():
    """Pro プラン API は未実装のため server に mount されていない"""
    from retro_radio.server import app

    paths = {getattr(route, "path", None) for route in app.routes}
    assert "/api/v1" not in paths
    assert not any(p and p.startswith("/v1/") for p in paths)


def test_api_v1_import_does_not_require_streamlit():
    import retro_radio.api.v1 as v1

    assert not hasattr(v1, "st")
