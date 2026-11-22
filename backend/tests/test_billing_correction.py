"""账期更正链路测试：确认凭证 → 更正用量 → 重新结算 → 重放 → 对账复核。"""
from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

_temp = tempfile.mkdtemp(prefix="billing_correction_test_")
os.environ["DATA_ROOT"] = _temp

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    import shutil
    from pathlib import Path

    from app.seed import SEED_ROWS
    from app.store import store
    from app.services import data_manager

    # 其他测试模块也会改写 DATA_ROOT，这里显式绑定本文件的临时目录。
    data_manager.DATA_ROOT = Path(_temp)
    store._tables = {name: [dict(row) for row in rows]
                     for name, rows in SEED_ROWS.items()}
    if os.path.exists(_temp):
        shutil.rmtree(_temp, ignore_errors=True)
    os.makedirs(_temp, exist_ok=True)
    yield


def _seed_confirmed_bill():
    """构造一张已确认并被对账方引用的账单。"""
    from app.services.data_manager import settle_billing, write_usage

    write_usage(101, {
        "usage_id": 101, "record_no": "USAG-0101", "caller": "对账方A",
        "api_name": "订单查询API", "call_count": 100, "data_rows": 1000,
        "amount": 100.0, "billing_cycle": "2026-10",
        "settlement_status": "已确认",
    })
    write_usage(102, {
        "usage_id": 102, "record_no": "USAG-0102", "caller": "对账方A",
        "api_name": "订单查询API", "call_count": 50, "data_rows": 500,
        "amount": 50.0, "billing_cycle": "2026-10",
        "settlement_status": "已确认",
    })
    voucher = settle_billing("2026-10", idempotency_key="initial-bill")
    return voucher


def _late_lines():
    return [
        {"source_call_id": "CALL-8801", "call_count": 10, "data_rows": 80,
         "amount": 12.5, "caller": "对账方A", "api_name": "订单查询API"},
        {"source_call_id": "CALL-8802", "call_count": 4, "data_rows": 30,
         "amount": 7.5, "caller": "对账方A", "api_name": "订单查询API"},
    ]


def test_settlement_voucher_is_versioned_and_retrievable():
    from app.services.data_manager import get_settlement_voucher

    voucher = _seed_confirmed_bill()
    assert voucher["version"] == 1
    assert voucher["total_amount"] == 150.0
    first = get_settlement_voucher("2026-10", 1)
    assert first["settlement_no"] == voucher["settlement_no"]
    assert len(first["line_items"]) == 2


def test_correction_links_original_bill_and_source_calls():
    from app.services.data_manager import list_usage
    from app.services.billing_correction import BillingCorrectionService

    voucher = _seed_confirmed_bill()
    svc = BillingCorrectionService()
    entry, message = svc.apply_correction(
        _late_lines(), original_bill_no=voucher["settlement_no"],
        counterparty="对账方A", idempotency_key="corr-1")
    assert entry is not None, message
    assert entry["原账单编号"] == voucher["settlement_no"]
    assert entry["更正金额"] == 20.0
    assert entry["status"] == "已申请"

    booked = [r for r in list_usage("2026-10") if r.get("correction_id") == entry["id"]]
    assert len(booked) == 2
    assert {r["source_call_ids"][0] for r in booked} == {"CALL-8801", "CALL-8802"}
    assert all(r["original_bill_no"] == voucher["settlement_no"] for r in booked)


def test_confirm_regenerates_only_incremental_amount():
    from app.services.data_manager import get_settlement_voucher, list_usage
    from app.services.billing_correction import BillingCorrectionService

    voucher = _seed_confirmed_bill()
    svc = BillingCorrectionService()
    entry, _ = svc.apply_correction(
        _late_lines(), billing_cycle="2026-10", counterparty="对账方A")

    confirmed, message = svc.confirm_correction(entry["id"])
    assert confirmed is not None, message
    assert confirmed["status"] == "已确认"
    assert confirmed["更正金额"] == 20.0
    assert confirmed["原确认金额"] == 150.0
    assert confirmed["_settlement_total"] == 170.0

    v1 = get_settlement_voucher("2026-10", 1)
    v2 = get_settlement_voucher("2026-10", 2)
    assert v1["total_amount"] == 150.0  # 原版本仍可按原样取数
    assert v2["incremental_amount"] == 20.0
    assert v2["total_amount"] == 170.0
    # 先前确认的用量行仍保留，本次修订行单独标记
    kinds = {line["kind"] for line in v2["line_items"]}
    assert kinds == {"usage", "correction"}

    # 原已出账明细 billed_amount 未被重复收取
    original = [r for r in list_usage("2026-10") if not r.get("correction_id")]
    assert all(r["billed_amount"] == r["amount"] for r in original)
    assert sum(line["charge_amount"] for line in v2["line_items"]) == 20.0


def test_resume_after_interruption_is_idempotent():
    from app.services.data_manager import get_settlement_voucher
    from app.services.billing_correction import BillingCorrectionService

    _seed_confirmed_bill()
    svc = BillingCorrectionService()
    entry, _ = svc.apply_correction(_late_lines(), billing_cycle="2026-10")

    interrupted, _ = svc.confirm_correction(entry["id"], interrupt_after_stage=2)
    assert interrupted["status"] == "已中断"
    assert interrupted["_completed_steps"] == ["校验原凭证", "入账更正用量"]

    resumed, message = svc.resume_correction(entry["id"])
    assert resumed is not None, message
    assert resumed["status"] == "已确认"
    assert resumed["_completed_steps"] == [
        "校验原凭证", "入账更正用量", "重新结算", "标记账期重开"]

    versions = [get_settlement_voucher("2026-10", v["version"])
                for v in
                __import__("app.services.data_manager", fromlist=["x"])
                .read_settlement_versions("2026-10")]
    assert len(versions) == 2, "恢复后不应重复生成结算版本"
    assert versions[-1]["incremental_amount"] == 20.0


def test_replayed_apply_and_confirm_do_not_double_charge():
    from app.services.data_manager import read_settlement_versions
    from app.services.billing_correction import BillingCorrectionService

    _seed_confirmed_bill()
    svc = BillingCorrectionService()
    entry, _ = svc.apply_correction(
        _late_lines(), billing_cycle="2026-10", idempotency_key="corr-replay")
    svc.confirm_correction(entry["id"])

    replay, message = svc.apply_correction(
        _late_lines(), billing_cycle="2026-10", idempotency_key="corr-replay")
    assert replay is not None and replay.get("_replayed")
    assert len(svc.list_corrections(billing_cycle="2026-10")) == 1
    again, _ = svc.confirm_correction(entry["id"])
    assert again["status"] == "已确认"
    assert len(read_settlement_versions("2026-10")) == 2


def test_duplicate_source_call_rejected():
    from app.services.billing_correction import BillingCorrectionService

    _seed_confirmed_bill()
    svc = BillingCorrectionService()
    svc.apply_correction(_late_lines(), billing_cycle="2026-10")
    _, message = svc.apply_correction(
        [{"source_call_id": "CALL-8801", "amount": 1.0}],
        billing_cycle="2026-10")
    assert "已入账" in message


def test_notify_tracks_reopen_and_is_idempotent():
    from app.services.data_manager import (
        list_reconciliation_notices, read_settlement_record,
    )
    from app.services.billing_correction import BillingCorrectionService

    _seed_confirmed_bill()
    svc = BillingCorrectionService()
    entry, _ = svc.apply_correction(
        _late_lines(), billing_cycle="2026-10", counterparty="对账方A")
    svc.confirm_correction(entry["id"])

    notified, message = svc.notify_counterparty(entry["id"], "补发对账结果")
    assert notified is not None, message
    assert notified["status"] == "已通知"
    assert notified["重开追踪"] == "需重开账期"
    assert read_settlement_record("2026-10")["reopened"] is True
    assert len(list_reconciliation_notices(correction_id=entry["id"])) == 1

    replay, message = svc.notify_counterparty(entry["id"])
    assert replay["status"] == "已通知"
    assert len(list_reconciliation_notices(correction_id=entry["id"])) == 1


def test_two_settlement_records_reconcile_actual_difference():
    """先发账单补两条明细后重新结算，重放请求并复核实际应收差额。"""
    from app.services.data_manager import (
        read_settlement_versions, settle_billing,
    )
    from app.services.billing_correction import BillingCorrectionService

    original = _seed_confirmed_bill()
    svc = BillingCorrectionService()
    entry, _ = svc.apply_correction(
        _late_lines(), original_bill_no=original["settlement_no"],
        billing_cycle="2026-10")

    # 重放一次确认请求：不应产生额外版本
    first, _ = svc.confirm_correction(entry["id"])
    replay, _ = svc.confirm_correction(entry["id"])
    assert replay["status"] == "已确认"

    # 结算侧再重放一次同幂等键
    replay_settlement = settle_billing(
        "2026-10", idempotency_key=f"correction:{entry['_idempotency_key'] or entry['更正编号']}:settle")
    assert replay_settlement["replayed"] is True

    versions = read_settlement_versions("2026-10")
    assert len(versions) == 2
    v1, v2 = versions
    assert v1["total_amount"] == 150.0
    assert v2["total_amount"] == 170.0
    assert round(v2["total_amount"] - v1["total_amount"], 2) == 20.0
    assert first["结算编号"] == v2["settlement_no"]


def test_correction_survives_memory_reset_from_persisted_record():
    """模拟进程中断：内存明细丢失后仍可从共享记录恢复并完成更正。"""
    from app.services.data_manager import read_settlement_versions
    from app.services.billing_correction import BillingCorrectionService

    _seed_confirmed_bill()
    svc = BillingCorrectionService()
    entry, _ = svc.apply_correction(_late_lines(), billing_cycle="2026-10")

    # 清空内存中的明细，仅保留持久化记录
    live = entry
    live["_lines"] = []
    live["_source_call_ids"] = []
    live["_completed_steps"] = []
    live["status"] = "已中断"

    resumed, message = svc.resume_correction(live["id"])
    assert resumed is not None, message
    assert resumed["status"] == "已确认"
    assert len(read_settlement_versions("2026-10")) == 2
