"""快照副本召回测试：筛选翻页、选择封存、归档冲突与按位置召回。

对应值班场景：
- 先查包含 P1/P2 的两页，排除另一张表同日的 P3；
- 归档 P1 的冷副本后立即提交旧选择，再重新筛选归档结果并召回；
- 每次召回都检查实际读取位置，或给出可核对的待核原因。
"""
from __future__ import annotations

import pytest

from app.snapshot_recall import (
    STATUS_HIT,
    STATUS_MOVED,
    STATUS_PENDING,
    SnapshotListTokenError,
    SnapshotRecallService,
)


@pytest.fixture
def service() -> SnapshotRecallService:
    return SnapshotRecallService()


def _catalog_ids(payload: dict) -> list[str]:
    return [item["partition_code"] for item in payload["items"]]


class TestCatalogFiltering:
    """可召回清单：日期、宽表、可读层级筛选与稳定翻页。"""

    def test_filter_excludes_other_table_same_day(self, service: SnapshotRecallService) -> None:
        payload = service.list_recallable(partition_date="2026-11-18", target_wide_table="WT_ORDER")
        assert _catalog_ids(payload) == ["SNAP-0001", "SNAP-0002"]
        # 另一张表同日的 P3 被排除，前一天的 P4 也被排除。
        assert payload["total"] == 2

    def test_two_pages_are_stable_with_token(self, service: SnapshotRecallService) -> None:
        first = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", page=1, size=1,
        )
        assert _catalog_ids(first) == ["SNAP-0001"]
        assert first["has_more"] is True

        second = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER",
            page=2, size=1, list_token=first["list_token"],
        )
        assert _catalog_ids(second) == ["SNAP-0002"]
        assert second["has_more"] is False

    def test_catalog_item_carries_recall_basis(self, service: SnapshotRecallService) -> None:
        payload = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER",
            readable_tier="冷存储", page=1, size=1,
        )
        p1 = payload["items"][0]
        assert p1["partition_code"] == "SNAP-0001"
        assert p1["readable_tier"] == "冷存储"
        assert p1["position"].startswith("cold://")
        assert p1["version"] == 2
        assert p1["digest"] == "SHA256-P1-V2"
        assert p1["list_position"] == 1
        assert p1["verifiable"] is True

    def test_readable_tier_filter_uses_real_position(self, service: SnapshotRecallService) -> None:
        hot = service.list_recallable(readable_tier="热存储")
        assert _catalog_ids(hot) == ["SNAP-0001", "SNAP-0002"]
        assert all(item["position"].startswith("hot://") for item in hot["items"])

        cold = service.list_recallable(readable_tier="冷存储")
        assert "SNAP-0003" in _catalog_ids(cold)
        archive = service.list_recallable(readable_tier="归档")
        assert archive["items"] == []

    def test_unknown_token_is_rejected(self, service: SnapshotRecallService) -> None:
        with pytest.raises(SnapshotListTokenError):
            service.list_recallable(page=2, list_token="RT-missing")

    def test_token_rejects_changed_filters(self, service: SnapshotRecallService) -> None:
        first = service.list_recallable(target_wide_table="WT_ORDER")
        with pytest.raises(SnapshotListTokenError):
            service.list_recallable(target_wide_table="WT_ITEM", list_token=first["list_token"])

    def test_pagination_survives_archive_during_turn(self, service: SnapshotRecallService) -> None:
        first = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", page=1, size=1,
        )
        service.archive_partition(partition_code="SNAP-0001", from_tier="冷存储")
        second = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER",
            page=2, size=1, list_token=first["list_token"],
        )
        # 旧依据冻结后，第 2 页仍然是 P2；按层级重新筛选时才会看到 P1 的新位置。
        assert _catalog_ids(second) == ["SNAP-0002"]


class TestSelectionCapture:
    """选择封存：记录分区身份、可读层级和版本。"""

    def test_capture_records_identity_tier_and_version(self, service: SnapshotRecallService) -> None:
        page = service.list_recallable(partition_date="2026-11-18", target_wide_table="WT_ORDER")
        selection = service.capture_selections(page["items"])[0]

        assert selection["partition_id"] == "LP-WT_ORDER-20261118-SNAP-0001"
        assert selection["partition_code"] == "SNAP-0001"
        assert selection["selected_tier"] == "冷存储"
        assert selection["selected_position"].startswith("cold://")
        assert selection["selected_version"] == 2
        assert selection["selected_digest"] == "SHA256-P1-V2"
        assert selection["selected_list_position"] == 1
        assert selection["basis_recorded"] is True

        stored = service.get_selection(selection["selection_id"])
        assert stored is not None
        assert stored["identity"]["partition_id"] == selection["partition_id"]

    def test_capture_requires_position(self, service: SnapshotRecallService) -> None:
        selection = service.capture_selections([{
            "partition_id": "LP-WT_ORDER-20261118-SNAP-0001",
            "selected_tier": "冷存储",
            "selected_version": 2,
        }])[0]
        assert selection["basis_recorded"] is False


class TestRecallByPosition:
    """召回：命中、归档冲突、位次漂移与待核。"""

    def test_recall_hits_selected_position_and_reads_11_rows(
        self, service: SnapshotRecallService,
    ) -> None:
        page = service.list_recallable(partition_date="2026-11-18", target_wide_table="WT_ORDER")
        selection = service.capture_selections(page["items"])[0]

        result = service.recall(selection_ids=[selection["selection_id"]])
        item = result["results"][0]
        assert item["status"] == STATUS_HIT
        assert item["actual_tier"] == "冷存储"
        assert item["actual_position"] == selection["selected_position"]
        assert item["actual_version"] == 2
        assert item["detail_row_count"] == 11
        assert len(item["rows"]) == 11
        assert all(row["读取位置"] == selection["selected_position"] for row in item["rows"])

    def test_archive_conflicts_with_old_tier_but_keeps_logical_partition(
        self, service: SnapshotRecallService,
    ) -> None:
        page = service.list_recallable(partition_date="2026-11-18", target_wide_table="WT_ORDER")
        selection = service.capture_selections([page["items"][0]])[0]
        archive = service.archive_partition(partition_code="SNAP-0001", from_tier="冷存储")
        assert archive["ok"] is True
        assert archive["old_position"] == selection["selected_position"]

        result = service.recall(selection_ids=[selection["selection_id"]])
        item = result["results"][0]
        assert item["status"] == STATUS_MOVED
        assert item["partition_id"] == selection["partition_id"]
        assert item["selected_tier"] == "冷存储"
        assert item["actual_tier"] == "归档"
        assert item["actual_position"] == archive["new_position"]
        assert item["conflict"]["tier_changed"] is True
        assert item["conflict"]["position_changed"] is True
        assert item["detail_row_count"] == 11
        assert all(row["读取位置"] == archive["new_position"] for row in item["rows"])

        # 重新筛选归档结果后，P1 的当前位次也可以核对。
        archived_catalog = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", readable_tier="归档",
        )
        assert _catalog_ids(archived_catalog) == ["SNAP-0001"]
        assert archived_catalog["items"][0]["position"] == archive["new_position"]

    def test_archive_without_digest_returns_pending_reason(
        self, service: SnapshotRecallService,
    ) -> None:
        page = service.list_recallable(partition_date="2026-11-18", target_wide_table="WT_ORDER")
        selection = service.capture_selections([page["items"][0]])[0]
        service.archive_partition(partition_code="SNAP-0001", from_tier="冷存储", verifiable=False)

        item = service.recall(selection_ids=[selection["selection_id"]])["results"][0]
        assert item["status"] == STATUS_PENDING
        assert "校验摘要" in item["pending_reason"]
        assert item["rows"] == []
        assert item["actual_position"] is None

    def test_list_position_can_shift_while_recall_follows_identity(
        self, service: SnapshotRecallService,
    ) -> None:
        page = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", page=1, size=1,
        )
        selection = service.capture_selections(page["items"])[0]
        service.archive_partition(partition_code="SNAP-0001", from_tier="冷存储")

        # 归档后按冷层筛选，P1 已经不在冷层清单里，P2 从第 2 位升到第 1 位。
        cold_now = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", readable_tier="冷存储",
        )
        assert _catalog_ids(cold_now) == ["SNAP-0002"]
        assert cold_now["items"][0]["list_position"] == 1

        item = service.recall(selection_ids=[selection["selection_id"]])["results"][0]
        assert item["partition_code"] == "SNAP-0001"
        assert item["status"] == STATUS_MOVED
        assert item["current_list_position"] is None  # 冷层清单里已经没有它
        archived_now = service.list_recallable(readable_tier="归档")
        assert archived_now["items"][0]["partition_id"] == selection["partition_id"]

    def test_both_pages_submitted_together_stay_on_own_partitions(
        self, service: SnapshotRecallService,
    ) -> None:
        first = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", page=1, size=1,
        )
        second = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER",
            page=2, size=1, list_token=first["list_token"],
        )
        selections = service.capture_selections(first["items"] + second["items"])
        assert [s["partition_code"] for s in selections] == ["SNAP-0001", "SNAP-0002"]

        service.archive_partition(partition_code="SNAP-0001", from_tier="冷存储")
        result = service.recall(selection_ids=[s["selection_id"] for s in selections])
        by_code = {item["partition_code"]: item for item in result["results"]}
        assert by_code["SNAP-0001"]["status"] == STATUS_MOVED
        assert by_code["SNAP-0002"]["status"] == STATUS_HIT
        assert by_code["SNAP-0002"]["actual_position"].startswith("cold://")
        assert by_code["SNAP-0002"]["actual_list_position"] == 1

    def test_position_reused_by_other_partition_is_pending(
        self, service: SnapshotRecallService,
    ) -> None:
        page = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", page=1, size=1,
        )
        selection = service.capture_selections([page["items"][0]])[0]
        # 模拟目录重建：P1 的旧位置被 P2 占用，旧位次不再属于 P1。
        p1 = service._find_partition(partition_code="SNAP-0001")
        p2 = service._find_partition(partition_code="SNAP-0002")
        assert p1 is not None and p2 is not None
        p1.replicas[1].position = "cold://cn-east/warehouse/cold-07/20261118/SNAP-0001-moved"
        p2.replicas[0].position = selection["selected_position"]

        item = service.recall(selection_ids=[selection["selection_id"]])["results"][0]
        assert item["status"] == STATUS_PENDING
        assert "不能按旧位次读取" in item["pending_reason"]

    def test_inline_selection_without_digest_is_pending(
        self, service: SnapshotRecallService,
    ) -> None:
        page = service.list_recallable(
            partition_date="2026-11-18", target_wide_table="WT_ORDER", page=1, size=1,
        )
        raw = {**page["items"][0]}
        raw.pop("digest")
        service.archive_partition(partition_code="SNAP-0001", from_tier="冷存储")

        item = service.recall(selections=[raw])["results"][0]
        assert item["status"] == STATUS_PENDING
        assert "摘要" in item["pending_reason"]

    def test_unknown_selection_id_is_pending(self, service: SnapshotRecallService) -> None:
        item = service.recall(selection_ids=["SEL-not-exist"])["results"][0]
        assert item["status"] == STATUS_PENDING
        assert "选择记录不存在" in item["pending_reason"]


class TestDetailRead:
    """按分区与层级读取明细。"""

    def test_detail_reads_selected_tier(self, service: SnapshotRecallService) -> None:
        detail = service.read_detail(partition_code="SNAP-0001", readable_tier="冷存储")
        assert detail["ok"] is True
        assert detail["position"].startswith("cold://")
        assert detail["detail_row_count"] == 11
        assert detail["rows"][0]["读取位置"] == detail["position"]

    def test_detail_after_archive_only_readable_at_new_position(
        self, service: SnapshotRecallService,
    ) -> None:
        service.archive_partition(partition_code="SNAP-0001", from_tier="冷存储")
        cold = service.read_detail(partition_code="SNAP-0001", readable_tier="冷存储")
        assert cold["ok"] is False
        archived = service.read_detail(partition_code="SNAP-0001", readable_tier="归档")
        assert archived["ok"] is True
        assert archived["position"].startswith("archive://")
