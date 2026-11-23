"""网页壳缓存口径：index.html 绝不缓存，哈希产物长期缓存。

回归 2026-11-18 缺陷：FileResponse 默认无 Cache-Control，浏览器按启发式
新鲜度复用旧 index.html，导致重新构建后用户仍看到旧版（旧哈希 chunk 已 404）。
"""
from __future__ import annotations

from legal_workbench.app import dist


def test_index_html_is_no_store(client):
    response = client.get("/")
    assert response.status_code == 200
    control = response.headers.get("cache-control", "")
    assert "no-store" in control
    assert "no-cache" in control
    assert "must-revalidate" in control
    assert response.headers.get("pragma") == "no-cache"
    assert response.headers.get("expires") == "0"


def test_spa_route_shell_is_no_store(client):
    response = client.get("/prototype/progress")
    assert response.status_code == 200
    assert "no-store" in response.headers.get("cache-control", "")


def test_hashed_asset_is_immutable(client):
    # 取构建产物中真实存在的一个哈希文件
    assets = sorted((dist / "assets").glob("index-*.js"))
    assert assets, "前端尚未构建"
    name = assets[0].name
    response = client.get(f"/assets/{name}")
    assert response.status_code == 200
    control = response.headers.get("cache-control", "")
    assert "immutable" in control
    assert "max-age=31536000" in control


def test_api_has_no_static_cache_header(client):
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    assert "cache-control" not in response.headers