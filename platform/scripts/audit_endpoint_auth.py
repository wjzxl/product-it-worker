#!/usr/bin/env python3
"""端点鉴权清单：把每个服务的 HTTP 写接口与其鉴权依赖列出来（鉴权专项的体检表）。

为什么需要它：平台是"AI 执行 + 人在闸门决策"，**写接口就是权力**——
`POST /console/decide` 落一次人审决策、`POST /apply` 改 SOP 配置、`POST /tools/invoke` 调工具。
这些接口如果没有鉴权，任何能连到端口的人都能替人做决策、改流程。

做法：纯 AST 解析（不 import 各服务，避免每个服务各自的 venv 与启动副作用）。
判定"有鉴权"= 路由函数签名里出现以下之一：
  - `Depends(current_principal)` / `Depends(require_*)` 等来自 `*auth*` 模块的依赖；
  - `Depends(service_token_guard)` / 任何名字含 `token`/`guard`/`require` 的依赖；
  - `Security(...)`；
  - 该 router/FastAPI 在构造时通过 `dependencies=[...]` 全局挂了鉴权。

用法：
    python3 platform/scripts/audit_endpoint_auth.py              # 打印清单
    python3 platform/scripts/audit_endpoint_auth.py --strict     # 存在无鉴权写接口则退出码 1
    python3 platform/scripts/audit_endpoint_auth.py --json       # 机器可读
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

PLATFORM = Path(__file__).resolve().parent.parent
SERVICES = PLATFORM / "services"
WRITE_METHODS = {"post", "put", "patch", "delete"}


def _dep_name(call: ast.Call) -> str:
    """`Depends(x)` / `Security(x)` → 'x' 的源码形式。"""
    if not call.args:
        return ""
    return ast.unparse(call.args[0]) if hasattr(ast, "unparse") else ""


def _is_auth_dep(src: str, auth_names: set[str]) -> bool:
    if not src:
        return False
    base = src.split("(")[0].split(".")[-1]
    if base in auth_names:
        return True
    low = src.lower()
    return any(k in low for k in ("principal", "current_user", "require_", "_guard", "service_token",
                                 "verify_", "auth"))


def _module_facts(path: Path, service: str) -> dict:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # 从 *auth* 模块导入的名字，或本地定义的 require_*/current_* 可调用对象
    auth_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = (node.module or "").lower()
            for alias in node.names:
                name = alias.asname or alias.name
                if "auth" in mod or "auth" in alias.name.lower():
                    auth_names.add(name)
                if alias.name.startswith(("require_", "current_")):
                    auth_names.add(name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith(("require_", "current_", "verify_", "guard_")):
                auth_names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id.lower().endswith(("guard", "auth_dep", "dep")):
                    auth_names.add(tgt.id)

    # router/app 构造时的全局 dependencies
    router_deps: dict[str, bool] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            fname = ast.unparse(node.value.func) if hasattr(ast, "unparse") else ""
            if fname.split(".")[-1] in ("APIRouter", "FastAPI"):
                guarded = False
                for kw in node.value.keywords:
                    if kw.arg == "dependencies" and isinstance(kw.value, (ast.List, ast.Tuple)):
                        guarded = any(
                            _is_auth_dep(_dep_name(e), auth_names)
                            for e in kw.value.elts if isinstance(e, ast.Call)
                        )
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        router_deps[tgt.id] = router_deps.get(tgt.id, False) or guarded

    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not isinstance(dec, ast.Call):
                continue
            fn = ast.unparse(dec.func) if hasattr(ast, "unparse") else ""
            parts = fn.split(".")
            if len(parts) != 2 or parts[1] not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            router_name, method = parts
            path_arg = dec.args[0].value if dec.args and isinstance(dec.args[0], ast.Constant) else "?"
            per_route = False
            for d in list(node.args.defaults) + [k for k in node.args.kw_defaults if k is not None]:
                if isinstance(d, ast.Call) and ast.unparse(d.func).split(".")[-1] in ("Depends", "Security"):
                    if _is_auth_dep(_dep_name(d), auth_names):
                        per_route = True
            routes.append({
                "service": service,
                "file": path.name,
                "method": method.upper(),
                "path": path_arg,
                "handler": node.name,
                "auth": per_route or router_deps.get(router_name, False),
                "auth_kind": ("principal" if per_route else ("router-level" if router_deps.get(router_name) else "none")),
            })
    return {"auth_names": sorted(auth_names), "routes": routes}


def _middleware_facts(svc_dir: Path) -> dict:
    """服务是否挂了统一服务间鉴权中间件，以及它显式豁免了哪些路径。

    中间件是"默认拒绝 + 显式豁免"，所以在它生效的服务里，**没有逐路由依赖不等于没鉴权**；
    但豁免清单必须被看见——豁免一个不验签的路径就等于开后门。
    """
    main_py = svc_dir / "app" / "main.py"
    if not main_py.exists():
        return {"installed": False, "exempt": []}
    tree = ast.parse(main_py.read_text(encoding="utf-8"), filename=str(main_py))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and ast.unparse(node.func).split(".")[-1] == "install_service_auth":
            exempt: list[str] = []
            for kw in node.keywords:
                if kw.arg == "extra_exempt" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    exempt = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
            return {"installed": True, "exempt": exempt}
    return {"installed": False, "exempt": []}


def scan(ignore_middleware: bool = False) -> dict:
    """`ignore_middleware=True` 只看逐路由依赖——用于对照"中间件到底挡了多少"。"""
    services = {}
    for svc_dir in sorted(p for p in SERVICES.iterdir() if p.is_dir()):
        app_dir = svc_dir / "app"
        if not app_dir.is_dir():
            continue
        routes, names = [], set()
        for py in sorted(app_dir.glob("*.py")):
            facts = _module_facts(py, svc_dir.name)
            routes.extend(facts["routes"])
            names.update(facts["auth_names"])
        mw = _middleware_facts(svc_dir)
        for r in routes:                      # 中间件覆盖 = 该服务所有非豁免路径都要求令牌
            if mw["installed"] and not ignore_middleware and not r["auth"]:
                r["auth"] = True
                r["auth_kind"] = "middleware"
        services[svc_dir.name] = {"auth_names": sorted(names), "routes": routes,
                                  "service_auth_middleware": mw["installed"], "exempt": mw["exempt"]}
    return services


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--strict", action="store_true", help="存在无鉴权写接口时退出码 1")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ignore-middleware", action="store_true",
                    help="忽略统一鉴权中间件，只看逐路由依赖（对照用）")
    args = ap.parse_args()

    services = scan(ignore_middleware=args.ignore_middleware)
    if args.json:
        print(json.dumps(services, ensure_ascii=False, indent=2))
        return 0

    total_write = total_open = 0
    print("服务                写接口  无鉴权写接口  读接口  覆盖方式")
    print("-" * 84)
    for name, facts in services.items():
        writes = [r for r in facts["routes"] if r["method"].lower() in WRITE_METHODS]
        reads = [r for r in facts["routes"] if r["method"].lower() not in WRITE_METHODS]
        open_writes = [r for r in writes if not r["auth"]]
        total_write += len(writes)
        total_open += len(open_writes)
        kinds = sorted({r["auth_kind"] for r in facts["routes"] if r["auth"]})
        cover = ("中间件(默认拒绝)" if facts["service_auth_middleware"] else "") or ", ".join(kinds) or "—"
        if facts["service_auth_middleware"] and kinds:
            cover = "中间件 + " + ", ".join(k for k in kinds if k != "middleware")
        print(f"{name:19s} {len(writes):5d} {len(open_writes):12d} {len(reads):7d}  {cover[:40]}")

    print("\n豁免清单（这些路径不走服务令牌，必须是「自带签名校验」的入站入口）：")
    for name, facts in services.items():
        if facts["exempt"]:
            print(f"  {name}: {', '.join(facts['exempt'])}")
    default_exempt = "/health, /docs, /openapi.json, /redoc, /favicon.ico（模块默认，所有服务）"
    print(f"  默认: {default_exempt}")

    print("\n无鉴权的写接口明细：")
    for name, facts in services.items():
        for r in facts["routes"]:
            if r["method"].lower() in WRITE_METHODS and not r["auth"]:
                print(f"  {r['method']:6s} /{'' if r['path'].startswith('/') else ''}{r['path']:<28s} "
                      f"{name}/{r['file']}::{r['handler']}")

    print(f"\n合计：写接口 {total_write} 个，其中无鉴权 {total_open} 个。")
    if args.strict and total_open:
        print("FAIL: 存在无鉴权写接口（`--strict`）")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
