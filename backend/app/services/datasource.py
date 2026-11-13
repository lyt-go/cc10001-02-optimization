"""采集源登记业务规则：状态流转、字段校验与筛选口径都收在这里。"""
from __future__ import annotations

from typing import Any

from app.services.accessverify import access_verify_service
from app.store import store

MODULE = "datasource"
REQUIRED_FIELDS = ["源端编号", "目标域名", "入口URL"]
STATUS_ORDER = ["草稿", "连通测试中", "已上线", "已归档"]
ACTION_RULES = {"发起连通测试": "连通测试中", "标记上线": "已上线", "归档源配置": "已归档"}
NEGATIVE_ACTIONS = []


class DatasourceService:
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
            rows = [row for row in rows if keyword in str(row.get("源端编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        page_rows = [access_verify_service.decorate_source(dict(row)) for row in rows[start:start + size]]
        return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        return access_verify_service.decorate_source(dict(entry))

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        # 连通指纹四项允许登记时一并落库，缺省给空串，后续探测按持久化值计算。
        for field in ("请求方式", "字符编码", "访问频率", "超时秒数", "归属项目"):
            entry[field] = values.get(field, "")
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return access_verify_service.decorate_source(dict(entry)), []

    def update_config(
        self, entry_id: int, values: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str]:
        """更新采集源持久化配置（指纹四项等）。

        指纹在读取时按当前持久化配置实时计算，因此入口URL/请求方式/字符编码/
        超时秒数任一变化后，旧验证结论立即失效；历史验证记录不删除，按其当时
        有效期保留。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集源 {entry_id} 不存在或已归档"
        editable = ("目标域名", "入口URL", "请求方式", "字符编码", "访问频率", "超时秒数", "归属项目")
        changed = [field for field in editable if field in values]
        if not changed:
            return None, "没有可更新的采集源配置字段"
        for field in changed:
            entry[field] = values[field]
        decorated = access_verify_service.decorate_source(dict(entry))
        if decorated["接入验证状态"] == "待重新验证":
            message = "采集源配置已更新，采集指纹变化，历史连通结论已失效，请重新发起连通测试"
        else:
            message = "采集源配置已更新，当前连通结论仍在有效期内"
        return decorated, message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"采集源 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于采集源登记可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"

        if action == "发起连通测试":
            # 连通测试统一走接入验证服务：指纹+有效期复用、并发去重、诊断记录都在里面。
            tested, message, _reused = access_verify_service.run_connectivity_test(entry_id)
            if tested is None:
                return None, message
            return access_verify_service.decorate_source(dict(tested)), message

        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return access_verify_service.decorate_source(dict(entry)), f"采集源已{action}"
