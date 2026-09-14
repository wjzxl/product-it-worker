#!/usr/bin/env python3
"""SSO / OIDC 真实链路验证：本地 IdP（JWKS 服务）+ approval-gateway 走真实 JWT 校验。

用法（用 approval-gateway 的 venv 运行，需 cryptography/pyjwt/httpx）：
  services/approval-gateway/.venv/bin/python scripts/sso_check.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.join(ROOT, "services")
PROXY_PORT = 8012
IDP_PORT = 8020
TMP = tempfile.mkdtemp(prefix="sso-check-")
ISSUER = f"http://127.0.0.1:{IDP_PORT}"
AUDIENCE = "platform-web"

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_KEY = PRIVATE_KEY.public_key()
PUBLIC_NUMBERS = PUBLIC_KEY.public_numbers()


def _b64u(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).decode().rstrip("=")


JWKS = {
    "keys": [
        {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "test-key-1",
            "n": _b64u(PUBLIC_NUMBERS.n.to_bytes((PUBLIC_NUMBERS.n.bit_length() + 7) // 8, "big")),
            "e": _b64u(PUBLIC_NUMBERS.e.to_bytes((PUBLIC_NUMBERS.e.bit_length() + 7) // 8, "big")),
        }
    ]
}


class IdP(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path.startswith("/.well-known/jwks.json") or self.path.startswith("/jwks.json"):
            body = json.dumps(JWKS).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


def token(roles=("approver",), minutes=10, aud=AUDIENCE):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": "alice@corp",
            "aud": aud,
            "iss": ISSUER,
            "exp": now + timedelta(minutes=minutes),
            "iat": now,
            "roles": list(roles),
        },
        PRIVATE_KEY,
        algorithm="RS256",
        headers={"kid": "test-key-1"},
    )


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", IDP_PORT), IdP)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"① 本地 IdP 启动：{ISSUER}/jwks.json（真实 JWKS，RSA-2048）")

    env = dict(os.environ)
    env.update({
        "AUTH_MODE": "oidc",
        "OIDC_ISSUER": ISSUER,
        "OIDC_AUDIENCE": AUDIENCE,
        "OIDC_JWKS_URL": f"{ISSUER}/jwks.json",
        "OIDC_REQUIRED_ROLE": "approver",
        "DATABASE_URL": f"sqlite:///{TMP}/approval.db",
    })
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(PROXY_PORT)],
        cwd=os.path.join(SERVICES, "approval-gateway"), env=env,
        stdout=open(f"{TMP}/gateway.log", "w"), stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                if httpx.get(f"http://127.0.0.1:{PROXY_PORT}/health", timeout=2).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            print("❌ 网关启动失败")
            return 1
        print(f"② approval-gateway 启动（AUTH_MODE=oidc，要求角色 approver）")

        base = f"http://127.0.0.1:{PROXY_PORT}"
        deadline_dt = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        apv = httpx.post(f"{base}/approvals", json={
            "type": "GATE_REQUIREMENT_REVIEW", "stage": "产品域 G1", "requester": "SSO 验证",
            "task_ref": "sso-check",
            "context_snapshot": {"artifact_ref": "PRD-SSO", "diff_summary": "SSO 链路验证",
                                 "evidence": ["e1"], "risk": {"score": 10, "level": "L2", "factors": []}},
            "options": ["approve", "reject", "revise"], "deadline": deadline_dt,
            "escalation_policy": {"remind_after_hours": 2, "escalate_to": "lead", "timeout_action": "reject"},
        }, timeout=15).json()
        print(f"③ 创建审批单 {apv['id']}（创建接口不要求登录，供工作流调用）")

        r = httpx.post(f"{base}/approvals/{apv['id']}/decision",
                       json={"decision": "approve", "decided_by": "", "comment": "无令牌"}, timeout=15)
        print(f"④ 无令牌决策 → HTTP {r.status_code}（期望 401）：{r.json().get('detail')}")

        r = httpx.post(f"{base}/approvals/{apv['id']}/decision",
                       headers={"Authorization": "Bearer " + token(roles=["viewer"])},
                       json={"decision": "approve", "decided_by": "", "comment": "角色不足"}, timeout=15)
        print(f"⑤ 角色不足（viewer）→ HTTP {r.status_code}（期望 403）：{r.json().get('detail')}")

        r = httpx.post(f"{base}/approvals/{apv['id']}/decision",
                       headers={"Authorization": "Bearer " + token(roles=["approver"])},
                       json={"decision": "approve", "decided_by": "alice@corp", "comment": "SSO 批准"}, timeout=15)
        print(f"⑥ 有效令牌（approver）→ HTTP {r.status_code}（期望 200）")

        info = httpx.get(f"{base}/auth/info", headers={"Authorization": "Bearer " + token()}, timeout=15).json()
        print(f"⑦ /auth/info → mode={info['mode']} issuer={info['issuer']} principal={info['principal']}")
        detail = httpx.get(f"{base}/approvals/{apv['id']}", timeout=15).json()
        print(f"⑧ 审计留痕：decided_by={detail['decisions'][-1]['decided_by']} status={detail['status']}")
        ok = r.status_code == 200 and detail["decisions"][-1]["decided_by"] == "alice@corp"
        print(f"\nSSO 链路验证：{'✅ 通过' if ok else '❌ 失败'}")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        server.shutdown()


if __name__ == "__main__":
    sys.exit(main())
