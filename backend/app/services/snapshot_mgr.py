"""快照分区领域服务：在通用列表之上补齐日期、目标宽表和可读层级筛选。

列表字段保持原有展示列不变；筛选条件额外接受“可读层级”“副本位置”“副本版本”，
这些字段由快照副本目录维护（归档后同步），页面上按真实副本位置筛选和核对。
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService


class SnapshotMgrService(BaseModuleService):
    MODULE = "snapshot_mgr"
    ENTITY = "快照分区"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["分区编号", "目标宽表", "分区键值", "记录行数", "Schema摘要", "状态标记", "依赖分区", "创建时间"]
    FILTER_FIELDS = ["可读层级", "副本位置", "副本版本", "partition_id"]
    REQUIRED_FIELDS = ["分区编号", "目标宽表", "分区键值"]
    STATUS_ORDER = ["创建中", "可用", "标记中", "已过期"]
    ACTION_RULES = {"创建快照": "可用", "标记版本": "标记中", "清理过期": "已过期"}
    NEGATIVE_ACTIONS = []

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        entry = super().get_entry(entry_id)
        if entry is None:
            return None
        return self._with_recall_hint(entry)

    def _with_recall_hint(self, entry: dict[str, Any]) -> dict[str, Any]:
        """补一列召回提示，让页面知道该分区当前有没有可核对的可读副本。"""
        try:
            from app.snapshot_recall import snapshot_recall_service
        except Exception:  # pragma: no cover - 理论上不会发生
            return entry
        hint = snapshot_recall_service.partition_hint(str(entry.get("分区编号", "")))
        if hint is None:
            return entry
        return {**entry, "召回提示": hint}
