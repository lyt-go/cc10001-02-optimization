"""采集任务业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.accessverify import access_verify_service
from app.store import store

MODULE = "taskcrawl"
REQUIRED_FIELDS = ["任务编号", "采集源名称", "规则编号"]
STATUS_ORDER = ["草稿", "待调度", "执行中", "已完成", "已终止"]
ACTION_RULES = {"提交调度": "待调度", "启动任务": "执行中", "终止任务": "已终止"}
NEGATIVE_ACTIONS = []


class TaskcrawlService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("任务编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        # 与采集源详情共用 access_verify_service 的同一份有效状态结论。
        page_rows = [access_verify_service.decorate_task(dict(row)) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return access_verify_service.decorate_task(dict(entry))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 源端编号用于与采集源的接入验证结论联动，允许登记时显式指定。
        entry["源端编号"] = values.get("源端编号", "")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return access_verify_service.decorate_task(dict(entry)), []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集任务 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集任务可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        # 提交调度是调度入口：连通结论失效或关联规则不可用时同步拦截，
        # 保证接入验证与任务执行保持同一结论。按项目动作契约返回 None 表示未生效，
        # 不可用原因经路由层 ActionResult.ok=False 透出，任务状态保持不变。
        if action == "提交调度":
            reason = access_verify_service.ensure_schedulable(entry)
            if reason:
                return None, f"调度入口不可用：{reason}"

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return access_verify_service.decorate_task(dict(entry)), f"采集任务已{action}"
