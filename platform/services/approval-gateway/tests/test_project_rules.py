"""WP4 项目规则包校验器测试（含反例：能拦住"悄悄删规则"与"多写不读"）。"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))

from check_project_rules import RulePackError, load_manifest, validate  # noqa: E402

REAL_PACK = ROOT / "project-rules"


@pytest.fixture()
def pack(tmp_path) -> Path:
    """把真实规则包复制到临时目录，便于制造缺陷。"""
    dst = tmp_path / "project-rules"
    shutil.copytree(REAL_PACK, dst)
    return dst


class TestRealPack:
    def test_repo_pack_is_valid(self):
        result = validate(REAL_PACK)
        assert result["ok"] is True, result["errors"]
        assert result["stats"]["layers"] == {"constraints": 3, "design": 2, "completeness": 2}
        assert result["stats"]["templates"] == 5

    def test_missing_manifest_raises(self, tmp_path):
        with pytest.raises(RulePackError, match="缺少 manifest.yaml"):
            load_manifest(tmp_path)


class TestCatchRegressions:
    def test_deleted_template_detected(self, pack):
        (pack / "templates" / "frontend.md").unlink()
        result = validate(pack)
        assert result["ok"] is False
        assert any("frontend" in e for e in result["errors"])

    def test_undeclared_extra_rule_file_detected(self, pack):
        """写了规则却没人读 = 假合规，必须报错。"""
        (pack / "constraints" / "brand-new-rule.md").write_text("# 新规则\n## 小节\n内容\n", encoding="utf-8")
        result = validate(pack)
        assert result["ok"] is False
        assert any("未在 manifest 登记" in e for e in result["errors"])

    def test_missing_required_section_detected(self, pack):
        (pack / "constraints" / "api-protocol.md").write_text("# 约束：统一接口协议\n\n（内容被删空）\n", encoding="utf-8")
        result = validate(pack)
        assert result["ok"] is False
        assert any("缺少必备小节" in e and "api-protocol.md" in e for e in result["errors"])

    def test_declared_file_missing_detected(self, pack):
        (pack / "completeness" / "ux-states.md").unlink()
        result = validate(pack)
        assert result["ok"] is False
        assert any("声明的文件不存在" in e for e in result["errors"])

    def test_missing_layer_detected(self, pack):
        manifest = yaml.safe_load((pack / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["layers"].pop("completeness")
        (pack / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
        result = validate(pack)
        assert result["ok"] is False
        assert any("缺少层：completeness" in e for e in result["errors"])

    def test_not_applicable_doc_removed_detected(self, pack):
        path = pack / "design" / "five-design-docs.md"
        text = path.read_text(encoding="utf-8").replace("not_applicable", "N/A")
        path.write_text(text, encoding="utf-8")
        result = validate(pack)
        assert result["ok"] is False
        assert any("not_applicable" in e for e in result["errors"])

    def test_wrong_facets_detected(self, pack):
        manifest = yaml.safe_load((pack / "manifest.yaml").read_text(encoding="utf-8"))
        manifest["templates"]["facets"] = ["data", "api"]
        (pack / "manifest.yaml").write_text(yaml.safe_dump(manifest, allow_unicode=True), encoding="utf-8")
        result = validate(pack)
        assert result["ok"] is False
        assert any("facets" in e for e in result["errors"])


class TestManifestShape:
    def test_five_facets_declared(self):
        manifest = load_manifest(REAL_PACK)
        assert set(manifest["templates"]["facets"]) == {"data", "business", "api", "frontend", "test"}

    def test_every_layer_file_has_required_sections(self):
        manifest = load_manifest(REAL_PACK)
        for layer, spec in manifest["layers"].items():
            for item in spec["files"]:
                assert item.get("required_sections"), f"{layer}/{item.get('path')} 未声明必备小节"

    def test_constraints_are_judgeable(self):
        """约束层必须写成可判定的要求，而不是"注意质量"这类期望。"""
        for path in (REAL_PACK / "constraints").glob("*.md"):
            text = path.read_text(encoding="utf-8")
            assert "禁止" in text or "必须" in text, f"{path.name} 缺少可判定的约束表述"
