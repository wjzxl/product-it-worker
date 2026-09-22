"""统一认证（SSO / OIDC）——生产化 ①。

模式（`AUTH_MODE`）：
| 模式 | 说明 |
|---|---|
| `off`（默认） | 不校验（本地开发/内网可信环境） |
| `static` | 共享 Bearer Token（`AUTH_STATIC_TOKEN`），快速内网部署 |
| `oidc` | OIDC/JWT：校验 RS256 签名（JWKS）、iss/aud/exp，角色从 claim 映射 |

配置（`AUTH_MODE=oidc`）：
- `OIDC_ISSUER`（必填，如 https://sso.example.com/realms/main）
- `OIDC_AUDIENCE`（必填，客户端 ID）
- `OIDC_JWKS_URL`（可选，默认 `{issuer}/.well-known/jwks.json`）
- `OIDC_ROLE_CLAIM`（默认 `roles`；也支持 `realm_access.roles` 这类嵌套路径）
- `OIDC_REQUIRED_ROLE`（可选：要求具备某角色，如 `approver`）
- `AUTH_PROTECT_READS`（默认 `false`：只保护写操作；置 `true` 则读接口也需登录）

真实 IdP（Keycloak / 飞书 / 企业微信 / Okta）均按上述 OIDC 接口对接，代码无需改动。
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import httpx
import jwt
from fastapi import Depends, Header, HTTPException, status

logger = logging.getLogger(__name__)


@dataclass
class Principal:
    sub: str
    roles: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class AuthConfig:
    def __init__(self) -> None:
        self.mode = os.getenv("AUTH_MODE", "off").lower()
        self.static_token = os.getenv("AUTH_STATIC_TOKEN", "")
        self.issuer = os.getenv("OIDC_ISSUER", "").rstrip("/")
        self.audience = os.getenv("OIDC_AUDIENCE", "")
        self.jwks_url = os.getenv("OIDC_JWKS_URL", "") or (f"{self.issuer}/.well-known/jwks.json" if self.issuer else "")
        self.role_claim = os.getenv("OIDC_ROLE_CLAIM", "roles")
        self.required_role = os.getenv("OIDC_REQUIRED_ROLE", "")
        self.protect_reads = os.getenv("AUTH_PROTECT_READS", "false").lower() == "true"


_config = AuthConfig()
_jwks_client: jwt.PyJWKClient | None = None
_jwks_transport: httpx.BaseTransport | None = None  # 测试注入


def refresh_config() -> AuthConfig:
    """重新读取环境变量（测试与热更新用）。"""
    global _config, _jwks_client
    _config = AuthConfig()
    _jwks_client = None
    return _config


def set_jwks_transport(transport: httpx.BaseTransport | None) -> None:
    """测试/自定义网络：注入 JWKS 获取通道。"""
    global _jwks_transport, _jwks_client
    _jwks_transport = transport
    _jwks_client = None


def _client() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(_config.jwks_url, cache_keys=True, timeout=10)
        if _jwks_transport is not None:  # 测试注入
            _jwks_client._httpx_client = httpx.Client(transport=_jwks_transport)  # type: ignore[attr-defined]
    return _jwks_client


def _roles_from_claims(claims: dict) -> list[str]:
    """支持 'roles' 与嵌套路径（如 realm_access.roles）。"""
    node: object = claims
    for part in _config.role_claim.split("."):
        if isinstance(node, dict):
            node = node.get(part)
        else:
            return []
    if isinstance(node, list):
        return [str(r) for r in node]
    if isinstance(node, str):
        return [node]
    return []


def verify_bearer(token: str) -> Principal:
    """校验 Bearer Token（static / oidc）。失败抛 401/403。"""
    if _config.mode == "static":
        if _config.static_token and token == _config.static_token:
            return Principal(sub="static-token", roles=["admin"])
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "无效的静态访问令牌")

    if _config.mode != "oidc":
        return Principal(sub="anonymous", roles=["admin"])

    if not _config.jwks_url or not _config.audience:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "OIDC 配置不完整（issuer/audience）")
    try:
        signing_key = _client().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256", "ES256"],
            audience=_config.audience,
            issuer=_config.issuer or None,
            options={"require": ["exp", "sub"]},
        )
    except jwt.ExpiredSignatureError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌已过期")
    except jwt.InvalidAudienceError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌受众不匹配")
    except jwt.InvalidIssuerError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "令牌签发方不匹配")
    except jwt.PyJWTError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, f"令牌校验失败: {type(exc).__name__}")

    roles = _roles_from_claims(claims)
    if _config.required_role and _config.required_role not in roles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, f"缺少角色 {_config.required_role}")
    return Principal(sub=str(claims.get("sub", "")), roles=roles, raw=claims)


def current_principal(authorization: str = Header(default="")) -> Principal:
    """FastAPI 依赖：从 Authorization: Bearer <token> 解析主体。"""
    if _config.mode == "off":
        return Principal(sub="anonymous", roles=["admin"])
    token = authorization[7:] if authorization.lower().startswith("bearer ") else ""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "缺少 Bearer 令牌")
    return verify_bearer(token)


def approver_roles() -> list[str]:
    """有资格做/触发人审决策的角色（`APPROVER_ROLES`，默认 approver,admin）。"""
    raw = os.getenv("APPROVER_ROLES", "approver,admin")
    return [r.strip() for r in raw.split(",") if r.strip()]


def require_role(*roles: str):
    """角色校验依赖（鉴权专项）。

    修复的问题：`AUTH_MODE=oidc` 只校验"登录了没有"，不校验"是谁"——
    任何登录用户（含只读账号）都能批准审批、触发全局超时扫描。
    `AUTH_MODE=off`（本机开发）与 `static`（共享令牌，主体即 admin）不受影响。
    """
    wanted = list(roles)

    def _dep(principal: Principal = Depends(current_principal)) -> Principal:
        if _config.mode == "off":
            return principal
        if wanted and not (set(wanted) & set(principal.roles)):
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                f"该操作需要角色之一 {wanted}，当前主体 {principal.sub!r} 的角色为 {principal.roles or '（空）'}",
            )
        return principal

    return _dep


def auth_summary() -> dict:
    return {
        "mode": _config.mode,
        "issuer": _config.issuer or None,
        "audience": _config.audience or None,
        "role_claim": _config.role_claim,
        "required_role": _config.required_role or None,
        "protect_reads": _config.protect_reads,
        "approver_roles": approver_roles(),
    }
