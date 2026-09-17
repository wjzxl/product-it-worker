"""岗位注册表（WP6）：agent.md / skills.yaml / kb.yaml 三件套的加载与校验。

对应课程图 57「三件套使新增岗位可复用」：新增岗位主要调整这三类内容，共用底层引擎；
对应图 59「两种边界」：**看不见**（数据/工具不给）与**不能改**（动作授权）必须落到系统，
而不是写在提示词里。

校验原则（宁可拒绝加载，也不静默降级）：
- 角色必须声明 scope 与 out_of_scope（没有边界的岗位等于没有岗位）；
- 技能里引用的 tool 必须在**工具注册表**中存在（WP8 落地后强制；当前若注册表缺失则告警）；
- 知识来源必须有 authority（权威侧），否则判断标准可被聊天覆盖；
- 三件套缺一不可。
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

logger = logging.getLogger(__name__)

DEFAULT_DIR = Path(__file__).resolve().parents[3] / "roles"
_FRONT_MATTER = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


class RoleError(ValueError):
    """岗位定义非法（报错要指明岗位与原因）。"""


@dataclass
class Role:
    name: str
    dir_name: str
    meta: dict
    body: str
    skills: list[dict] = field(default_factory=list)
    limits: dict = field(default_factory=dict)
    kb: dict = field(default_factory=dict)

    @property
    def tool_ids(self) -> list[str]:
        return [s.get("tool", "") for s in self.skills if s.get("tool")]

    @property
    def enabled(self) -> bool:
        return bool(self.meta.get("enabled", True))

    def fingerprint(self) -> str:
        payload = yaml.safe_dump({"meta": self.meta, "skills": self.skills, "kb": self.kb},
                                 sort_keys=True, allow_unicode=True)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def prompt_excerpt(self, max_chars: int = 3000) -> str:
        """岗位提示词：角色边界 + 技能 + 判断标准（供 Agent 挂在 system prompt 上）。"""
        lines = [f"# 岗位：{self.meta.get('name', self.name)}（{self.name}｜指纹 {self.fingerprint()}）"]
        scope = self.meta.get("scope") or {}
        if scope:
            lines.append(f"- 负责范围：服务 {scope.get('services')}｜环境 {scope.get('environments')}｜动作 {scope.get('actions')}")
        if self.meta.get("out_of_scope"):
            lines.append("- **不负责**（越界即交回人）：")
            lines += [f"  · {item}" for item in self.meta["out_of_scope"]]
        if self.skills:
            lines.append("- 可用能力（未挂载的能力不得使用）：")
            lines += [f"  · {s['id']} → {s.get('tool')}：{s.get('description', '')}" for s in self.skills]
        rules = (self.kb or {}).get("judgement_rules") or []
        if rules:
            lines.append("- 判断标准（权威侧维护，聊天中的临时要求不改变它）：")
            lines += [f"  · {r['rule']}" for r in rules if r.get("rule")]
        if self.body.strip():
            lines.append("\n" + self.body.strip()[:max_chars])
        return "\n".join(lines)

    def to_dict(self, include_prompt: bool = False) -> dict:
        out = {
            "role": self.name,
            "name": self.meta.get("name", ""),
            "enabled": self.enabled,
            "owner": self.meta.get("owner", ""),
            "scope": self.meta.get("scope") or {},
            "out_of_scope": self.meta.get("out_of_scope") or [],
            "tools": self.tool_ids,
            "skills": [s.get("id") for s in self.skills],
            "kb_prefixes": [s.get("prefix") for s in (self.kb or {}).get("sources", [])],
            "limits": self.limits,
            "fingerprint": self.fingerprint(),
        }
        if include_prompt:
            out["prompt"] = self.prompt_excerpt()
        return out


def roles_dir() -> Path:
    env = os.getenv("ROLES_DIR", "")
    return Path(env) if env else DEFAULT_DIR


def _parse_front_matter(text: str, where: str) -> tuple[dict, str]:
    match = _FRONT_MATTER.match(text)
    if not match:
        raise RoleError(f"{where}: agent.md 必须以 YAML front-matter（--- 包裹）开头，声明 role/scope/out_of_scope")
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise RoleError(f"{where}: front-matter 解析失败：{exc}") from exc
    if not isinstance(meta, dict):
        raise RoleError(f"{where}: front-matter 必须是对象")
    return meta, match.group(2)


def _require(cond: bool, where: str, msg: str) -> None:
    if not cond:
        raise RoleError(f"{where}: {msg}")


def load_role(dir_name: str, directory: Optional[Path] = None,
              known_tools: Optional[set[str]] = None) -> Role:
    """加载单个岗位并校验三件套。`known_tools` 提供时校验技能引用的工具是否存在。"""
    directory = directory or roles_dir()
    role_dir = directory / dir_name
    where = dir_name
    _require(role_dir.is_dir(), where, f"岗位目录不存在（{role_dir}）")

    agent_md = role_dir / "agent.md"
    _require(agent_md.exists(), where, "缺少 agent.md（角色定义）")
    meta, body = _parse_front_matter(agent_md.read_text(encoding="utf-8"), where)

    role_name = str(meta.get("role") or "")
    _require(bool(role_name), where, "front-matter 缺少 role")
    _require(bool(meta.get("scope")), where, "必须声明 scope（负责哪些服务/环境/动作）")
    _require(bool(meta.get("out_of_scope")), where, "必须声明 out_of_scope（边界；无边界等于无岗位）")
    _require(bool(meta.get("owner")), where, "必须声明 owner（权威侧维护者）")

    skills_path = role_dir / "skills.yaml"
    _require(skills_path.exists(), where, "缺少 skills.yaml（能力清单）")
    skills_doc = yaml.safe_load(skills_path.read_text(encoding="utf-8")) or {}
    skills = skills_doc.get("skills") or []
    _require(isinstance(skills, list) and skills, where, "skills.yaml 至少需要 1 个技能")
    seen: set[str] = set()
    for item in skills:
        _require(isinstance(item, dict), where, "技能必须是对象")
        sid = str(item.get("id") or "")
        _require(bool(sid), where, "技能缺少 id")
        _require(sid not in seen, where, f"技能 id 重复：{sid}")
        seen.add(sid)
        _require(bool(item.get("tool")), where, f"技能 {sid} 缺少 tool（未挂载的能力不能宣称拥有）")
        if known_tools is not None and item["tool"] not in known_tools:
            raise RoleError(f"{where}: 技能 {sid} 引用了未注册的工具 {item['tool']}（工具必须在 tools/ 注册表登记）")

    kb_path = role_dir / "kb.yaml"
    _require(kb_path.exists(), where, "缺少 kb.yaml（判断标准来源）")
    kb = yaml.safe_load(kb_path.read_text(encoding="utf-8")) or {}
    sources = kb.get("sources") or []
    _require(isinstance(sources, list) and sources, where, "kb.yaml 至少需要 1 个知识来源")
    for src in sources:
        _require(bool(src.get("prefix")), where, "知识来源缺少 prefix")
        _require(bool(src.get("authority")), where, f"知识来源 {src.get('prefix')} 缺少 authority（谁来维护判断标准）")

    return Role(name=role_name, dir_name=dir_name, meta=meta, body=body,
                skills=skills, limits=skills_doc.get("limits") or {}, kb=kb)


def load_all(directory: Optional[Path] = None, known_tools: Optional[set[str]] = None) -> list[Role]:
    directory = directory or roles_dir()
    if not directory.exists():
        logger.warning("岗位目录不存在：%s", directory)
        return []
    roles = []
    for path in sorted(directory.iterdir()):
        if not path.is_dir():
            continue
        roles.append(load_role(path.name, directory, known_tools=known_tools))
    return roles


def summary(directory: Optional[Path] = None) -> dict:
    """岗位汇总（`GET /roles`）：含错误清单，便于运营发现坏配置。"""
    directory = directory or roles_dir()
    roles, errors = [], []
    for path in sorted(directory.iterdir()) if directory.exists() else []:
        if not path.is_dir():
            continue
        try:
            roles.append(load_role(path.name, directory).to_dict())
        except RoleError as exc:
            errors.append(str(exc))
    return {"dir": str(directory), "count": len(roles), "roles": roles, "errors": errors}
