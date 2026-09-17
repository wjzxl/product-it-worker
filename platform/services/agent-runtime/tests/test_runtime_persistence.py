"""WP7 持久化运行时测试：checkpointer 灰度、SOP 命名空间隔离、等待状态、运行归档。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from app import checkpointer, runs
from app.apis import MockApprovalAPI, MockGitLabAPI
from app.llm import MockLLM
from app.main import _config, create_app


class TestCheckpointer:
    def test_memory_mode_is_honest_about_restart(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER", "memory")
        info = checkpointer.describe()
        assert info["configured"] == "memory" and info["restart_safe"] is False
        assert "重启即丢" in info["note"]

    def test_postgres_mode_marks_restart_safe(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER", "postgres")
        monkeypatch.setenv("CHECKPOINTER_DSN", "postgresql://u:p@127.0.0.1:5432/db")
        info = checkpointer.describe()
        assert info["configured"] == "postgres" and info["restart_safe"] is True
        assert "5432/db" in info["dsn"] and "u:p" not in info["dsn"]   # 不泄露口令

    def test_unknown_mode_falls_back_to_memory(self, monkeypatch):
        monkeypatch.setenv("CHECKPOINTER", "随便写的")
        assert getattr(checkpointer.build(), "kind") == "memory"

    def test_postgres_build_falls_back_when_unreachable(self, monkeypatch):
        """PG 不可达时回退 memory 并告警，而不是让服务起不来。"""
        monkeypatch.setenv("CHECKPOINTER", "postgres")
        monkeypatch.setenv("CHECKPOINTER_DSN", "postgresql://x:y@127.0.0.1:59999/none")
        saver = checkpointer.build()
        assert getattr(saver, "kind") == "memory"

    def test_dsn_suffix_normalized(self):
        assert checkpointer._normalize("postgresql+psycopg://a@b/c") == "postgresql://a@b/c"


class TestSopNamespace:
    def test_same_thread_id_does_not_collide_across_sops(self):
        """共用持久化 checkpointer 时，两个 SOP 用同一 thread_id 不能串状态。"""
        app = create_app(approval_api=MockApprovalAPI(), gitlab_api=MockGitLabAPI(),
                         llm=MockLLM(answers=["澄清", "PRD v1", "诊断", "复盘"]))
        graphs = app.state.graphs
        product, ops = graphs["product-domain/prd-flow"], graphs["ops-domain/alert-to-postmortem"]
        cfg_product = _config("shared-id", "product-domain/prd-flow")
        cfg_ops = _config("shared-id", "ops-domain/alert-to-postmortem")
        assert cfg_product["configurable"]["thread_id"] != cfg_ops["configurable"]["thread_id"]
        assert product.get_state(cfg_product).values == {}
        assert ops.get_state(cfg_ops).values == {}


class TestRunArchive:
    @pytest.fixture(autouse=True)
    def isolated_db(self, tmp_path, monkeypatch):
        runs.reset_engine(f"sqlite:///{tmp_path}/runs.db")
        yield
        runs._engine = None

    def test_start_finish_and_duration(self):
        runs.start_run("t1", sop="product-domain/prd-flow", trigger="schedule",
                       tenant="line-a", role="product-agent", checkpoint="postgres")
        done = runs.finish_run("t1", "done", {"llm_calls": 3, "prompt_tokens": 100,
                                              "completion_tokens": 20, "cost_usd": 0.0012,
                                              "cost_metered": True})
        assert done["status"] == "done" and done["cost_usd"] == 0.0012
        assert done["cost_metered"] is True and done["duration_s"] >= 0

    def test_missing_usage_is_not_claimed_as_metered(self):
        """没有用量信息时不得记成"已计量 $0"。"""
        runs.start_run("t2", sop="x")
        done = runs.finish_run("t2", "done", {})
        assert done["cost_metered"] is False and done["cost_usd"] == 0.0

    def test_waiting_records_reason_and_counts(self):
        runs.start_run("t3", sop="ops-domain/alert-to-postmortem", role="ops-duty")
        first = runs.mark_waiting("t3", "指标未到齐")
        second = runs.mark_waiting("t3", "仍未到齐")
        assert first["status"] == "waiting" and second["waits"] == 2
        assert second["waiting_reason"] == "仍未到齐"

    def test_list_and_stats(self):
        for i, status in enumerate(["done", "done", "failed", "waiting"]):
            tid = f"s{i}"
            runs.start_run(tid, sop="x", role="ops-duty", tenant="line-a", trigger="schedule")
            if status == "waiting":
                runs.mark_waiting(tid, "等数据")
            else:
                runs.finish_run(tid, status, {"cost_usd": 0.01, "cost_metered": True})
        stats = runs.stats(tenant="line-a")
        assert stats["runs"] == 4 and stats["by_status"]["done"] == 2
        assert stats["success_rate"] == round(2 / 3, 3)
        assert round(stats["total_cost_usd"], 4) == 0.03
        assert stats["by_trigger"] == {"schedule": 4}

    def test_tenant_filter(self):
        runs.start_run("a1", sop="x", tenant="line-a")
        runs.start_run("b1", sop="x", tenant="line-b")
        assert [r["thread_id"] for r in runs.list_runs(tenant="line-b")] == ["b1"]
