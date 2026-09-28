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

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型标注用；纯配置/姿态逻辑不依赖 Web 框架（CLI 与体检脚本要能直接 import）
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

DEFAULT_EXEMPT = ("/health", "/docs", "/openapi.json", "/redoc", "/favicon.ico")
SERVICE_TOKEN_HEADER = "x-service-token"
SERVICE_TOKEN_HEADER_HTTP = "X-Service-Token"


class ServiceAuthConfig:
    """每次请求实时读环境变量（配置热更新与测试注入都简单）。"""

    def __init__(self, extra_exempt: tuple[str, ...] = (), static_dir=None) -> None:
        self.platform_env = platform_env()
        self.mode = resolve_setting("SERVICE_AUTH_MODE", dev="off", staging="token", prod="token").lower()
        self.token = os.getenv("SERVICE_AUTH_TOKEN", "")
        self.static_token = os.getenv("AUTH_STATIC_TOKEN", "")
        # 代理头信任**必须显式声明**，因此各档位默认都是关：
        # 它在入口剥离同名头的前提下才成立，默认打开等于替部署方做了一个它可能没做的假设。
        self.trust_proxy = resolve_bool("TRUST_PROXY_AUTH", dev=False, staging=False, prod=False)
        raw = os.getenv("SERVICE_AUTH_EXEMPT", "")
        # 静态页面的资源按**目录实际内容**豁免，而不是硬编码文件名：
        # 硬编码的结果是"加一个 app.js 就忘了加豁免 → 页面白屏"（这个坑真的踩到了）。
        # 静态资源不含业务数据，且逐文件精确匹配，不会扩大暴露面。
        static_exempt: tuple[str, ...] = ()
        if static_dir is not None:
            try:
                static_exempt = tuple(f"/{f.name}" for f in sorted(static_dir.iterdir()) if f.is_file())
            except OSError:
                static_exempt = ()
        self.exempt = (DEFAULT_EXEMPT + tuple(extra_exempt) + static_exempt
                       + tuple(p.strip() for p in raw.split(",") if p.strip()))
        for entry in self.exempt:  # "/" 当前缀会豁免一切 —— 只允许它做精确匹配，其余照常
            if entry == "":
                raise ValueError("SERVICE_AUTH_EXEMPT 含空项（会豁免所有路径）")

    def describe(self) -> dict:
        return {
            "platform_env": self.platform_env,
            "mode": self.mode,
            "token_configured": bool(self.token),
            "static_token_configured": bool(self.static_token),
            "trust_proxy_auth": self.trust_proxy,
            "exempt": list(self.exempt),
        }


# ---------------------------------------------------------------------------
# 部署档位（治理默认值取舍的唯一旋钮）
#
# 结论（见 21-治理默认值取舍与部署档位.md）：**严格程度按"失败后果是否可逆"分档**，
# 而不是按"开发便利"一刀切。不可逆的治理动作（人审决策、SOP 配置、代码仓库写入）
# 缺配置时必须 fail-closed；可逆、可事后补齐的动作（工程闸门服务不可用）允许宽松但必须吵。
#
# 于是把"严格 vs 便利"这个取舍**收拢到一个变量**：PLATFORM_ENV=dev|staging|prod。
# 各开关仍可单独显式覆盖（显式值永远优先），但不再需要每个部署各自拍 8 次脑袋。
# ---------------------------------------------------------------------------
PLATFORM_ENVS = ("dev", "staging", "prod")


def platform_env() -> str:
    raw = (os.getenv("PLATFORM_ENV", "") or "").strip().lower()
    if not raw:
        return "dev"
    if raw not in PLATFORM_ENVS:
        logger.warning("PLATFORM_ENV=%r 非法（可用 %s），按 dev 处理（并会体现在 /health 里）",
                       raw, "/".join(PLATFORM_ENVS))
        return "dev"
    return raw


def resolve_setting(name: str, dev: str, staging: str | None = None, prod: str | None = None) -> str:
    """显式环境变量 > 档位默认。`staging`/`prod` 省略时回落到 `dev` 的值。"""
    explicit = os.getenv(name, "")
    if explicit != "":
        return explicit
    env = platform_env()
    if env == "prod":
        return prod if prod is not None else dev
    if env == "staging":
        return staging if staging is not None else dev
    return dev


def resolve_bool(name: str, dev: bool, staging: bool | None = None, prod: bool | None = None) -> bool:
    return resolve_setting(name, dev="1" if dev else "0",
                           staging=("1" if staging else "0") if staging is not None else None,
                           prod=("1" if prod else "0") if prod is not None else None) == "1"


def strict_expectations() -> dict:
    """各档位下**期望**的治理开关值——`security_posture()` 与部署体检脚本共用一份口径。

    口径只有一处，才不会出现"体检脚本说合格、服务实际是松的"这种自欺。
    """
    env = platform_env()
    return {
        "PLATFORM_ENV": env,
        "SERVICE_AUTH_MODE": resolve_setting("SERVICE_AUTH_MODE", dev="off", staging="token", prod="token"),
        "TRUST_PROXY_AUTH": "1" if resolve_bool("TRUST_PROXY_AUTH", dev=False) else "0",
        "GATE_DECISION_VERIFY": resolve_setting("GATE_DECISION_VERIFY", dev="warn", staging="block", prod="block"),
        "ENG_GATES_MODE": resolve_setting("ENG_GATES_MODE", dev="warn", staging="warn", prod="block"),
        "ENG_GATES_REQUIRED": resolve_setting("ENG_GATES_REQUIRED", dev="0", staging="0", prod="1"),
        "CHECKPOINTER": resolve_setting("CHECKPOINTER", dev="memory", staging="postgres", prod="postgres"),
    }


def security_posture(service: str = "") -> dict:
    """当前安全姿态（暴露在 `/health`）：**宽松必须是看得见的**。

    一个团队不会因为日志里有一行 WARNING 就发现自己跑在无鉴权模式；
    但每个人都会看到 `/health` 里的 `compliant: false` 与横幅提示。
    """
    cfg = ServiceAuthConfig()
    expect = strict_expectations()
    warnings: list[str] = []
    if cfg.mode != "token":
        warnings.append(f"SERVICE_AUTH_MODE={cfg.mode}：所有写接口对能连上端口的人开放")
    if cfg.mode == "token" and not (cfg.token or cfg.static_token or cfg.trust_proxy):
        warnings.append("SERVICE_AUTH_MODE=token 但未配置任何凭据（已 fail-closed 拒绝请求）")
    if expect["GATE_DECISION_VERIFY"] != "block":
        warnings.append(f"GATE_DECISION_VERIFY={expect['GATE_DECISION_VERIFY']}：人审恢复不强制核验真实决策")
    if expect["ENG_GATES_MODE"] != "block":
        warnings.append(f"ENG_GATES_MODE={expect['ENG_GATES_MODE']}：工程闸门只记录不阻断")
    if not os.getenv("ALERT_WEBHOOK_SECRET") and not os.getenv("WEBHOOK_SECRET"):
        warnings.append("告警 webhook 未配置密钥：/alerts/prometheus 会拒绝（503）")
    if not os.getenv("IM_CALLBACK_SIGNING_KEY") and not os.getenv("FEISHU_VERIFY_TOKEN"):
        warnings.append("IM 回调未配置签名密钥：卡片按钮会失效（503）")
    must_be_configured = cfg.platform_env in ("staging", "prod")
    return {
        "service": service,
        "platform_env": cfg.platform_env,
        "compliant": not warnings,
        "warnings": warnings,
        "effective": {**expect, "SERVICE_AUTH_MIN": cfg.mode},
        "credentials_configured": bool(cfg.token or cfg.static_token or (cfg.trust_proxy and True)),
        "strict_required": must_be_configured,
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


def check_request(request: "Request", extra_exempt: tuple[str, ...] = (),
                  static_dir=None) -> tuple[bool, str]:
    """返回 (是否放行, 原因)。抽成纯函数，便于单测与在别处复用。"""
    cfg = ServiceAuthConfig(extra_exempt, static_dir)
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


def install_service_auth(app: "FastAPI", service: str, extra_exempt: tuple[str, ...] = (),
                         static_dir=None) -> None:
    """挂上统一鉴权中间件。**每个服务都必须在 create_app 里调用**。

    `extra_exempt` 用于"自带签名校验的入站 webhook"（GitLab `/webhooks/gitlab`、
    IM 卡片 `/callbacks/im`、Alertmanager `/alerts/prometheus`）。
    加豁免前先确认那条路径真的验了签名——否则豁免就是后门。

    `static_dir` 用于"纯静态控制台页面"：目录里每个文件按文件名精确豁免，
    因此**新增一个 js/css 不会让页面白屏**（硬编码文件名踩过这个坑）。

    档位（`PLATFORM_ENV`）：
    - `dev`（默认）：宽松，启动打 WARNING 说明"当前无服务间鉴权"；
    - `staging`/`prod`：默认 `token`，且**没有任何凭据时直接拒绝启动**——
      "跑着但看起来有鉴权"比"根本没起"危险得多（后者一定会被发现）。
    """
    cfg = ServiceAuthConfig(extra_exempt, static_dir)
    posture = security_posture(service)

    if cfg.mode == "token" and not (cfg.token or cfg.static_token or cfg.trust_proxy):
        message = (
            f"{service}: PLATFORM_ENV={cfg.platform_env} 要求服务间鉴权，但没有可用凭据。"
            "请配置 SERVICE_AUTH_TOKEN（服务间）或 AUTH_STATIC_TOKEN / TRUST_PROXY_AUTH=1（人类入口）。"
            "拒绝启动：带着'看起来有鉴权'的状态运行比直接失败更危险。"
        )
        logger.critical(message)
        raise RuntimeError(message)

    if cfg.mode != "token":
        logger.warning(
            "服务间鉴权未开启（SERVICE_AUTH_MODE=%s，PLATFORM_ENV=%s）：%s 的所有写接口对能连上端口的人开放。"
            "上线请设 PLATFORM_ENV=prod（自动启用 token 模式）。",
            cfg.mode, cfg.platform_env, service,
        )
    else:
        logger.info("服务间鉴权已开启（token 模式）：%s %s", service, cfg.describe())
    if posture["warnings"]:
        logger.warning("%s 治理姿态提示（%d 项）：%s", service, len(posture["warnings"]),
                       "；".join(posture["warnings"]))

    @app.middleware("http")
    async def _service_auth_guard(request: "Request", call_next):  # type: ignore[no-untyped-def]
        from fastapi.responses import JSONResponse

        allowed, reason = check_request(request, extra_exempt, static_dir)
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
