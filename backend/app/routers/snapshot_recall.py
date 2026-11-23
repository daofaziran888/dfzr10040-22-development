"""快照副本召回接口：可召回清单、选择封存、按位置召回、归档核对与明细读取。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.snapshot_recall import (
    SnapshotListTokenError,
    snapshot_recall_service,
)

router = APIRouter(prefix="/api/snapshot-recall", tags=["快照副本召回"])


class SelectionPayload(BaseModel):
    items: list[dict[str, Any]] = Field(default_factory=list)


class RecallPayload(BaseModel):
    selection_ids: list[str] = Field(default_factory=list)
    selections: list[dict[str, Any]] = Field(default_factory=list)
    detail_size: int = 11


class ArchivePayload(BaseModel):
    partition_id: str | None = None
    partition_code: str | None = None
    from_tier: str = "冷存储"
    verifiable: bool = True


@router.get("/catalog")
def list_catalog(
    partition_date: str | None = Query(default=None),
    target_wide_table: str | None = Query(default=None),
    readable_tier: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
    list_token: str | None = Query(default=None),
) -> dict[str, Any]:
    """按日期、宽表和可读层级筛选可召回分区；翻页时带 list_token 保持位次稳定。"""
    try:
        return snapshot_recall_service.list_recallable(
            partition_date=partition_date,
            target_wide_table=target_wide_table,
            readable_tier=readable_tier,
            page=page,
            size=size,
            list_token=list_token,
        )
    except SnapshotListTokenError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error


@router.post("/selections")
def capture_selections(payload: SelectionPayload) -> dict[str, Any]:
    """封存选择：记录选中时的分区身份、可读层级、位置和版本。"""
    selections = snapshot_recall_service.capture_selections(payload.items)
    return {"ok": True, "count": len(selections), "selections": selections}


@router.get("/selections/{selection_id}")
def get_selection(selection_id: str) -> dict[str, Any]:
    selection = snapshot_recall_service.get_selection(selection_id)
    if selection is None:
        raise HTTPException(status_code=404, detail=f"选择记录 {selection_id} 不存在")
    return selection


@router.post("/recall")
def recall(payload: RecallPayload) -> dict[str, Any]:
    """按选择记录召回：按位置取副本并读取 11 行明细，必要时给出待核原因。"""
    return snapshot_recall_service.recall(
        selection_ids=payload.selection_ids,
        selections=payload.selections,
        detail_size=payload.detail_size,
    )


@router.post("/archive")
def archive(payload: ArchivePayload) -> dict[str, Any]:
    """把指定层级的副本归档到新位置，旧位置停止可读。"""
    return snapshot_recall_service.archive_partition(
        partition_id=payload.partition_id,
        partition_code=payload.partition_code,
        from_tier=payload.from_tier,
        verifiable=payload.verifiable,
    )


@router.get("/detail")
def read_detail(
    partition_code: str = Query(...),
    readable_tier: str | None = Query(default=None),
    size: int = Query(default=11, ge=1, le=200),
) -> dict[str, Any]:
    """按分区与层级读取明细，用于核对实际读取位置。"""
    result = snapshot_recall_service.read_detail(
        partition_code=partition_code, readable_tier=readable_tier, size=size,
    )
    if not result.get("ok"):
        raise HTTPException(status_code=404, detail=result.get("message", "副本不存在"))
    return result
