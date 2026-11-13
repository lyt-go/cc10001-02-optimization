"""接入验证链路：连通探测、采集指纹复用与解析规则可用性联动的统一业务入口。

设计要点（对应接入验证链路优化需求）：

1. 连通结论按「采集指纹 + 有效期」复用。指纹由已持久化的入口URL、请求方式、
   字符编码、超时秒数四项归一化后生成；有效期默认 12 分钟，可被访问频率口径
   覆盖。未过期且指纹一致时直接复用最近一次成功结论，不再重发探测。
2. 配置（入口URL/请求方式/字符编码/超时秒数）一旦变化，实时计算出的指纹随之
   变化，旧结论立即失效；历史验证记录不删除，按其当时的有效期保留。
3. 每个采集源串行化：重复点击或连续触发只保留一次有效结果，避免重复探测请求
   与重复诊断记录。
4. 解析规则从「已启用」变为「规则验证中/已废弃」时规则版本递增，关联采集任务
   的调度入口同步得到不可用原因，采集任务清单与采集源详情共用同一份有效状态。
"""
from __future__ import annotations

import hashlib
import json
import threading
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

DATASOURCE_MODULE = "datasource"
PARSERULE_MODULE = "parserule"
TASK_MODULE = "taskcrawl"
DIAGNOSE_MODULE = "diagnose"

# 连通结论有效期：12 分钟（需求约定），真实项目可下沉到配置中心。
DEFAULT_TTL_SECONDS = 12 * 60

# 采集指纹包含的四项配置；访问频率不进指纹，只参与有效期口径展示。
FINGERPRINT_FIELDS = ("入口URL", "请求方式", "字符编码", "超时秒数")

# 连通验证在采集源/任务行上透出的派生字段，前端两页共用同一份状态。
VERIFY_FIELDS = (
    "接入验证状态",
    "不可用原因",
    "最近成功时间",
    "有效到期时间",
    "采集指纹",
    "结论复用",
    "关联规则版本",
    "可调度",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def isoformat(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


class AccessVerifyService:
    """接入验证的单一事实源：连通记录、规则版本与任务可用性都从这里派生。"""

    def __init__(self, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> None:
        self._ttl_seconds = ttl_seconds
        # 连通验证历史记录：每次成功探测一条，失败不产生复用记录，全部保留。
        self._records: list[dict[str, Any]] = []
        # 每个采集源一把锁 + in-flight 标记，串行化重复/并发探测。
        self._locks: dict[int, threading.Lock] = {}
        self._inflight: set[int] = set()
        self._locks_guard = threading.Lock()
        # 诊断报告自增序号，与仓库现有 id 口径保持独立。
        self._diagnose_seq = 0
        self.bootstrap_demo_records()

    # ------------------------------------------------------------------
    # 指纹与有效期
    # ------------------------------------------------------------------
    def fingerprint_of(self, source: dict[str, Any]) -> str:
        """按已持久化的四项配置计算采集指纹；指纹冲突时以当前配置为准。"""
        payload = {
            field: str(source.get(field) or "").strip()
            for field in FINGERPRINT_FIELDS
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def ttl_seconds_for(self, source: dict[str, Any]) -> int:
        """访问频率参与有效期口径：解析失败时回落到默认 12 分钟。"""
        raw = str(source.get("访问频率") or "").strip()
        # 支持「12分钟」「720秒」「720s」等简单写法，不识别就用默认值。
        if raw:
            digits = "".join(ch for ch in raw if ch.isdigit())
            if digits:
                value = int(digits)
                if "小时" in raw or "h" in raw.lower():
                    return value * 3600
                if "分钟" in raw or "min" in raw.lower():
                    return value * 60
                return value
        return self._ttl_seconds

    # ------------------------------------------------------------------
    # 历史验证记录（保留全部，按当时有效期判断）
    # ------------------------------------------------------------------
    @property
    def records(self) -> list[dict[str, Any]]:
        return self._records

    def history_for(self, source_id: int) -> list[dict[str, Any]]:
        rows = [
            dict(row)
            for row in self._records
            if int(row.get("采集源id", 0)) == source_id
        ]
        now = utc_now()
        for row in rows:
            row["记录状态"] = (
                "有效"
                if row.get("有效到期时间")
                and datetime.fromisoformat(row["有效到期时间"]) > now
                and row.get("采集指纹") == self._current_fingerprint(source_id)
                else "已失效"
            )
        return sorted(rows, key=lambda row: row["探测时间"], reverse=True)

    def _current_fingerprint(self, source_id: int) -> str | None:
        source = store.find(DATASOURCE_MODULE, source_id)
        return self.fingerprint_of(source) if source else None

    def latest_effective_record(
        self, source: dict[str, Any], *, now: datetime | None = None
    ) -> dict[str, Any] | None:
        """找到当前配置下仍在有效期内的最近一条成功记录。"""
        now = now or utc_now()
        fingerprint = self.fingerprint_of(source)
        candidates = [
            row
            for row in self._records
            if int(row.get("采集源id", 0)) == int(source.get("id", 0))
            and row.get("采集指纹") == fingerprint
            and datetime.fromisoformat(row["有效到期时间"]) > now
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda row: row["探测时间"])

    # ------------------------------------------------------------------
    # 连通探测（带复用与并发去重）
    # ------------------------------------------------------------------
    def run_connectivity_test(
        self, source_id: int, *, now: datetime | None = None
    ) -> tuple[dict[str, Any] | None, str, bool]:
        """对采集源发起连通测试。

        返回 ``(采集源行, 消息, 是否复用结论)``；采集源不存在时首项为 None。
        - 指纹一致且未过期：直接复用最近成功结论，不重发探测、不新增诊断记录。
        - 已有同采集源探测在执行：等待并复用其结果，重复请求不产生第二条记录。
        - 否则执行一次模拟探测：成功落一条历史记录，失败落一条诊断记录。
        """
        now = now or utc_now()
        source = store.find(DATASOURCE_MODULE, source_id)
        if source is None:
            return None, f"采集源 {source_id} 不存在或已归档", False

        lock = self._get_lock(source_id)
        with lock:
            # 双重检查：等锁期间可能已有并发请求完成了探测。
            cached = self.latest_effective_record(source, now=now)
            if cached is not None:
                source["status"] = "连通测试中"
                return source, self._reuse_message(source, cached), True

            # 同采集源已有探测在途（极端并发下由锁串行，in-flight 再兜一层），
            # 这里直接按当前结论返回，保证只保留一次有效结果。
            if source_id in self._inflight:
                cached = self.latest_effective_record(source, now=now)
                if cached is not None:
                    return source, self._reuse_message(source, cached), True
                return source, "采集源连通测试正在执行中，请勿重复触发", False

            # 同指纹下已有未结案的失败诊断：重复触发直接复用诊断，不重发探测、
            # 不新增诊断记录；配置变化导致指纹变化后自然放行新探测。
            open_diagnose = self._open_diagnose_for(source)
            if open_diagnose is not None:
                source["status"] = "连通测试中"
                source["abnormal"] = True
                return (
                    source,
                    f"连通探测失败，诊断记录 {open_diagnose['报告编号']} 仍未结案，"
                    "重复触发不再重发探测或生成新记录",
                    False,
                )

            self._inflight.add(source_id)
            try:
                record = self._probe(source, now=now)
            finally:
                self._inflight.discard(source_id)

            if record is None:
                source["status"] = "连通测试中"
                source["abnormal"] = True
                diagnose, created = self._write_diagnose(source, now=now)
                if created:
                    return (
                        source,
                        f"连通探测失败，已生成诊断记录 {diagnose['报告编号']}，请调整配置后重试",
                        False,
                    )
                return (
                    source,
                    f"连通探测失败，诊断记录 {diagnose['报告编号']} 仍未结案，"
                    "重复触发不再生成新记录",
                    False,
                )

            self._records.append(record)
            source["status"] = "连通测试中"
            source["abnormal"] = False
            return source, (
                f"连通探测成功，结论有效期至 {record['有效到期时间'][11:16]}，"
                f"有效期内重复测试将直接复用该结论"
            ), False

    def _probe(
        self, source: dict[str, Any], *, now: datetime
    ) -> dict[str, Any] | None:
        """模拟一次真实探测：入口URL 为 http(s) 地址视为成功。

        真实项目这里替换为 requests/httppro 调用；成功才写历史记录，
        失败交由调用方落诊断记录。
        """
        url = str(source.get("入口URL") or "").strip().lower()
        if not (url.startswith("http://") or url.startswith("https://")):
            return None
        ttl = self.ttl_seconds_for(source)
        return {
            "记录编号": f"CONN-{len(self._records) + 1:04d}",
            "采集源id": int(source["id"]),
            "源端编号": source.get("源端编号"),
            "采集指纹": self.fingerprint_of(source),
            "入口URL": source.get("入口URL"),
            "请求方式": source.get("请求方式"),
            "字符编码": source.get("字符编码"),
            "超时秒数": source.get("超时秒数"),
            "访问频率": source.get("访问频率"),
            "关联规则版本": self._rule_version_for_source(source),
            "探测时间": isoformat(now),
            "有效到期时间": isoformat(now + timedelta(seconds=ttl)),
            "有效期秒数": ttl,
            "探测结论": "成功",
        }

    def _reuse_message(self, source: dict[str, Any], record: dict[str, Any]) -> str:
        return (
            f"采集指纹与 {record['记录编号']} 一致且结论未过期，"
            f"复用 {record['探测时间'][11:16]} 的成功结论，不再重发探测"
        )

    # ------------------------------------------------------------------
    # 诊断记录（失败才写，且同一指纹下未结案诊断至多一条）
    # ------------------------------------------------------------------
    def _open_diagnose_for(self, source: dict[str, Any]) -> dict[str, Any] | None:
        """同采集源、同指纹下是否还有未结案的失败诊断。"""
        fingerprint = self.fingerprint_of(source)
        for row in store.rows(DIAGNOSE_MODULE):
            if (
                row.get("异常类型") == "连通探测失败"
                and row.get("关联源端id") == int(source.get("id", 0))
                and row.get("采集指纹") == fingerprint
                and row.get("status") not in ("已结案", "已归档")
            ):
                return row
        return None

    def _write_diagnose(
        self, source: dict[str, Any], *, now: datetime
    ) -> tuple[dict[str, Any], bool]:
        """落一条失败诊断；同指纹已有未结案诊断时直接复用，返回 (记录, 是否新建)。"""
        existing = self._open_diagnose_for(source)
        if existing is not None:
            return existing, False
        self._diagnose_seq += 1
        rows = store.rows(DIAGNOSE_MODULE)
        entry = {
            "id": max((int(row.get("id", 0)) for row in rows), default=0) + 1,
            "status": "分析中",
            "pending": True,
            "abnormal": True,
            "报告编号": f"DIAG-CONN-{self._diagnose_seq:04d}",
            "关联任务": source.get("源端编号"),
            "关联源端id": int(source.get("id", 0)),
            "采集指纹": self.fingerprint_of(source),
            "异常类型": "连通探测失败",
            "异常堆栈": (
                f"入口URL={source.get('入口URL')}；请求方式={source.get('请求方式')}；"
                f"采集指纹={self.fingerprint_of(source)}"
            ),
            "发生时间": isoformat(now),
            "影响行数": 0,
            "根因分类": "接入配置不可达",
            "修复建议": "核对入口URL、请求方式与超时配置后重新发起连通测试",
        }
        rows.append(entry)
        return entry, True

    # ------------------------------------------------------------------
    # 采集源详情/清单的有效状态（任务清单复用同一份结论）
    # ------------------------------------------------------------------
    def decorate_source(
        self, source: dict[str, Any], *, now: datetime | None = None
    ) -> dict[str, Any]:
        """给采集源行补充接入验证派生字段，不改动原始配置字段。"""
        now = now or utc_now()
        record = self.latest_effective_record(source, now=now)
        rule_version = self._rule_version_for_source(source)
        if record is not None:
            source["接入验证状态"] = "验证有效"
            source["不可用原因"] = ""
            source["最近成功时间"] = record["探测时间"]
            source["有效到期时间"] = record["有效到期时间"]
            source["结论复用"] = "可复用"
        else:
            source["接入验证状态"] = "待重新验证"
            source["不可用原因"] = "无未过期且指纹一致的连通结论，请重新发起连通测试"
            source["最近成功时间"] = self._last_success_time(source)
            source["有效到期时间"] = ""
            source["结论复用"] = "不可复用"
        source["采集指纹"] = self.fingerprint_of(source)
        source["关联规则版本"] = rule_version
        source["可调度"] = record is not None
        return source

    def _last_success_time(self, source: dict[str, Any]) -> str:
        history = [
            row
            for row in self._records
            if int(row.get("采集源id", 0)) == int(source.get("id", 0))
        ]
        if not history:
            return ""
        return max(history, key=lambda row: row["探测时间"])["探测时间"]

    # ------------------------------------------------------------------
    # 解析规则联动：规则版本与任务可用性
    # ------------------------------------------------------------------
    def bump_rule_version_if_needed(
        self, rule: dict[str, Any], previous_status: str
    ) -> int:
        """规则从已启用变为验证中/已废弃时版本递增；其他流转保持原版本。"""
        version = int(rule.get("规则版本") or 1)
        if previous_status == "已启用" and rule.get("status") in ("规则验证中", "已废弃"):
            version += 1
        rule["规则版本"] = version
        return version

    def decorate_task(
        self, task: dict[str, Any], *, now: datetime | None = None
    ) -> dict[str, Any]:
        """给采集任务行补充与采集源详情一致的有效状态与不可用原因。"""
        now = now or utc_now()
        source = self._find_source_by_code(task.get("源端编号"))
        rule = self._find_rule_by_code(task.get("规则编号"))

        reasons: list[str] = []
        if source is None:
            reasons.append(f"关联采集源 {task.get('源端编号') or '（空）'} 不存在")
        if rule is None:
            reasons.append(f"关联解析规则 {task.get('规则编号') or '（空）'} 不存在")
        elif rule.get("status") != "已启用":
            reasons.append(
                f"关联解析规则 {rule.get('规则编号')} 当前为「{rule.get('status')}」"
                "（已启用规则才能支撑调度）"
            )

        effective_record = None
        if source is not None:
            effective_record = self.latest_effective_record(source, now=now)
            if effective_record is None:
                reasons.append(
                    f"关联采集源 {source.get('源端编号')} 缺少未过期且指纹一致的连通结论"
                )

        if source is not None:
            task["接入验证状态"] = "验证有效" if effective_record else "待重新验证"
            task["采集指纹"] = self.fingerprint_of(source)
            task["最近成功时间"] = (
                effective_record["探测时间"] if effective_record else self._last_success_time(source)
            )
            task["有效到期时间"] = effective_record["有效到期时间"] if effective_record else ""
            task["关联规则版本"] = int(rule.get("规则版本") or 1) if rule else ""
        else:
            task["接入验证状态"] = "待重新验证"
            task["采集指纹"] = ""
            task["最近成功时间"] = ""
            task["有效到期时间"] = ""
            task["关联规则版本"] = ""

        task["不可用原因"] = "；".join(reasons)
        task["可调度"] = not reasons
        return task

    def _rule_version_for_source(self, source: dict[str, Any]) -> int:
        """采集源按同域名字面匹配启用中的规则版本，取不到时给 1。"""
        rule = self._find_rule_by_code(source.get("源端编号"))
        return int(rule.get("规则版本") or 1) if rule else 1

    def _find_source_by_code(self, code: Any) -> dict[str, Any] | None:
        code = str(code or "").strip()
        if not code:
            return None
        for row in store.rows(DATASOURCE_MODULE):
            if str(row.get("源端编号") or "").strip() == code:
                return row
        return None

    def _find_rule_by_code(self, code: Any) -> dict[str, Any] | None:
        code = str(code or "").strip()
        if not code:
            return None
        for row in store.rows(PARSERULE_MODULE):
            if str(row.get("规则编号") or "").strip() == code:
                return row
        return None

    # ------------------------------------------------------------------
    # 调度入口可用性校验：接入验证与任务执行保持同一结论
    # ------------------------------------------------------------------
    def ensure_schedulable(self, task: dict[str, Any]) -> str | None:
        """任务提交调度前的统一闸门，返回 None 表示可调度，否则返回原因。"""
        decorated = self.decorate_task(dict(task))
        if decorated["可调度"]:
            return None
        return decorated["不可用原因"]

    def _get_lock(self, source_id: int) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(source_id)
            if lock is None:
                lock = threading.Lock()
                self._locks[source_id] = lock
            return lock

    # ------------------------------------------------------------------
    # 测试支撑：重置为种子数据对应的初始验证状态
    # ------------------------------------------------------------------
    def reset(self) -> None:
        """清空进程内验证记录并按当前仓库数据重新预置，仅供测试/重置场景使用。"""
        self._records.clear()
        self._inflight.clear()
        self._diagnose_seq = 0
        self.bootstrap_demo_records()

    # ------------------------------------------------------------------
    # 示例数据：演示复用与历史失效两种场景
    # ------------------------------------------------------------------
    def bootstrap_demo_records(self) -> None:
        """给已上线示例采集源预置两条历史记录。

        - 一条指纹与当前持久化配置一致、仍在 12 分钟有效期内：打开页面即看到
          「验证有效 / 可复用」，再点连通测试直接复用、不重发探测。
        - 一条是旧指纹的过期记录：验证历史保留，但不再参与当前结论。
        """
        now = utc_now()
        for source in store.rows(DATASOURCE_MODULE):
            if source.get("status") != "已上线":
                continue
            ttl = self.ttl_seconds_for(source)
            current_fingerprint = self.fingerprint_of(source)
            self._records.append(
                {
                    "记录编号": f"CONN-{len(self._records) + 1:04d}",
                    "采集源id": int(source["id"]),
                    "源端编号": source.get("源端编号"),
                    "采集指纹": current_fingerprint,
                    "入口URL": source.get("入口URL"),
                    "请求方式": source.get("请求方式"),
                    "字符编码": source.get("字符编码"),
                    "超时秒数": source.get("超时秒数"),
                    "访问频率": source.get("访问频率"),
                    "关联规则版本": self._rule_version_for_source(source),
                    "探测时间": isoformat(now - timedelta(minutes=3)),
                    "有效到期时间": isoformat(now - timedelta(minutes=3) + timedelta(seconds=ttl)),
                    "有效期秒数": ttl,
                    "探测结论": "成功",
                }
            )
            # 旧指纹的历史记录：保留可查，但已过期且指纹不匹配，结论不可复用。
            old_source = dict(source)
            old_source["入口URL"] = f"{source.get('入口URL')}#legacy"
            self._records.append(
                {
                    "记录编号": f"CONN-{len(self._records) + 1:04d}",
                    "采集源id": int(source["id"]),
                    "源端编号": source.get("源端编号"),
                    "采集指纹": self.fingerprint_of(old_source),
                    "入口URL": old_source["入口URL"],
                    "请求方式": source.get("请求方式"),
                    "字符编码": source.get("字符编码"),
                    "超时秒数": source.get("超时秒数"),
                    "访问频率": source.get("访问频率"),
                    "关联规则版本": max(int(source.get("规则版本") or 1), 1),
                    "探测时间": isoformat(now - timedelta(minutes=30)),
                    "有效到期时间": isoformat(now - timedelta(minutes=30) + timedelta(seconds=ttl)),
                    "有效期秒数": ttl,
                    "探测结论": "成功",
                }
            )


# 进程级单例：路由、各 service 与刷新后的页面请求共享同一份验证状态。
access_verify_service = AccessVerifyService()
