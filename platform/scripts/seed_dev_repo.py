#!/usr/bin/env python3
"""Phase 1 试点仓库初始化：向真实 GitLab 仓库写入基础项目（基线代码 + 既有测试）。

用法：
  export GITLAB_TOKEN=glpat-xxx
  export DEMO_PROJECT="sandbox6853335/prodesgin-demo"
  python3 scripts/seed_dev_repo.py
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile

BASE_FILES = {
    "pyproject.toml": '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
    "src/reports/__init__.py": '"""报表模块（Phase 1 试点基线）。"""\n',
    "src/reports/query.py": '''"""报表查询模块（试点基线）。"""
from datetime import date


def get_report_data(start_date: date, end_date: date, rows: list[dict]) -> list[dict]:
    """按日期范围过滤报表数据行。"""
    return [r for r in rows if start_date <= date.fromisoformat(r["日期"]) <= end_date]
''',
    "tests/test_query.py": '''from datetime import date

from reports.query import get_report_data


def test_filter():
    rows = [
        {"日期": "2024-01-01", "类别": "营收", "数值": 1},
        {"日期": "2024-02-01", "类别": "营收", "数值": 2},
    ]
    out = get_report_data(date(2024, 1, 1), date(2024, 1, 31), rows)
    assert len(out) == 1 and out[0]["日期"] == "2024-01-01"
''',
    "README.md": "# prodesgin-demo\n\nPhase 1 试点仓库：报表 CSV 导出功能（AI 编码 Agent 试点基线）。\n",
}


def main() -> int:
    url = os.getenv("GITLAB_URL", "https://gitlab.com")
    token = os.getenv("GITLAB_TOKEN", "")
    project = os.getenv("DEMO_PROJECT", "sandbox6853335/prodesgin-demo")
    if not token:
        print("GITLAB_TOKEN 未设置")
        return 1

    host = url.split("://")[1]
    clone_url = f"https://oauth2:{token}@{host}/{project}.git"
    repo = tempfile.mkdtemp(prefix="seed-")
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    subprocess.run(["git", "clone", "--depth=1", clone_url, repo], check=True, capture_output=True, text=True, env=env)

    if os.path.exists(os.path.join(repo, "src", "reports", "query.py")):
        print("仓库已有基线代码，跳过初始化")
        return 0

    for rel, content in BASE_FILES.items():
        path = os.path.join(repo, rel)
        os.makedirs(os.path.dirname(path) or repo, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=Platform Bot", "-c", "user.email=bot@platform.local", *args],
            cwd=repo, check=True, capture_output=True, text=True, env=env,
        )

    git("add", "-A")
    git("commit", "-m", "chore: Phase 1 试点基线（报表查询模块 + 既有测试）")
    git("push", "origin", "main")
    print(f"✅ 基线已推送到 {project}/main：src/reports/query.py + tests/test_query.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
