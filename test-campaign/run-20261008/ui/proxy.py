#!/usr/bin/env python3
"""入口代理夹具：模拟 Caddy + SSO 的生产拓扑（战役专用，不属于被测系统）。

它做两件生产入口必须做的事，因此"驾驶舱在代理后可用"这条路径能被真的跑一次：

1. **剥离**客户端自带的 `X-Platform-User` / `X-Service-Token`（`header_up -X-...`），
   否则任何人都能伪造身份直连服务；
2. **注入** `X-Platform-User: <已登录用户>`，模拟 SSO 网关在登录成功后写入的可信头。

用法：
    python3 ui/proxy.py --listen 18015 --target http://127.0.0.1:18005 --user campaign-ui
"""
from __future__ import annotations

import argparse
import os
import http.server
import urllib.error
import urllib.request

STRIP = {"x-platform-user", "x-service-token", "x-proxy-token"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length"}


class Proxy(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _forward(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None

        headers = {k: v for k, v in self.headers.items()
                   if k.lower() not in HOP_BY_HOP and k.lower() not in STRIP}
        # SSO 注入身份 + 内部代理密钥（新契约：只有同时持有独立代理密钥的入口才被信任）
        headers["X-Platform-User"] = self.server.user
        if self.server.proxy_token:
            headers["X-Proxy-Token"] = self.server.proxy_token

        req = urllib.request.Request(f"{self.server.target}{self.path}", data=body,
                                     method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                status, resp_headers = resp.status, dict(resp.headers)
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            status, resp_headers = exc.code, dict(exc.headers)
        except Exception as exc:  # noqa: BLE001
            payload = f'{{"detail":"proxy error: {type(exc).__name__}: {exc}"}}'.encode()
            status, resp_headers = 502, {"Content-Type": "application/json"}

        self.send_response(status)
        for k, v in resp_headers.items():
            if k.lower() in HOP_BY_HOP:
                continue
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = lambda self: self._forward("GET")        # noqa: E731
    do_POST = lambda self: self._forward("POST")      # noqa: E731
    do_PUT = lambda self: self._forward("PUT")        # noqa: E731
    do_DELETE = lambda self: self._forward("DELETE")  # noqa: E731

    def log_message(self, *args):  # noqa: D102
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--listen", type=int, default=18015)
    ap.add_argument("--target", default="http://127.0.0.1:18005")
    ap.add_argument("--user", default="campaign-ui")
    ap.add_argument("--proxy-token", default=os.getenv("PROXY_AUTH_TOKEN", ""))
    args = ap.parse_args()

    server = http.server.ThreadingHTTPServer(("127.0.0.1", args.listen), Proxy)
    server.target = args.target.rstrip("/")
    server.user = args.user
    server.proxy_token = args.proxy_token
    print(f"入口代理：http://127.0.0.1:{args.listen} → {server.target}"
          f"（注入 X-Platform-User: {args.user} + X-Proxy-Token，剥离客户端同名声明的身份头）")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
