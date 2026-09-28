"""审批台前端安全回归（安全专项）：转义 + CSP + 无内联事件处理器。

为什么这个页面值得单独测：它是**人做决策的界面**，字段全部来自 AI 与用户输入
（阶段名、需求文本、证据链接、AI 自审）。此前这些值直接拼进 `innerHTML`，
构造一个标题为 `<img src=x onerror=...>` 的审批单就能在审批人浏览器里执行脚本——
而审批人恰恰是权限最高的角色。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
import tempfile
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

_tmp = tempfile.mkdtemp(prefix="console-xss-")
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/gw.db"

from app.main import app  # noqa: E402
from app.models import utcnow  # noqa: E402

XSS = "<img src=x onerror=alert(1)>"


def _strip_comments(html: str) -> str:
    """注释不参与执行，断言前先去掉（否则注释里解释"为什么不用 onclick"会误报）。"""
    return re.sub(r"<!--.*?-->", "", html, flags=re.S)


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


class TestConsoleAssets:
    def test_console_is_served_without_inline_script_or_handlers(self, client):
        html = _strip_comments(client.get("/").text)
        assert "<script src=\"/app.js\">" in html, "脚本必须外置（否则 CSP 只能开 unsafe-inline）"
        assert "onclick=" not in html, "内联事件处理器会被 CSP 拒绝，且是 XSS 的常见落点"
        assert "Content-Security-Policy" in html
        assert "script-src 'self'" in html and "'unsafe-inline'" not in html

    def test_styles_are_external(self, client):
        assert client.get("/app.css").status_code == 200
        assert "<style>" not in client.get("/").text

    def test_app_js_escapes_every_interpolation(self, client):
        js = client.get("/app.js").text
        assert "function esc(" in js
        # 详情面板的每个字段都必须过 esc()（漏一个就是一条 XSS 路径）
        for field in ("current.stage", "current.requester", "ctx.diff_summary",
                      "ctx.evidence", "risk.level", "risk.score", "ctx.self_review",
                      "ctx.artifact_ref", "a.stage", "a.id"):
            assert f"esc({field}" in js or f"esc(({field}" in js, f"{field} 未转义"

    def test_app_js_has_no_innerhtml_with_raw_value(self, client):
        js = client.get("/app.js").text
        for bad in ("innerHTML = a.", "innerHTML = item.", "innerHTML = ctx."):
            assert bad not in js

    def test_escaping_helper_covers_html_metacharacters(self, client):
        js = client.get("/app.js").text
        for ch in ("&amp;", "&lt;", "&gt;", "&quot;", "&#39;"):
            assert ch in js, f"esc() 未处理 {ch}"


class TestHealthPostureOnConsole:
    def test_console_shows_loose_posture_warning(self, client, monkeypatch):
        """宽松模式必须在审批人眼前显示——决策者有权知道闸门现在是松的。"""
        monkeypatch.delenv("PLATFORM_ENV", raising=False)
        sec = client.get("/health").json()["security"]
        assert sec["compliant"] is False and sec["warnings"]
        js = client.get("/app.js").text
        assert "loadPosture" in js and "治理开关未开" in js


class TestStaticAssetsSurviveTokenMode:
    """鉴权专项的连带坑：开了服务令牌后，页面资源若没被豁免，控制台会**白屏**。

    这个坑真的踩到了（加了 app.js/app.css 但豁免清单只写了 index.html）。
    所以豁免改为"按 static 目录内容"，并在这里逐文件验证——以后新增资源不会再漏。
    """

    def test_every_static_file_is_reachable_without_token(self, client, monkeypatch):
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        static_dir = Path(__file__).resolve().parent.parent / "app" / "static"
        files = [f.name for f in static_dir.iterdir() if f.is_file()]
        assert files, "static 目录不应为空"
        for name in files:
            r = client.get(f"/{name}")
            assert r.status_code == 200, f"{name} 在令牌模式下不可达（HTTP {r.status_code}）→ 页面会白屏"

    def test_api_still_requires_token(self, client, monkeypatch):
        """豁免只覆盖静态资源，业务接口照旧要令牌（别把豁免开成后门）。"""
        monkeypatch.setenv("SERVICE_AUTH_MODE", "token")
        monkeypatch.setenv("SERVICE_AUTH_TOKEN", "svc")
        assert client.get("/approvals").status_code == 401
