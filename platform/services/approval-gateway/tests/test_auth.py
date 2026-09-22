"""SSO / OIDC 认证测试（真实 RSA 签名与 JWKS 校验路径）。

不依赖外部 IdP：本地生成 RSA 密钥对，签名 JWT；JWKS 获取以桩替代（PyJWT 负责解析）。
真实 IdP（Keycloak/飞书/Okta）按同一 OIDC 接口对接。
"""
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import auth as auth_mod
from app.auth import current_principal, verify_bearer, refresh_config
from app.models import utcnow

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()
PUBLIC_PEM = PUBLIC_KEY.public_bytes(
    encoding=serialization.Encoding.PEM, format=serialization.PublicFormat.SubjectPublicKeyInfo
)


class _StubJWKClient:
    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": PUBLIC_PEM})()


@pytest.fixture(autouse=True)
def _oidc(monkeypatch):
    monkeypatch.setattr(auth_mod, "_client", lambda: _StubJWKClient())
    monkeypatch.setenv("AUTH_MODE", "oidc")
    monkeypatch.setenv("OIDC_ISSUER", "https://sso.example.com/realms/main")
    monkeypatch.setenv("OIDC_AUDIENCE", "platform-web")
    monkeypatch.setenv("OIDC_JWKS_URL", "https://sso.example.com/realms/main/.well-known/jwks.json")
    monkeypatch.delenv("OIDC_REQUIRED_ROLE", raising=False)
    refresh_config()
    yield
    monkeypatch.setenv("AUTH_MODE", "off")
    refresh_config()


def _token(roles=("approver",), aud="platform-web", iss="https://sso.example.com/realms/main",
           exp_minutes=10, key=PRIVATE_KEY, role_claim="roles"):
    now = datetime.now(timezone.utc)
    payload = {
        "sub": "user-123",
        "aud": aud,
        "iss": iss,
        "exp": now + timedelta(minutes=exp_minutes),
        "iat": now,
        role_claim: list(roles),
    }
    return jwt.encode(payload, key, algorithm="RS256")


class TestVerifyBearer:
    def test_valid_token(self):
        p = verify_bearer(_token())
        assert p.sub == "user-123" and p.roles == ["approver"]

    def test_expired_token(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            verify_bearer(_token(exp_minutes=-5))
        assert e.value.status_code == 401 and "过期" in e.value.detail

    def test_wrong_audience(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            verify_bearer(_token(aud="other-client"))
        assert e.value.status_code == 401 and "受众" in e.value.detail

    def test_wrong_issuer(self):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as e:
            verify_bearer(_token(iss="https://evil.example.com"))
        assert e.value.status_code == 401

    def test_wrong_signature(self):
        from fastapi import HTTPException

        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        with pytest.raises(HTTPException) as e:
            verify_bearer(_token(key=other))
        assert e.value.status_code == 401 and "校验失败" in e.value.detail

    def test_required_role_enforced(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setenv("OIDC_REQUIRED_ROLE", "approver")
        refresh_config()
        assert verify_bearer(_token()).roles == ["approver"]
        with pytest.raises(HTTPException) as e:
            verify_bearer(_token(roles=["viewer"]))
        assert e.value.status_code == 403

    def test_nested_role_claim(self, monkeypatch):
        monkeypatch.setenv("OIDC_ROLE_CLAIM", "realm_access.roles")
        refresh_config()
        token = _token(role_claim="realm_access")
        # 手工构造嵌套 claim
        import jwt as _jwt

        payload = _jwt.decode(token, options={"verify_signature": False})
        payload.pop("realm_access")
        payload["realm_access"] = {"roles": ["approver", "admin"]}
        token = _jwt.encode(payload, PRIVATE_KEY, algorithm="RS256")
        assert verify_bearer(token).roles == ["approver", "admin"]


class TestStaticMode:
    def test_static_token(self, monkeypatch):
        from fastapi import HTTPException

        monkeypatch.setenv("AUTH_MODE", "static")
        monkeypatch.setenv("AUTH_STATIC_TOKEN", "tok-abc")
        refresh_config()
        assert verify_bearer("tok-abc").roles == ["admin"]
        with pytest.raises(HTTPException):
            verify_bearer("wrong")

    def test_off_mode_anonymous(self, monkeypatch):
        monkeypatch.setenv("AUTH_MODE", "off")
        refresh_config()
        assert verify_bearer("anything").sub == "anonymous"


class TestApiWithAuth:
    def test_decision_requires_token(self, client):
        """OIDC 模式：无令牌 → 401；有效令牌 → 200，且**审计署名取自登录主体**。

        鉴权专项后语义更严：身份真实时不再采信请求体里填的姓名
        （否则登录用户可以冒名顶替写审计），`decided_by` 恒为 token 的 `sub`。
        """
        apv = client.post("/approvals", json=_payload()).json()
        r = client.post(f"/approvals/{apv['id']}/decision",
                        json={"decision": "approve", "decided_by": "", "comment": "SSO 登录后批准"})
        assert r.status_code == 401

        r = client.post(f"/approvals/{apv['id']}/decision",
                        headers={"Authorization": f"Bearer {_token()}"},
                        json={"decision": "approve", "decided_by": "reviewer-from-sso", "comment": "SSO"})
        assert r.status_code == 200
        detail = client.get(f"/approvals/{apv['id']}").json()
        assert detail["decisions"][-1]["decided_by"] == "user-123", "署名必须来自登录主体，而非客户端自述"

    def test_auth_info(self, client):
        info = client.get("/auth/info", headers={"Authorization": f"Bearer {_token()}"}).json()
        assert info["mode"] == "oidc" and info["principal"]["sub"] == "user-123"
        assert info["principal"]["roles"] == ["approver"]


@pytest.fixture(scope="module")
def client():
    from test_api import _payload as payload_factory  # noqa: F401

    from app.main import app

    with TestClient(app) as c:
        yield c


def _payload():
    import test_api

    return test_api._payload()
