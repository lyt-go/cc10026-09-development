"""账期更正接口：更正申请、明细核对与结算版本查询。

通用资源路由只覆盖列表/登记/动作；这里补充更正申请这类需要逐条来源明细
和按版本回查原结算凭证的场景。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services import service_for

router = APIRouter(prefix="/api", tags=["账期更正"])


class CorrectionLinePayload(BaseModel):
    source_call_id: str
    call_count: int = 0
    data_rows: int = 0
    amount: float = 0.0
    caller: str = ""
    api_name: str = ""


class CorrectionApplyPayload(BaseModel):
    lines: list[CorrectionLinePayload] = Field(default_factory=list)
    original_bill_no: str | None = None
    billing_cycle: str | None = None
    counterparty: str = ""
    correction_type: str = "迟调用补录"
    idempotency_key: str | None = None


class NotifyPayload(BaseModel):
    remark: str = ""


def _correction_service():
    service = service_for("billing_correction")
    if service is None:
        raise HTTPException(status_code=404, detail="账期更正服务未注册")
    return service


@router.post("/billing_correction/apply")
def apply_correction(payload: CorrectionApplyPayload) -> dict[str, Any]:
    service = _correction_service()
    entry, message = service.apply_correction(
        [line.model_dump() for line in payload.lines],
        original_bill_no=payload.original_bill_no,
        billing_cycle=payload.billing_cycle,
        counterparty=payload.counterparty,
        correction_type=payload.correction_type,
        idempotency_key=payload.idempotency_key,
    )
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.get("/billing_correction/{correction_id}/detail")
def correction_detail(correction_id: int) -> dict[str, Any]:
    service = _correction_service()
    detail = service.correction_detail(correction_id)
    if not detail:
        raise HTTPException(status_code=404, detail=f"账期更正明细 {correction_id} 不存在")
    return detail


@router.post("/billing_correction/{correction_id}/notify")
def notify_counterparty(correction_id: int, payload: NotifyPayload) -> dict[str, Any]:
    service = _correction_service()
    entry, message = service.notify_counterparty(correction_id, payload.remark)
    return {"ok": entry is not None, "message": message, "entry": entry}


@router.post("/usage_billing/settlements/{billing_cycle}/settle")
def settle_cycle(billing_cycle: str, idempotency_key: str | None = None) -> dict[str, Any]:
    """从结算侧发起账期结算；同一幂等键重放不会重复出账。"""
    service = service_for("usage_billing")
    if service is None:
        raise HTTPException(status_code=404, detail="用量计量服务未注册")
    voucher = service.settle_cycle(
        billing_cycle, idempotency_key=idempotency_key)
    return {
        "ok": True,
        "replayed": voucher.get("replayed", False),
        "voucher": voucher,
    }


@router.get("/usage_billing/settlements/{billing_cycle}/versions")
def settlement_versions(billing_cycle: str) -> dict[str, Any]:
    from app.services.data_manager import read_settlement_record

    record = read_settlement_record(billing_cycle)
    return {
        "billing_cycle": billing_cycle,
        "current_version": record.get("current_version", 0),
        "reopened": record.get("reopened", False),
        "reopen_corrections": record.get("reopen_corrections", []),
        "versions": record.get("versions", []),
    }


@router.get("/usage_billing/settlements/{billing_cycle}/versions/{version}")
def settlement_voucher(billing_cycle: str, version: int) -> dict[str, Any]:
    from app.services.data_manager import get_settlement_voucher

    voucher = get_settlement_voucher(billing_cycle, version)
    if not voucher:
        raise HTTPException(
            status_code=404,
            detail=f"账期 {billing_cycle} 的结算版本 {version} 不存在")
    return voucher


@router.get("/usage_billing/settlements/{billing_cycle}/reconciliation")
def reconciliation(
    billing_cycle: str,
    correction_id: int | None = Query(default=None),
) -> dict[str, Any]:
    """对账视图：逐版本差额与通知记录，用于复核实际应收。"""
    from app.services.data_manager import (
        list_reconciliation_notices, read_settlement_record,
    )

    record = read_settlement_record(billing_cycle)
    versions = record.get("versions", [])
    notices = list_reconciliation_notices(
        correction_id=correction_id, billing_cycle=billing_cycle)
    return {
        "billing_cycle": billing_cycle,
        "reopened": record.get("reopened", False),
        "reopen_corrections": record.get("reopen_corrections", []),
        "versions": [
            {
                "version": v.get("version"),
                "settlement_no": v.get("settlement_no"),
                "kind": v.get("kind"),
                "previous_total_amount": v.get("previous_total_amount"),
                "incremental_amount": v.get("incremental_amount"),
                "total_amount": v.get("total_amount"),
                "correction_id": v.get("correction_id"),
                "settled_at": v.get("settled_at"),
            }
            for v in versions
        ],
        "actual_receivable": versions[-1].get("total_amount") if versions else 0,
        "notices": notices,
    }
