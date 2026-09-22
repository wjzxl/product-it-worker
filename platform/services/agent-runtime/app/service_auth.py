"""服务间鉴权：`X-Service-Token` 共享令牌 + "默认拒绝、显式豁免"的中间件。

**这是本仓库所有服务 `app/service_auth.py` 的唯一真源**（新增服务后跑
`python3 platform/scripts/sync_service_auth.py` 同步，`--check` 用于 CI 检查漂移）。

为什么要中间件而不是逐个路由加 `Depends`：鉴权专项清点结果——
7 个服务里 6 个、33 个写接口里 22 个**没有任何鉴权依赖**
（`POST /console/decide` 能替人落决策、`POST /tools/invoke` 能调工具、
`POST /mining/proposals/apply` 能改 SOP 配置、`POST /runs/{id}/resume` 能绕过人审）。
逐个补依赖，漏一个就前功尽弃；中间件按"默认拒绝 + 显式豁免"工作，**新增路由自动被保护**。

模式（`SERVICE_AUTH_MODE`）
| 值 | 行为 |
|---|---|
| `off`（默认） | 不校验，启动打 WARNING 明说"当前无服务间鉴权"（本机开发用） |
| `token` | 所有非豁免请求必须带 `X-Service-Token`（或下面三种可信来源之一） |

`token` 模式下放行的四类调用方：
1. **服务间**：`X-Service-Token: $SERVICE_AUTH_TOKEN`（常量时间比较）；
2. **人类 API 客户端**：`Authorization: Bearer $AUTH_STATIC_TOKEN`（若配置了该变量）；
3. **反向代理后的已登录用户**：`X-Platform-User: <用户名>`，**仅当 `TRUST_PROXY_AUTH=1`**；
   要求入口（Caddy/SSO）剥离客户端自带的同名头——直连端口时该头可被伪造，
   所以服务端口必须只在内网暴露（见 `platform/runbooks/` 与 `Caddyfile`）；
4. 豁免路径（见下）。

豁免（`SERVICE_AUTH_EXEMPT`，逗号分隔的路径前缀；默认 `/health,/docs,/openapi.json,/redoc`）
**只应豁免"自带签名校验"的入站 webhook**。取消豁免前必须确认那条路径真的验了签名，
否则豁免就是后门。
"""
from __future__ import annotations

import hmac
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

DEFAULT_EXEMPT = ("/health", "/docs", "/openapi.json", "/redoc", "/favicon.ico")
SERVICE_TOKEN_HEADER = "x-service-token"
SERVICE_TOKEN_HEADER_HTTP = "X-Service-Token"


class ServiceAuthConfig:
    """每次请求实时读环境变量（配置热更新与测试注入都简单）。"""

    def __init__(self, extra_exempt: tuple[str, ...] = ()) -> None:
        self.mode = os.getenv("SERVICE_AUTH_MODE", "off").strip().lower()
        self.token = os.getenv("SERVICE_AUTH_TOKEN", "")
        self.static_token = os.getenv("AUTH_STATIC_TOKEN", "")
        self.trust_proxy = os.getenv("TRUST_PROXY_AUTH", "0") == "1"
        raw = os.getenv("SERVICE_AUTH_EXEMPT", "")
        self.exempt = DEFAULT_EXEMPT + tuple(extra_exempt) + tuple(p.strip() for p in raw.split(",") if p.strip())
        for entry in self.exempt:  # "/" 当前缀会豁免一切 —— 只允许它做精确匹配，其余照常
            if entry == "":
                raise ValueError("SERVICE_AUTH_EXEMPT 含空项（会豁免所有路径）")

    def describe(self) -> dict:
        return {
            "mode": self.mode,
            "token_configured": bool(self.token),
            "static_token_configured": bool(self.static_token),
            "trust_proxy_auth": self.trust_proxy,
            "exempt": list(self.exempt),
        }


_STATIC_SUFFIXES = (".html", ".js", ".css", ".ico", ".png", ".svg", ".map", ".woff", ".woff2")


def _is_exempt(path: str, exempt: tuple[str, ...]) -> bool:
    """豁免匹配：`/` 与静态文件精确匹配，目录项按路径边界前缀匹配。

    绝不做"裸前缀"匹配——`/health` 作前缀会顺带豁免 `/healthz-admin`，`/` 作前缀会豁免全部。
    """
    for entry in exempt:
        if entry in ("/", ""):
            if entry == "/" and path == "/":
                return True
            continue
        if entry.endswith(_STATIC_SUFFIXES):
            if path == entry:
                return True
            continue
        base = entry.rstrip("/")
        if path == base or path.startswith(base + "/"):
            return True
    return False


def outbound_headers(extra: dict | None = None) -> dict:
    """服务间**出站**请求头：`token` 模式下自动带上 `X-Service-Token`。

    鉴权专项的连带必修项：中间件一开，服务之间的调用（agent-runtime→审批网关、
    任务中心→agent-runtime、事件发布→任务中心…）会因为没带令牌全部 401。
    出站头统一从这里取，避免"每个客户端各自记得加"。
    """
    headers = dict(extra or {})
    if os.getenv("SERVICE_AUTH_MODE", "off").strip().lower() == "token":
        token = os.getenv("SERVICE_AUTH_TOKEN", "")
        if token:
            headers.setdefault(SERVICE_TOKEN_HEADER_HTTP, token)
    return headers


def _constant_eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def check_request(request: Request, extra_exempt: tuple[str, ...] = ()) -> tuple[bool, str]:
    """返回 (是否放行, 原因)。抽成纯函数，便于单测与在别处复用。"""
    cfg = ServiceAuthConfig(extra_exempt)
    if cfg.mode != "token":
        return True, "mode=off"
    if _is_exempt(request.url.path, cfg.exempt):
        return True, "exempt"
    supplied = request.headers.get(SERVICE_TOKEN_HEADER, "")
    if cfg.token and supplied and _constant_eq(supplied, cfg.token):
        return True, "service-token"
    auth = request.headers.get("authorization", "")
    if cfg.static_token and auth.lower().startswith("bearer ") and _constant_eq(auth[7:], cfg.static_token):
        return True, "static-bearer"
    if cfg.trust_proxy and request.headers.get("x-platform-user", ""):
        return True, "proxy-user"
    if not cfg.token and not cfg.static_token and not cfg.trust_proxy:
        # token 模式但一个凭据都没配：fail-closed（拒绝），并说清怎么修
        return False, "misconfigured: SERVICE_AUTH_MODE=token 但未配置 SERVICE_AUTH_TOKEN/AUTH_STATIC_TOKEN"
    return False, "missing-or-invalid-credential"


def install_service_auth(app: FastAPI, service: str, extra_exempt: tuple[str, ...] = ()) -> None:
    """挂上统一鉴权中间件。**每个服务都必须在 create_app 里调用**。

    `extra_exempt` 用于"自带签名校验的入站 webhook"（GitLab `/webhooks/gitlab`、
    IM 卡片 `/callbacks/im`、Alertmanager `/alerts/prometheus`）与纯静态审批台页面。
    加豁免前先确认那条路径真的验了签名——否则豁免就是后门。
    """
    cfg = ServiceAuthConfig(extra_exempt)
    if cfg.mode != "token":
        logger.warning(
            "服务间鉴权未开启（SERVICE_AUTH_MODE=%s）：%s 的所有写接口对能连上端口的人开放。"
            "生产请设 SERVICE_AUTH_MODE=token + SERVICE_AUTH_TOKEN，并只在网关后暴露端口。",
            cfg.mode, service,
        )
    else:
        logger.info("服务间鉴权已开启（token 模式）：%s %s", service, cfg.describe())

    @app.middleware("http")
    async def _service_auth_guard(request: Request, call_next):  # type: ignore[no-untyped-def]
        allowed, reason = check_request(request, extra_exempt)
        if not allowed:
            if reason.startswith("misconfigured"):
                logger.error("%s 鉴权配置错误：%s", service, reason)
                status_code = 503
            else:
                logger.warning("%s 拒绝未鉴权请求 %s %s（%s）",
                               service, request.method, request.url.path, reason)
                status_code = 401
            return JSONResponse(
                {"detail": f"服务间鉴权失败（{reason}）：请带 X-Service-Token 或经 SSO 入口访问",
                 "service": service},
                status_code=status_code,
            )
        return await call_next(request)
