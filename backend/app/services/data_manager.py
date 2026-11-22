"""跨模块共享数据管理器：读写 data/ 目录下的业务数据产物。

职责：
- 每个服务产出 domain_snapshot 后序列化到 data/{module}/snapshot.json
- 下游服务从 data/ 目录读取上游的标准化数据产物
- 审计记录写入 data/audit/
- 用量记录写入 data/usage/
- 导出任务结果写入 data/exports/
- API 注册快照写入 data/api/
- 订阅审批记录写入 data/subscriptions/
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# 数据目录相对于仓库根
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(
    Path(__file__).resolve().parents[3] / "data"
)))

# 东八区
TZ = timezone(timedelta(hours=8))


def _ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write_json(path: Path, data: Any) -> None:
    _ensure_dir(path.parent)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str), encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(TZ).isoformat()


# ========== API 注册快照 ==========


def write_api_snapshot(api_id: int, api_data: dict[str, Any]) -> None:
    """将单个 API 注册数据写入共享快照，供路由、缓存、限流等模块消费。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    api_data["_synced_at"] = _now_iso()
    snapshot[key] = api_data
    _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


def read_api_snapshot(api_id: int | None = None) -> dict[str, Any] | dict[str, dict[str, Any]]:
    """读取 API 注册快照。返回单个或全部。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    if api_id is not None:
        return snapshot.get(str(api_id), {})
    return snapshot


def remove_api_snapshot(api_id: int) -> None:
    """标记 API 为下线。"""
    snapshot = _read_json(DATA_ROOT / "api" / "registry.json", {})
    key = str(api_id)
    if key in snapshot:
        snapshot[key]["status"] = "已下线"
        snapshot[key]["_deleted_at"] = _now_iso()
        _write_json(DATA_ROOT / "api" / "registry.json", snapshot)


# ========== 订阅审批 ==========


def write_subscription(sub_id: int, sub_data: dict[str, Any]) -> None:
    """写入订阅申请记录到共享目录。"""
    record = {
        "subscription_id": sub_id,
        **sub_data,
        "_created_at": sub_data.get("_created_at", _now_iso()),
        "_updated_at": _now_iso(),
    }
    _write_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", record)


def read_subscription(sub_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "subscriptions" / f"{sub_id:06d}.json", {})


def list_subscriptions() -> list[dict[str, Any]]:
    """列出所有订阅记录。"""
    _ensure_dir(DATA_ROOT / "subscriptions")
    records = []
    for f in sorted(DATA_ROOT.glob("subscriptions/*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


def approve_subscription(sub_id: int, approved: bool, approver: str = "system") -> dict[str, Any]:
    """审批订阅，记录审批流程。"""
    record = read_subscription(sub_id)
    if not record:
        return {}
    record["approval_status"] = "已通过" if approved else "已驳回"
    record["approver"] = approver
    record["_approved_at"] = _now_iso()
    write_subscription(sub_id, record)
    return record


# ========== 用量计费 ==========


def _to_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def write_usage(usage_id: int, usage_data: dict[str, Any]) -> None:
    """写入用量记录到共享目录。"""
    usage_data["_created_at"] = usage_data.get("_created_at", _now_iso())
    usage_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", usage_data)


def read_usage(usage_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "usage" / f"{usage_id:06d}.json", {})


def list_usage(billing_cycle: str | None = None) -> list[dict[str, Any]]:
    """列出用量记录，支持按计费周期过滤。

    `usage_id` 以文件名回填，避免调用方漏带上游主键时写错文件。
    """
    _ensure_dir(DATA_ROOT / "usage")
    records = []
    for f in sorted(DATA_ROOT.glob("usage/[0-9]*.json")):
        record = _read_json(f)
        if record:
            record.setdefault("usage_id", _to_int(f.stem))
            if billing_cycle is None or record.get("billing_cycle") == billing_cycle:
                records.append(record)
    return records


# ---------- 结算凭证（账期汇总按版本追加，可回查原版本） ----------


def _settlement_path(billing_cycle: str) -> Path:
    return DATA_ROOT / "usage" / "settlements" / f"{billing_cycle}.json"


def read_settlement_record(billing_cycle: str) -> dict[str, Any]:
    """读取账期的结算版本档案；兼容历史单文件汇总。"""
    data = _read_json(_settlement_path(billing_cycle))
    if data:
        data.setdefault("billing_cycle", billing_cycle)
        data.setdefault("versions", [])
        data.setdefault("current_version", len(data["versions"]))
        return data

    legacy = _read_json(DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json")
    if legacy:
        legacy.setdefault("version", 1)
        legacy.setdefault("settlement_id", 1)
        legacy.setdefault("settlement_no", f"STL-{billing_cycle.replace('-', '')}-0001")
        legacy.setdefault("line_items", [])
        return {
            "billing_cycle": billing_cycle,
            "versions": [legacy],
            "current_version": legacy["version"],
            "reopened": False,
            "reopen_corrections": [],
        }
    return {
        "billing_cycle": billing_cycle,
        "versions": [],
        "current_version": 0,
        "reopened": False,
        "reopen_corrections": [],
    }


def read_settlement_versions(billing_cycle: str) -> list[dict[str, Any]]:
    return list(read_settlement_record(billing_cycle).get("versions", []))


def get_settlement_voucher(
    billing_cycle: str, version: int | None = None
) -> dict[str, Any]:
    """按版本取原确认凭证；不传版本时返回当前最新版本。"""
    versions = read_settlement_versions(billing_cycle)
    if not versions:
        return {}
    if version is None:
        return versions[-1]
    for voucher in versions:
        if _to_int(voucher.get("version")) == version:
            return voucher
    return {}


def _next_settlement_id() -> int:
    max_id = 0
    _ensure_dir(DATA_ROOT / "usage" / "settlements")
    for f in DATA_ROOT.glob("usage/settlements/*.json"):
        data = _read_json(f, {})
        for voucher in (data.get("versions", []) if isinstance(data, dict) else []):
            max_id = max(max_id, _to_int(voucher.get("settlement_id")))
    return max_id + 1


def write_settlement_voucher(billing_cycle: str, voucher: dict[str, Any]) -> None:
    """追加一版结算凭证，并同步历史兼容的最新汇总文件。"""
    data = read_settlement_record(billing_cycle)
    data["versions"].append(voucher)
    data["current_version"] = voucher.get("version", len(data["versions"]))
    data["billing_cycle"] = billing_cycle
    _write_json(_settlement_path(billing_cycle), data)
    _write_json(
        DATA_ROOT / "usage" / f"_settlement_{billing_cycle}.json", voucher)


def mark_cycle_reopened(billing_cycle: str, correction_id: int | None = None) -> None:
    """记录账期是否需要重开以及由哪次更正触发。"""
    data = read_settlement_record(billing_cycle)
    data["reopened"] = True
    reopened = data.setdefault("reopen_corrections", [])
    if correction_id is not None and correction_id not in reopened:
        reopened.append(correction_id)
        data["reopen_count"] = len(reopened)
    _write_json(_settlement_path(billing_cycle), data)


def settle_billing(
    billing_cycle: str,
    use_auto_settlement: bool = True,
    *,
    correction_id: int | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """结算指定计费周期，按版本追加结算凭证。

    - 首次结算：汇总账期内全部基础用量。
    - 更正结算：只对本次更正新增/调整的金额计费，原已出账明细的
      `billed_amount` 保持不变，不会重复收费。
    - `idempotency_key` 相同的请求直接返回已有版本，不重复生成凭证。
    """
    record = read_settlement_record(billing_cycle)
    versions = list(record.get("versions", []))

    if idempotency_key:
        for voucher in versions:
            if voucher.get("idempotency_key") == idempotency_key:
                return {**voucher, "replayed": True}

    records = list_usage(billing_cycle)
    previous = versions[-1] if versions else None
    previous_total = _to_float(previous.get("total_amount")) if previous else 0.0
    correction_key = None if correction_id is None else str(correction_id)

    if not records and previous is None and correction_key is None:
        return {
            "billing_cycle": billing_cycle,
            "version": 0,
            "total_amount": 0.0,
            "total_calls": 0,
            "record_count": 0,
            "incremental_amount": 0.0,
            "previous_total_amount": 0.0,
            "line_items": [],
            "empty": True,
            "settled_at": _now_iso(),
        }

    if correction_key is not None:
        covered = [
            r for r in records
            if str(r.get("correction_id")) == correction_key
        ]
        if not covered:
            raise ValueError("更正用量尚未入账，无法重新结算")
    else:
        covered = [r for r in records if not r.get("correction_id")]

    covered_ids = {id(r) for r in covered}

    # 凭证行包含原已确认明细（本次计费 0）与本次更正明细，便于逐行核对。
    included: list[dict[str, Any]] = []
    for r in records:
        cid = r.get("correction_id")
        if cid:
            if correction_key is not None and str(cid) == correction_key:
                included.append(r)
            elif r.get("settlement_id"):
                included.append(r)
        else:
            included.append(r)

    line_items: list[dict[str, Any]] = []
    incremental = 0.0
    total_calls = 0
    for r in included:
        amount = _to_float(r.get("amount"))
        billed = _to_float(r.get("billed_amount"))
        is_covered = id(r) in covered_ids
        charge = round(amount - billed, 2) if is_covered else 0.0
        incremental += charge
        total_calls += _to_int(r.get("call_count"))
        line_items.append({
            "usage_id": r.get("usage_id"),
            "record_no": r.get("record_no"),
            "caller": r.get("caller"),
            "api_name": r.get("api_name"),
            "call_count": _to_int(r.get("call_count")),
            "data_rows": _to_int(r.get("data_rows")),
            "amount": round(amount, 2),
            "previously_billed_amount": round(billed, 2),
            "charge_amount": charge,
            "kind": "correction" if r.get("correction_id") else "usage",
            "correction_id": r.get("correction_id"),
            "original_bill_no": r.get("original_bill_no"),
            "source_call_ids": list(r.get("source_call_ids") or []),
            "settlement_id": r.get("settlement_id"),
        })
    incremental = round(incremental, 2)

    if incremental == 0:
        # 重放或空结算：不新增版本，避免重复出账。
        if correction_key is not None:
            for voucher in reversed(versions):
                if str(voucher.get("correction_id")) == correction_key:
                    return {**voucher, "replayed": True}
        if previous is not None:
            return {**previous, "noop": True, "replayed": True}

    version = len(versions) + 1
    settlement_id = _next_settlement_id()
    settlement_no = f"STL-{billing_cycle.replace('-', '')}-{version:04d}"
    settled_at = _now_iso()
    first = versions[0] if versions else None

    voucher: dict[str, Any] = {
        "settlement_id": settlement_id,
        "settlement_no": settlement_no,
        "billing_cycle": billing_cycle,
        "version": version,
        "kind": "correction" if correction_key is not None else (
            "resettle" if previous else "initial"),
        "original_settlement_id": (
            first.get("settlement_id") if first else settlement_id),
        "original_settlement_no": (
            first.get("settlement_no") if first else settlement_no),
        "previous_settlement_id": previous.get("settlement_id") if previous else None,
        "previous_settlement_no": previous.get("settlement_no") if previous else None,
        "previous_total_amount": round(previous_total, 2),
        "incremental_amount": incremental,
        "total_amount": round(previous_total + incremental, 2),
        "total_calls": total_calls,
        "record_count": len(included),
        "correction_id": correction_id,
        "idempotency_key": idempotency_key,
        "auto_settlement": use_auto_settlement,
        "line_items": line_items,
        "settled_at": settled_at,
    }

    for r in covered:
        r["billed_amount"] = _to_float(r.get("amount"))
        r["settlement_status"] = "已结算"
        r["settlement_id"] = settlement_id
        r["settlement_no"] = settlement_no
        r["settlement_version"] = version
        r["_settled_at"] = settled_at
        usage_id = _to_int(r.get("usage_id"))
        if usage_id:
            write_usage(usage_id, r)

    write_settlement_voucher(billing_cycle, voucher)
    return voucher


# ---------- 账期更正（申请、确认、通知、恢复） ----------


def _correction_path(correction_id: int) -> Path:
    return DATA_ROOT / "usage" / "corrections" / f"{correction_id:06d}.json"


def write_correction_record(correction_id: int, data: dict[str, Any]) -> None:
    data["_created_at"] = data.get("_created_at", _now_iso())
    data["_updated_at"] = _now_iso()
    _write_json(_correction_path(correction_id), data)


def read_correction_record(correction_id: int) -> dict[str, Any]:
    return _read_json(_correction_path(correction_id), {})


def list_correction_records() -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "usage" / "corrections")
    records = []
    for f in sorted(DATA_ROOT.glob("usage/corrections/*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


def _next_correction_id() -> int:
    max_id = 0
    if (DATA_ROOT / "usage" / "corrections").exists():
        for f in DATA_ROOT.glob("usage/corrections/*.json"):
            max_id = max(max_id, _to_int(f.stem))
    return max_id + 1


def write_reconciliation_notice(notice_id: int, data: dict[str, Any]) -> None:
    data["_created_at"] = data.get("_created_at", _now_iso())
    data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "usage" / "notices" / f"{notice_id:06d}.json", data)


def list_reconciliation_notices(
    correction_id: int | None = None,
    billing_cycle: str | None = None,
) -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "usage" / "notices")
    records = []
    for f in sorted(DATA_ROOT.glob("usage/notices/*.json")):
        record = _read_json(f)
        if not record:
            continue
        if correction_id is not None and _to_int(record.get("correction_id")) != correction_id:
            continue
        if billing_cycle is not None and record.get("billing_cycle") != billing_cycle:
            continue
        records.append(record)
    return records


def _next_notice_id() -> int:
    max_id = 0
    if (DATA_ROOT / "usage" / "notices").exists():
        for f in DATA_ROOT.glob("usage/notices/*.json"):
            max_id = max(max_id, _to_int(f.stem))
    return max_id + 1


# ========== 调用审计 ==========


def write_audit(audit_id: int, audit_data: dict[str, Any]) -> None:
    """写入审计记录。"""
    audit_data["_logged_at"] = _now_iso()
    _write_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", audit_data)


def read_audit(audit_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "audit" / f"{audit_id:06d}.json", {})


def list_audit(api_name: str | None = None, caller: str | None = None,
               start_time: str | None = None, end_time: str | None = None) -> list[dict[str, Any]]:
    """按条件查询审计记录。"""
    _ensure_dir(DATA_ROOT / "audit")
    records = []
    for f in sorted(DATA_ROOT.glob("audit/*.json")):
        record = _read_json(f)
        if not record:
            continue
        if api_name and record.get("api_name") != api_name:
            continue
        if caller and record.get("caller") != caller:
            continue
        if start_time and record.get("_logged_at", "") < start_time:
            continue
        if end_time and record.get("_logged_at", "") > end_time:
            continue
        records.append(record)
    return records


def archive_audit_records(before_date: str) -> dict[str, Any]:
    """归档指定日期之前的审计记录。"""
    records = list_audit()
    archived = [r for r in records if r.get("_logged_at", "")[:10] < before_date]
    archive_data = {
        "archive_date": _now_iso(),
        "before_date": before_date,
        "archived_count": len(archived),
        "records": archived,
    }
    _write_json(DATA_ROOT / "audit" / f"_archive_{before_date}.json", archive_data)
    # 标记已归档
    for r in archived:
        rid = r.get("audit_id", 0)
        r["status"] = "已归档"
        write_audit(rid, r)
    return archive_data


# ========== 导出任务 ==========


def write_export(export_id: int, export_data: dict[str, Any]) -> None:
    """写入导出任务结果。"""
    export_data["_created_at"] = export_data.get("_created_at", _now_iso())
    export_data["_updated_at"] = _now_iso()
    _write_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", export_data)
    # 同时生成一份导出 CSV
    _write_export_csv(export_id, export_data)


def _write_export_csv(export_id: int, export_data: dict[str, Any]) -> None:
    """根据导出数据生成 CSV 文件头。"""
    columns = export_data.get("columns", [])
    rows = export_data.get("rows", [])
    csv_path = DATA_ROOT / "exports" / f"{export_id:06d}.csv"
    lines = [",".join(columns)]
    for row in rows:
        lines.append(",".join(str(v) for v in row))
    csv_path.write_text("\n".join(lines), encoding="utf-8")


def read_export(export_id: int) -> dict[str, Any]:
    return _read_json(DATA_ROOT / "exports" / f"{export_id:06d}.json", {})


def list_exports() -> list[dict[str, Any]]:
    _ensure_dir(DATA_ROOT / "exports")
    records = []
    for f in sorted(DATA_ROOT.glob("exports/[0-9]*.json")):
        record = _read_json(f)
        if record:
            records.append(record)
    return records


# ========== 模块数据快照 ==========


def write_module_snapshot(module: str, snapshot: dict[str, Any]) -> None:
    """各服务将自己的 domain_snapshot 写入共享目录，供跨模块消费。"""
    snapshot["_snapshot_at"] = _now_iso()
    _write_json(DATA_ROOT / module / "snapshot.json", snapshot)


def read_module_snapshot(module: str) -> dict[str, Any]:
    return _read_json(DATA_ROOT / module / "snapshot.json", {})


def list_module_snapshots() -> list[dict[str, Any]]:
    """读取所有模块的快照汇总。"""
    snapshots = []
    for module_dir in DATA_ROOT.iterdir():
        if module_dir.is_dir() and not module_dir.name.startswith("_"):
            snap = _read_json(module_dir / "snapshot.json")
            if snap:
                snapshots.append(snap)
    return snapshots