"""分区快照副本召回：可读层级筛选、稳定翻页、选择封存与按位置召回。

这个模块补的是快照分区“可召回性”的一层：快照表里原本只有日期、层级等展示字段，
没有真实副本位置。这里为每个逻辑分区维护一组带位置、版本和校验摘要的副本，
并提供三件事：

1. 按日期 / 目标宽表 / 可读层级筛选，分页得到稳定的“可召回分区清单”；
2. 把值班选中时的分区身份、可读层级、位置和版本封存成选择记录；
3. 召回时按分区身份和位置取副本，层级或位次变化时仍然对准同一个逻辑分区，
   缺少可核对依据时返回“待核原因”，不猜测、不按旧位次硬读。
"""
from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import RLock
from typing import Any

TIER_HOT = "热存储"
TIER_COLD = "冷存储"
TIER_ARCHIVE = "归档"

# 快照清单默认展示哪些副本：优先冷副本，其次热副本，最后归档副本。
TIER_PREFERENCE = (TIER_COLD, TIER_HOT, TIER_ARCHIVE)
READABLE_TIERS = (TIER_HOT, TIER_COLD, TIER_ARCHIVE)

TIER_ALIASES = {
    "hot": TIER_HOT,
    "热": TIER_HOT,
    "热存储": TIER_HOT,
    "cold": TIER_COLD,
    "冷": TIER_COLD,
    "冷存储": TIER_COLD,
    "archive": TIER_ARCHIVE,
    "归档": TIER_ARCHIVE,
    "归档存储": TIER_ARCHIVE,
}

STATUS_HIT = "命中"
STATUS_MOVED = "层级变更"
STATUS_RELOCATED = "位置变更"
STATUS_PENDING = "待核"

DETAIL_SIZE = 11


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_tier(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return TIER_ALIASES.get(text, text)


def _as_int(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass
class Replica:
    """分区在某个可读层级上的真实副本。position 是副本读取依据。"""

    replica_id: str
    tier: str
    position: str
    version: int
    digest: str | None
    readable: bool
    rows: list[dict[str, Any]] = field(default_factory=list)
    retired: bool = False
    archived_from: str | None = None
    archived_at: str | None = None

    @property
    def verifiable(self) -> bool:
        """副本是否具备可核对依据：有位置、有校验摘要、当前可读。"""
        return bool(self.readable and self.position and self.digest)


@dataclass
class LogicalPartition:
    """逻辑分区：即使副本迁移、列表位次变化，身份仍然稳定。"""

    partition_id: str
    partition_code: str
    target_wide_table: str
    partition_date: str
    partition_key_value: str
    schema_digest: str
    dependency_partitions: list[str] = field(default_factory=list)
    created_time: str = ""
    replicas: list[Replica] = field(default_factory=list)


class SnapshotListTokenError(RuntimeError):
    """清单翻页依据失效：继续按旧位次翻页会读错分区。"""


class SnapshotRecallService:
    """快照副本召回服务：内存态实现，接口与真实副本目录解耦。"""

    def __init__(self) -> None:
        self._lock = RLock()
        self._partitions: dict[str, LogicalPartition] = {}
        self._views: dict[str, dict[str, Any]] = {}
        self._selections: dict[str, dict[str, Any]] = {}
        self._generation = 0
        self.reset()

    # ------------------------------------------------------------------ seed
    def reset(self) -> None:
        """重置为示例目录；测试里每个用例都能从同一份数据开始。"""
        with self._lock:
            self._partitions = {}
            self._views = {}
            self._selections = {}
            self._generation = 0
            self._seed()

    def _seed(self) -> None:
        self._add_partition(
            partition_code="SNAP-0001",
            target_wide_table="WT_ORDER",
            partition_date="2026-11-18",
            partition_key_value="20261118",
            schema_digest="SHA256-ORDER-20261118",
            dependency_partitions=["20261117"],
            replicas=[
                ("hot", "hot://cn-east/warehouse/hot-02/20261118/SNAP-0001-v1", 1, "SHA256-P1-V1"),
                ("cold", "cold://cn-east/warehouse/cold-07/20261118/SNAP-0001-v2", 2, "SHA256-P1-V2"),
            ],
        )
        self._add_partition(
            partition_code="SNAP-0002",
            target_wide_table="WT_ORDER",
            partition_date="2026-11-18",
            partition_key_value="20261118",
            schema_digest="SHA256-ORDER-20261118",
            dependency_partitions=["20261117"],
            replicas=[
                ("hot", "hot://cn-east/warehouse/hot-02/20261118/SNAP-0002-v1", 1, "SHA256-P2-V1"),
                ("cold", "cold://cn-east/warehouse/cold-07/20261118/SNAP-0002-v1", 1, "SHA256-P2-V1"),
            ],
        )
        # 另一张表、同一天的 P3：按目标宽表筛选时必须被排除。
        self._add_partition(
            partition_code="SNAP-0003",
            target_wide_table="WT_ITEM",
            partition_date="2026-11-18",
            partition_key_value="20261118",
            schema_digest="SHA256-ITEM-20261118",
            replicas=[
                ("cold", "cold://cn-east/warehouse/cold-07/20261118/SNAP-0003-v1", 1, "SHA256-P3-V1"),
            ],
        )
        # 同一张表但不同日期：验证日期筛选。
        self._add_partition(
            partition_code="SNAP-0004",
            target_wide_table="WT_ORDER",
            partition_date="2026-11-17",
            partition_key_value="20261117",
            schema_digest="SHA256-ORDER-20261117",
            replicas=[
                ("cold", "cold://cn-east/warehouse/cold-07/20261117/SNAP-0004-v1", 1, "SHA256-P4-V1"),
            ],
        )

    def _add_partition(
        self,
        *,
        partition_code: str,
        target_wide_table: str,
        partition_date: str,
        partition_key_value: str,
        schema_digest: str,
        replicas: list[tuple[str, str, int, str | None]],
        dependency_partitions: list[str] | None = None,
    ) -> LogicalPartition:
        partition_id = f"LP-{target_wide_table}-{partition_key_value}-{partition_code}"
        logical = LogicalPartition(
            partition_id=partition_id,
            partition_code=partition_code,
            target_wide_table=target_wide_table,
            partition_date=partition_date,
            partition_key_value=partition_key_value,
            schema_digest=schema_digest,
            dependency_partitions=list(dependency_partitions or []),
            created_time=_now_iso(),
        )
        for index, (tier, position, version, digest) in enumerate(replicas, start=1):
            logical.replicas.append(Replica(
                replica_id=f"REP-{partition_code}-{index:02d}",
                tier=normalize_tier(tier) or tier,
                position=position,
                version=version,
                digest=digest,
                readable=True,
                rows=self._build_rows(
                    partition_code=partition_code,
                    partition_date=partition_date,
                    position=position,
                    version=version,
                ),
            ))
        self._partitions[partition_id] = logical
        return logical

    @staticmethod
    def _build_rows(
        *, partition_code: str, partition_date: str, position: str, version: int,
        count: int = DETAIL_SIZE,
    ) -> list[dict[str, Any]]:
        """每个副本固定 11 行明细，行内带读取位置，便于核对实际读的是哪份副本。"""
        rows: list[dict[str, Any]] = []
        for index in range(1, count + 1):
            rows.append({
                "行号": index,
                "订单编号": f"{partition_code}-{index:03d}",
                "分区编号": partition_code,
                "分区日期": partition_date,
                "金额": round(100 + index * 13.5 + version * 7, 2),
                "行状态": "有效",
                "副本版本": version,
                "读取位置": position,
            })
        return rows

    # -------------------------------------------------------------- lookups
    def _find_partition(
        self,
        *,
        partition_id: str | None = None,
        partition_code: str | None = None,
        target_wide_table: str | None = None,
        partition_date: str | None = None,
    ) -> LogicalPartition | None:
        if not any((partition_id, partition_code, target_wide_table, partition_date)):
            return None
        if partition_id and partition_id in self._partitions:
            return self._partitions[partition_id]
        for partition in self._partitions.values():
            if partition_code and partition.partition_code != partition_code:
                continue
            if target_wide_table and partition.target_wide_table.casefold() != target_wide_table.casefold():
                continue
            if partition_date and partition_date not in (
                partition.partition_date, partition.partition_key_value,
            ):
                continue
            return partition
        return None

    def _find_replica_by_position(self, position: str | None) -> tuple[LogicalPartition, Replica] | None:
        if not position:
            return None
        for partition in self._partitions.values():
            for replica in partition.replicas:
                if replica.position == position:
                    return partition, replica
        return None

    def _pick_replica(self, partition: LogicalPartition, tier_filter: str | None) -> Replica | None:
        readable = [r for r in partition.replicas if r.readable and r.position]
        if tier_filter:
            candidates = [r for r in readable if r.tier == tier_filter]
            return candidates[0] if candidates else None
        for tier in TIER_PREFERENCE:
            candidates = [r for r in readable if r.tier == tier]
            if candidates:
                return max(candidates, key=lambda r: (r.version, r.replica_id))
        return None

    # --------------------------------------------------------------- listing
    def _collect_items(self, filters: dict[str, str | None]) -> list[dict[str, Any]]:
        date_filter = filters.get("partition_date")
        table_filter = filters.get("target_wide_table")
        tier_filter = normalize_tier(filters.get("readable_tier"))

        partitions = sorted(
            self._partitions.values(),
            key=lambda p: (p.partition_date, p.target_wide_table, p.partition_code),
        )
        items: list[dict[str, Any]] = []
        for partition in partitions:
            if date_filter:
                key = str(date_filter).strip()
                if key not in (partition.partition_date, partition.partition_key_value):
                    continue
            if table_filter and str(table_filter).strip().casefold() not in partition.target_wide_table.casefold():
                continue
            replica = self._pick_replica(partition, tier_filter)
            if replica is None:
                continue
            items.append(self._item(partition, replica, filters))
        for index, item in enumerate(items, start=1):
            item["list_position"] = index
        return items

    def _item(
        self, partition: LogicalPartition, replica: Replica, filters: dict[str, str | None],
    ) -> dict[str, Any]:
        return {
            "partition_id": partition.partition_id,
            "partition_code": partition.partition_code,
            "target_wide_table": partition.target_wide_table,
            "partition_date": partition.partition_date,
            "partition_key_value": partition.partition_key_value,
            "readable_tier": replica.tier,
            "replica_id": replica.replica_id,
            "position": replica.position,
            "version": replica.version,
            "digest": replica.digest,
            "verifiable": replica.verifiable,
            "row_count": len(replica.rows),
            "schema_digest": partition.schema_digest,
            "list_position": 0,
            "catalog_generation": self._generation,
            "view_filters": {
                "partition_date": filters.get("partition_date"),
                "target_wide_table": filters.get("target_wide_table"),
                "readable_tier": normalize_tier(filters.get("readable_tier")),
            },
        }

    def list_recallable(
        self,
        *,
        partition_date: str | None = None,
        target_wide_table: str | None = None,
        readable_tier: str | None = None,
        page: int = 1,
        size: int = 20,
        list_token: str | None = None,
    ) -> dict[str, Any]:
        """可召回分区清单。

        第一次查询会冻结一份清单依据（list_token）。后续翻页带上 token，
        即使期间有分区归档、层级变化，页码落点也不会漂移；清单项里仍然保留
        选中当时的分区身份、层级和版本。
        """
        filters = {
            "partition_date": partition_date or None,
            "target_wide_table": target_wide_table or None,
            "readable_tier": normalize_tier(readable_tier),
        }
        with self._lock:
            if list_token:
                view = self._views.get(list_token)
                if view is None:
                    raise SnapshotListTokenError("可召回清单依据已失效，请重新筛选后再翻页")
                if not self._filters_match(view["filters"], filters):
                    raise SnapshotListTokenError("清单依据与当前筛选条件不一致，请重新筛选")
                items = copy.deepcopy(view["items"])
                token = list_token
                basis = copy.deepcopy(view["basis"])
            else:
                items = self._collect_items(filters)
                token = f"RT-{uuid.uuid4().hex}"
                basis = {
                    "captured_at": _now_iso(),
                    "catalog_generation": self._generation,
                    "filters": dict(filters),
                    "total": len(items),
                }
                self._views[token] = {
                    "items": copy.deepcopy(items),
                    "filters": dict(filters),
                    "basis": copy.deepcopy(basis),
                }
                self._trim_views()

            total = len(items)
            page = max(int(page), 1)
            size = max(min(int(size), 200), 1)
            start = (page - 1) * size
            page_items = copy.deepcopy(items[start:start + size])
            has_more = start + size < total
            return {
                "items": page_items,
                "total": total,
                "page": page,
                "size": size,
                "list_token": token,
                "has_more": has_more,
                "next_page": page + 1 if has_more else None,
                "basis": basis,
            }

    @staticmethod
    def _filters_match(left: dict[str, Any], right: dict[str, Any]) -> bool:
        for key in ("partition_date", "target_wide_table", "readable_tier"):
            if (left.get(key) or None) != (right.get(key) or None):
                return False
        return True

    def _trim_views(self, keep: int = 64) -> None:
        while len(self._views) > keep:
            oldest = next(iter(self._views))
            self._views.pop(oldest, None)

    def _current_list_position(
        self, partition_id: str, filters: dict[str, str | None], tier: str | None,
    ) -> int | None:
        current_filters = {
            "partition_date": filters.get("partition_date"),
            "target_wide_table": filters.get("target_wide_table"),
            "readable_tier": tier,
        }
        for item in self._collect_items(current_filters):
            if item["partition_id"] == partition_id:
                return int(item["list_position"])
        return None

    # ----------------------------------------------------------- selections
    def capture_selections(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """把选中项封存为选择记录，冻结分区身份、可读层级、位置和版本。"""
        selections: list[dict[str, Any]] = []
        with self._lock:
            for raw in items:
                raw = dict(raw or {})
                partition = self._find_partition(
                    partition_id=raw.get("partition_id"),
                    partition_code=raw.get("partition_code"),
                    target_wide_table=raw.get("target_wide_table"),
                    partition_date=raw.get("partition_date"),
                )
                selection: dict[str, Any] = {
                    "selection_id": f"SEL-{uuid.uuid4().hex}",
                    "recorded_at": _now_iso(),
                    "partition_id": raw.get("partition_id"),
                    "partition_code": raw.get("partition_code"),
                    "target_wide_table": raw.get("target_wide_table"),
                    "partition_date": raw.get("partition_date"),
                    "selected_tier": normalize_tier(
                        raw.get("selected_tier") or raw.get("readable_tier") or raw.get("tier")
                    ),
                    "selected_position": raw.get("selected_position") or raw.get("position"),
                    "selected_version": _as_int(
                        raw.get("selected_version")
                        if raw.get("selected_version") is not None else raw.get("version")
                    ),
                    "selected_digest": (
                        raw.get("selected_digest")
                        if raw.get("selected_digest") is not None else raw.get("digest")
                    ),
                    "selected_list_position": _as_int(
                        raw.get("selected_list_position")
                        if raw.get("selected_list_position") is not None else raw.get("list_position")
                    ),
                    "list_token": raw.get("list_token"),
                    "catalog_generation": raw.get("catalog_generation"),
                    "view_filters": dict(raw.get("view_filters") or {}),
                }
                if partition is not None:
                    selection["partition_id"] = partition.partition_id
                    selection["partition_code"] = partition.partition_code
                    selection["target_wide_table"] = partition.target_wide_table
                    selection["partition_date"] = partition.partition_date
                    if not selection["view_filters"]:
                        selection["view_filters"] = {
                            "partition_date": partition.partition_date,
                            "target_wide_table": partition.target_wide_table,
                            "readable_tier": selection["selected_tier"],
                        }
                selection["identity"] = {
                    "partition_id": selection["partition_id"],
                    "partition_code": selection["partition_code"],
                    "target_wide_table": selection["target_wide_table"],
                    "partition_date": selection["partition_date"],
                }
                selection["basis_recorded"] = bool(
                    selection["partition_id"] and selection["selected_tier"]
                    and selection["selected_position"] and selection["selected_version"] is not None
                )
                self._selections[selection["selection_id"]] = copy.deepcopy(selection)
                selections.append(selection)
        return selections

    def get_selection(self, selection_id: str) -> dict[str, Any] | None:
        stored = self._selections.get(selection_id)
        return copy.deepcopy(stored) if stored else None

    # --------------------------------------------------------------- recall
    def recall(
        self,
        *,
        selection_ids: list[str] | None = None,
        selections: list[dict[str, Any]] | None = None,
        detail_size: int = DETAIL_SIZE,
    ) -> dict[str, Any]:
        """按选择记录召回：先按身份定位逻辑分区，再按位置/摘要核对副本。"""
        pending = list(selection_ids or [])
        inline = [dict(item) for item in (selections or [])]
        collected: list[dict[str, Any]] = []

        with self._lock:
            for selection_id in pending:
                stored = self._selections.get(selection_id)
                if stored is None:
                    collected.append({
                        "selection_id": selection_id,
                        "status": STATUS_PENDING,
                        "pending_reason": "选择记录不存在，无法核对副本依据",
                        "rows": [],
                        "_unresolved": True,
                    })
                    continue
                collected.append(copy.deepcopy(stored))
            collected.extend(copy.deepcopy(inline))

            results = []
            for selection in collected:
                if selection.pop("_unresolved", False):
                    results.append(selection)
                else:
                    results.append(self._recall_one(selection, max(int(detail_size), 1)))

        return {
            "ok": True,
            "detail_size": max(int(detail_size), 1),
            "recalled": len(results),
            "hit": sum(1 for item in results if item["status"] == STATUS_HIT),
            "moved": sum(1 for item in results if item["status"] in (STATUS_MOVED, STATUS_RELOCATED)),
            "pending": sum(1 for item in results if item["status"] == STATUS_PENDING),
            "results": results,
        }

    def _recall_one(self, selection: dict[str, Any], detail_size: int) -> dict[str, Any]:
        identity = selection.get("identity") or {}
        partition_id = selection.get("partition_id") or identity.get("partition_id")
        partition = self._find_partition(
            partition_id=partition_id,
            partition_code=selection.get("partition_code") or identity.get("partition_code"),
            target_wide_table=selection.get("target_wide_table") or identity.get("target_wide_table"),
            partition_date=selection.get("partition_date") or identity.get("partition_date"),
        )
        base: dict[str, Any] = {
            "selection_id": selection.get("selection_id"),
            "partition_id": selection.get("partition_id") or identity.get("partition_id"),
            "partition_code": selection.get("partition_code") or identity.get("partition_code"),
            "target_wide_table": selection.get("target_wide_table") or identity.get("target_wide_table"),
            "partition_date": selection.get("partition_date") or identity.get("partition_date"),
            "selected_tier": selection.get("selected_tier"),
            "selected_position": selection.get("selected_position"),
            "selected_version": selection.get("selected_version"),
            "selected_digest": selection.get("selected_digest"),
            "selected_list_position": selection.get("selected_list_position"),
            "recorded_at": selection.get("recorded_at"),
            "status": STATUS_PENDING,
            "pending_reason": None,
            "conflict": None,
            "actual_tier": None,
            "actual_position": None,
            "actual_version": None,
            "actual_digest": None,
            "actual_replica_id": None,
            "current_list_position": None,
            "actual_list_position": None,
            "row_count": 0,
            "detail_row_count": 0,
            "rows": [],
        }
        if partition is None:
            base["pending_reason"] = "逻辑分区不存在或已被清理，无法核对副本依据"
            return base

        base["partition_id"] = partition.partition_id
        base["partition_code"] = partition.partition_code
        base["target_wide_table"] = partition.target_wide_table
        base["partition_date"] = partition.partition_date

        selected_position = selection.get("selected_position")
        selected_tier = normalize_tier(selection.get("selected_tier"))
        selected_version = _as_int(selection.get("selected_version"))
        selected_digest = selection.get("selected_digest")

        # 1) 先按选择时的位置取副本；位置若指向别的逻辑分区，直接待核。
        owner = self._find_replica_by_position(selected_position)
        if owner is not None and owner[0].partition_id != partition.partition_id:
            base["pending_reason"] = (
                f"所选位置 {selected_position} 当前指向逻辑分区 "
                f"{owner[0].partition_code}，不能按旧位次读取原分区"
            )
            return base

        exact = owner[1] if owner is not None else None
        if exact is not None and exact.readable and not exact.retired:
            problem = self._basis_problem(exact, selected_version, selected_digest)
            if problem is None:
                base["status"] = STATUS_HIT
                self._attach_actual(base, partition, exact, selection, detail_size)
                return base
            # 原位置仍可读但依据对不上：不给假命中，转为待核。
            base["pending_reason"] = problem
            return base

        # 2) 原位置不可读（例如冷副本已归档）：按逻辑分区身份找新的可读副本。
        readable = [r for r in partition.replicas if r.readable and r.position]
        if not readable:
            base["pending_reason"] = "该逻辑分区已没有可读副本，缺少可核对的副本依据"
            return base

        verifiable = [r for r in readable if r.digest]
        if not verifiable:
            tiers = "、".join(sorted({r.tier for r in readable}))
            base["pending_reason"] = (
                f"仅存 {tiers} 副本，但副本缺少校验摘要，无法核对是否与所选副本一致"
            )
            return base

        if not selected_digest:
            base["pending_reason"] = "选择记录未保存副本摘要，跨位置召回缺少可核对依据"
            return base

        matched = [r for r in verifiable if r.digest == selected_digest]
        if not matched:
            base["pending_reason"] = (
                "可读副本的校验摘要与选择记录不一致，无法确认是同一个逻辑分区的内容"
            )
            return base

        # 优先同版本，其次目前可读的归档副本。
        matched.sort(key=lambda r: (
            r.version != selected_version,
            r.tier != TIER_ARCHIVE,
            r.replica_id,
        ))
        actual = matched[0]
        tier_changed = actual.tier != selected_tier
        position_changed = actual.position != selected_position
        base["status"] = STATUS_MOVED if tier_changed else STATUS_RELOCATED
        base["conflict"] = {
            "kind": "readable_tier_changed" if tier_changed else "readable_position_changed",
            "message": (
                f"选择记录为 {selected_tier}@{selected_position}（v{selected_version}），"
                f"当前可读位置为 {actual.tier}@{actual.position}（v{actual.version}）"
            ),
            "selected_tier": selected_tier,
            "actual_tier": actual.tier,
            "tier_changed": tier_changed,
            "selected_position": selected_position,
            "actual_position": actual.position,
            "position_changed": position_changed,
            "selected_version": selected_version,
            "actual_version": actual.version,
            "version_changed": selected_version != actual.version,
        }
        self._attach_actual(base, partition, actual, selection, detail_size)
        return base

    @staticmethod
    def _basis_problem(replica: Replica, selected_version: int | None, selected_digest: Any) -> str | None:
        if not replica.position or not replica.digest:
            return "所选位置缺少有效的副本位置或校验摘要，无法核对副本依据"
        if selected_version is not None and replica.version != selected_version:
            return (
                f"所选位置上的副本版本为 v{replica.version}，与选择记录的 "
                f"v{selected_version} 不一致，无法核对"
            )
        if selected_digest and replica.digest != selected_digest:
            return "所选位置上的副本校验摘要与选择记录不一致，无法核对"
        return None

    def _attach_actual(
        self,
        result: dict[str, Any],
        partition: LogicalPartition,
        replica: Replica,
        selection: dict[str, Any],
        detail_size: int,
    ) -> None:
        filters = selection.get("view_filters") or {
            "partition_date": partition.partition_date,
            "target_wide_table": partition.target_wide_table,
            "readable_tier": selection.get("selected_tier"),
        }
        result["actual_tier"] = replica.tier
        result["actual_position"] = replica.position
        result["actual_version"] = replica.version
        result["actual_digest"] = replica.digest
        result["actual_replica_id"] = replica.replica_id
        result["current_list_position"] = self._current_list_position(
            partition.partition_id, filters, normalize_tier(selection.get("selected_tier")),
        )
        result["actual_list_position"] = self._current_list_position(
            partition.partition_id, filters, replica.tier,
        )
        rows = copy.deepcopy(replica.rows[:detail_size])
        result["row_count"] = len(replica.rows)
        result["detail_row_count"] = len(rows)
        result["rows"] = rows

    # -------------------------------------------------------------- archive
    def archive_partition(
        self,
        *,
        partition_id: str | None = None,
        partition_code: str | None = None,
        from_tier: str = TIER_COLD,
        verifiable: bool = True,
    ) -> dict[str, Any]:
        """把指定层级的副本归档：旧副本停止可读，新归档副本带新位置。

        verifiable=False 用来模拟归档时丢失校验摘要，召回应返回待核原因。
        """
        with self._lock:
            partition = self._find_partition(
                partition_id=partition_id, partition_code=partition_code,
            )
            if partition is None:
                return {"ok": False, "message": f"逻辑分区 {partition_id or partition_code} 不存在"}

            tier = normalize_tier(from_tier) or TIER_COLD
            source = next(
                (r for r in partition.replicas if r.readable and not r.retired and r.tier == tier),
                None,
            )
            if source is None:
                return {
                    "ok": False,
                    "message": f"分区 {partition.partition_code} 在 {tier} 没有可归档的副本",
                }

            source.readable = False
            source.retired = True
            new_position = (
                f"archive://cn-east/vault/{partition.partition_date.replace('-', '')}/"
                f"{partition.partition_code}-v{source.version}-{uuid.uuid4().hex[:6]}"
            )
            archived = Replica(
                replica_id=f"REP-{partition.partition_code}-ARCH-{uuid.uuid4().hex[:6]}",
                tier=TIER_ARCHIVE,
                position=new_position,
                version=source.version,
                digest=source.digest if verifiable else None,
                readable=True,
                rows=copy.deepcopy(source.rows),
                archived_from=source.position,
                archived_at=_now_iso(),
            )
            for row in archived.rows:
                row["读取位置"] = archived.position
                row["副本版本"] = archived.version
            partition.replicas.append(archived)
            self._generation += 1
            self._sync_generic_row(partition, archived)

            return {
                "ok": True,
                "partition_id": partition.partition_id,
                "partition_code": partition.partition_code,
                "from_tier": tier,
                "to_tier": TIER_ARCHIVE,
                "old_position": source.position,
                "new_position": archived.position,
                "version": archived.version,
                "verifiable": bool(archived.position and archived.digest),
                "message": (
                    f"分区 {partition.partition_code} 的{tier}副本已归档至 "
                    f"{archived.position}"
                ),
            }

    def _sync_generic_row(self, partition: LogicalPartition, archived: Replica) -> None:
        """同步通用快照列表里的展示字段，避免页面还显示旧的冷副本位置。"""
        try:
            from app.store import store
        except Exception:  # pragma: no cover - 理论上不会发生
            return
        for row in store.rows("snapshot_mgr"):
            if row.get("分区编号") != partition.partition_code:
                continue
            row["可读层级"] = archived.tier
            row["副本位置"] = archived.position
            row["副本版本"] = str(archived.version)
            row["状态标记"] = "已归档"
            row["partition_id"] = partition.partition_id
            break

    def partition_hint(self, partition_code: str) -> str | None:
        """给通用列表补一句召回提示：按当前可读副本能不能核对。"""
        partition = self._find_partition(partition_code=partition_code)
        if partition is None:
            return None
        replica = self._pick_replica(partition, None)
        if replica is None:
            return "该分区已没有可读副本，召回会返回待核"
        if not replica.digest:
            return f"仅存{replica.tier}副本且缺少校验摘要，召回会返回待核"
        return f"{replica.tier}副本可读，位置 {replica.position}，v{replica.version}"

    # --------------------------------------------------------------- detail
    def read_detail(
        self,
        *,
        partition_code: str,
        readable_tier: str | None = None,
        size: int = DETAIL_SIZE,
    ) -> dict[str, Any]:
        """直接按分区和层级读取明细，供页面核对使用；召回路径不依赖它。"""
        with self._lock:
            partition = self._find_partition(partition_code=partition_code)
            if partition is None:
                return {"ok": False, "message": f"逻辑分区 {partition_code} 不存在"}
            replica = self._pick_replica(partition, normalize_tier(readable_tier))
            if replica is None:
                return {"ok": False, "message": f"分区 {partition_code} 没有可读副本"}
            rows = copy.deepcopy(replica.rows[:max(int(size), 1)])
            return {
                "ok": True,
                "partition_id": partition.partition_id,
                "partition_code": partition.partition_code,
                "target_wide_table": partition.target_wide_table,
                "readable_tier": replica.tier,
                "position": replica.position,
                "version": replica.version,
                "row_count": len(replica.rows),
                "detail_row_count": len(rows),
                "rows": rows,
            }


snapshot_recall_service = SnapshotRecallService()
