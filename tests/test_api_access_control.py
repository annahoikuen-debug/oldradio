"""ルーターの平坦化と「v1 経路が存在しない」ことの検証。

`api/v1.py`（Pro プラン API のスタブ）は死蔵コードのため削除した
（コードレビュー D-7）。ここに残すのは、`include_router` の平坦化が
機能していること（`/api/me` が実経路として見えていること）と、
v1 経路が復活していないことの2つだけ。
"""

from fastapi.routing import APIRoute


def _paths(routes):
    for route in routes:
        if isinstance(route, APIRoute):
            yield route.path
        original = getattr(route, "original_router", None)
        if original is not None:
            yield from _paths(original.routes)
        nested = getattr(route, "routes", None)
        if nested:
            yield from _paths(nested)


def test_no_v1_routes_and_me_is_mounted():
    """`/api/me` は実経路として存在し、`/api/v1` は存在しない。

    .. warning::
       かつては ``{getattr(route, "path", None) for route in app.routes}``
       に `"/api/v1" not in paths` を確かめるだけの実装で、
       **常に空振りしていた**。FastAPI 0.11 以降 `include_router` は
       path を持たない `_IncludedRouter` 1 件だけを `app.routes` に
       登録するため、`"/api/v1"` は**経路として存在し得ない**。
       正しくは**平坦化した route** を見る。
    """
    from retro_radio.server import app

    paths = set(_paths(app.routes))

    # 検査が機能していることの担保（me / audit は include 済み）。
    assert "/api/me" in paths, "include_router の平坦化に失敗している"
    assert not any(p.startswith("/api/v1/") for p in paths), paths
    assert not any(p.startswith("/v1/") for p in paths), paths
