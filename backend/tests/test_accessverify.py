"""接入验证链路的服务层回归测试。

只用标准库 unittest，不依赖 pytest/httpx；在 backend 目录下执行：

    .venv/bin/python -m unittest discover -s tests -v

覆盖：指纹复用与有效期、配置变更即时失效、历史记录保留、并发/重复去重、
失败诊断去重、规则版本递增、任务调度闸门以及采集源/任务同一结论。
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from app.seed import SEED_ROWS
from app.services.accessverify import (
    DEFAULT_TTL_SECONDS,
    AccessVerifyService,
)
from app.store import Store


class AccessVerifyChainTest(unittest.TestCase):
    def setUp(self) -> None:
        # 每个用例使用独立仓库与独立验证服务，互不串状态。
        self.store = Store.__new__(Store)
        self.store._tables = {
            name: [dict(row) for row in rows] for name, rows in SEED_ROWS.items()
        }
        # 把全局 store 替换成本用例仓库，服务内部通过 app.store.store 访问。
        import app.store as store_module

        self._store_patch = store_module.store
        store_module.store = self.store
        import app.services.accessverify as av_module

        av_module.store = self.store
        self.service = AccessVerifyService()
        self.now = datetime(2026, 11, 13, 2, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        import app.store as store_module
        import app.services.accessverify as av_module

        store_module.store = self._store_patch
        av_module.store = self._store_patch

    # ---- 工具 --------------------------------------------------------
    def _source(self, source_id: int) -> dict:
        return self.store.find("datasource", source_id)

    def _task(self, task_id: int) -> dict:
        return self.store.find("taskcrawl", task_id)

    # ---- 用例 --------------------------------------------------------
    def test_seed_source_starts_with_effective_conclusion(self) -> None:
        # 已上线采集源 DATA-0003 预置了未过期、同指纹的成功结论。
        decorated = self.service.decorate_source(dict(self._source(3)), now=self.now)
        self.assertEqual(decorated["接入验证状态"], "验证有效")
        self.assertTrue(decorated["可调度"])
        self.assertEqual(decorated["结论复用"], "可复用")

    def test_repeated_test_reuses_conclusion_without_new_record(self) -> None:
        before = len(self.service.records)
        _, msg1, reused1 = self.service.run_connectivity_test(3, now=self.now)
        _, msg2, reused2 = self.service.run_connectivity_test(3, now=self.now + timedelta(minutes=1))
        self.assertTrue(reused1 and reused2)
        self.assertEqual(len(self.service.records), before)
        self.assertIn("复用", msg1)
        self.assertIn("复用", msg2)

    def test_ttl_default_is_twelve_minutes(self) -> None:
        source = self._source(3)
        # 种子访问频率为「12分钟」，与默认 TTL 口径一致。
        self.assertEqual(self.service.ttl_seconds_for(source), 12 * 60)
        plain = dict(source)
        plain.pop("访问频率", None)
        self.assertEqual(self.service.ttl_seconds_for(plain), DEFAULT_TTL_SECONDS)

    def test_fingerprint_changes_when_any_of_four_fields_changes(self) -> None:
        source = self._source(1)
        baseline = self.service.fingerprint_of(source)
        for field, new_value in (
            ("入口URL", "https://example.com/another"),
            ("请求方式", "POST"),
            ("字符编码", "GBK"),
            ("超时秒数", "30"),
        ):
            changed = dict(source)
            changed[field] = new_value
            self.assertNotEqual(
                self.service.fingerprint_of(changed),
                baseline,
                f"{field} 变化必须改变采集指纹",
            )

    def test_config_change_invalidates_old_conclusion_but_keeps_history(self) -> None:
        # 先让 DATA-0001 获得一条有效结论。
        self.service.run_connectivity_test(1, now=self.now)
        self.assertEqual(
            self.service.decorate_source(dict(self._source(1)), now=self.now)["接入验证状态"],
            "验证有效",
        )
        history_before = len(self.service.history_for(1))

        # 改入口URL：指纹变化，旧结论立即失效。
        self._source(1)["入口URL"] = "https://example.com/news/v2"
        decorated = self.service.decorate_source(dict(self._source(1)), now=self.now)
        self.assertEqual(decorated["接入验证状态"], "待重新验证")
        self.assertFalse(decorated["可调度"])

        # 历史记录不删除，旧记录标注为已失效。
        history = self.service.history_for(1)
        self.assertEqual(len(history), history_before)
        self.assertTrue(all(row["记录状态"] == "已失效" for row in history))

    def test_expired_record_is_not_reused_but_retained(self) -> None:
        self.service.run_connectivity_test(1, now=self.now)
        record = self.service.latest_effective_record(self._source(1), now=self.now)
        self.assertIsNotNone(record)
        # 越过有效期后不可复用。
        later = self.now + timedelta(seconds=DEFAULT_TTL_SECONDS + 1)
        self.assertIsNone(self.service.latest_effective_record(self._source(1), now=later))
        # 历史仍可查到该记录。
        self.assertEqual(len(self.service.history_for(1)), 1)

    def test_failed_probe_writes_single_diagnose_and_dedupes_repeats(self) -> None:
        # DATA-0002 种子入口URL 无 http(s) 前缀，探测失败。
        diagnose_rows = self.store.rows("diagnose")
        initial = len(diagnose_rows)
        self.service.run_connectivity_test(2, now=self.now)
        self.service.run_connectivity_test(2, now=self.now + timedelta(seconds=5))
        self.service.run_connectivity_test(2, now=self.now + timedelta(seconds=10))
        conn_diagnoses = [
            row for row in diagnose_rows if row.get("异常类型") == "连通探测失败"
        ]
        self.assertEqual(len(conn_diagnoses), 1)
        self.assertEqual(len(diagnose_rows), initial + 1)

        # 指纹变化（修正为合法 URL）后允许新的一次探测，并成功落记录。
        self._source(2)["入口URL"] = "https://shop.example/goods"
        _, _, reused = self.service.run_connectivity_test(2, now=self.now)
        self.assertFalse(reused)
        self.assertEqual(
            self.service.decorate_source(dict(self._source(2)), now=self.now)["接入验证状态"],
            "验证有效",
        )

    def test_rule_leaving_enabled_bumps_version_and_blocks_task(self) -> None:
        # PARS-0003 初始已启用、版本 1；TASK-0003 初始可调度。
        rule = self.store.find("parserule", 3)
        self.assertEqual(rule["status"], "已启用")
        self.assertTrue(
            self.service.decorate_task(dict(self._task(3)), now=self.now)["可调度"]
        )

        # 已启用 -> 规则验证中：版本递增，任务立即不可用并给出原因。
        # 与 ParseruleService.run_action 一致：先落目标状态，再按旧状态判断是否递增。
        rule["status"] = "规则验证中"
        bumped = self.service.bump_rule_version_if_needed(rule, "已启用")
        self.assertEqual(bumped, 2)
        task_view = self.service.decorate_task(dict(self._task(3)), now=self.now)
        self.assertFalse(task_view["可调度"])
        self.assertIn("规则验证中", task_view["不可用原因"])
        self.assertEqual(task_view["关联规则版本"], 2)
        self.assertIsNotNone(self.service.ensure_schedulable(self._task(3)))

        # 重新启用：连通结论仍有效，调度入口恢复。
        rule["status"] = "已启用"
        self.service.bump_rule_version_if_needed(rule, "规则验证中")
        self.assertIsNone(self.service.ensure_schedulable(self._task(3)))

    def test_task_and_source_share_single_effective_status(self) -> None:
        source_view = self.service.decorate_source(dict(self._source(3)), now=self.now)
        task_view = self.service.decorate_task(dict(self._task(3)), now=self.now)
        self.assertEqual(source_view["采集指纹"], task_view["采集指纹"])
        self.assertEqual(source_view["最近成功时间"], task_view["最近成功时间"])
        self.assertEqual(source_view["有效到期时间"], task_view["有效到期时间"])
        self.assertEqual(source_view["接入验证状态"], task_view["接入验证状态"])

    def test_schedule_gate_collects_all_unavailable_reasons(self) -> None:
        # TASK-0001：规则 PARS-0001 为草稿，且 DATA-0001 尚无有效连通结论。
        view = self.service.decorate_task(dict(self._task(1)), now=self.now)
        self.assertFalse(view["可调度"])
        self.assertIn("PARS-0001", view["不可用原因"])
        self.assertIn("DATA-0001", view["不可用原因"])
        # 补齐连通结论后，规则原因仍然存在。
        self.service.run_connectivity_test(1, now=self.now)
        view2 = self.service.decorate_task(dict(self._task(1)), now=self.now)
        self.assertFalse(view2["可调度"])
        self.assertIn("PARS-0001", view2["不可用原因"])
        self.assertNotIn("DATA-0001", view2["不可用原因"])

    def test_missing_source_or_rule_is_reported(self) -> None:
        broken = dict(self._task(1))
        broken["源端编号"] = "DATA-9999"
        broken["规则编号"] = "PARS-9999"
        view = self.service.decorate_task(broken, now=self.now)
        self.assertFalse(view["可调度"])
        self.assertIn("不存在", view["不可用原因"])


if __name__ == "__main__":
    unittest.main()
