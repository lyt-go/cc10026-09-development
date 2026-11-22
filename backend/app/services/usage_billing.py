"""用量计量领域服务：管理API调用用量计量和计费周期结算。

跨模块数据流：
- 消费 api_registry 共享快照 → 核验计费API
- 消费 subscription 共享快照 → 审批通过后初始化用量
- 消费 rate_limiter 快照 → 超限写入用量异常
- 写入 data/usage/ 共享文件 → call_audit 消费审计
- 计费周期结束后触发结算 → 按版本写入结算凭证
- 账期更正明细（billing_correction）→ 只对修订部分追加结算版本
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class UsageBillingService(BaseModuleService):
    MODULE = "usage_billing"
    ENTITY = "用量记录"
    OPERATION_TERM = "校验"
    LIST_FIELDS = ["记录编号", "调用方", "API名称", "调用次数", "数据行数", "计费周期", "费用金额", "结算状态"]
    REQUIRED_FIELDS = ["记录编号", "调用方", "API名称"]
    STATUS_ORDER = ["计量中", "已出账", "已确认", "已结算"]
    ACTION_RULES = {"确认出账": "已出账", "标记确认": "已确认", "完成结算": "已结算"}
    NEGATIVE_ACTIONS = []

    def record_usage(self, api_name: str, caller: str, call_count: int = 1,
                     data_rows: int = 0, amount: float = 0.0,
                     source_call_id: str | None = None) -> dict[str, Any]:
        """记录一次API调用用量。

        `source_call_id` 给定时按来源调用去重：同一来源调用只累计一次，
        避免迟到的调用明细在重放时被重复计费。
        """
        from app.services.data_manager import write_usage
        from app.store import store as s

        rows = s.rows(self.MODULE)
        # 查找已有记录
        billing_cycle = self._current_billing_cycle()
        for row in rows:
            if (row.get("API名称") == api_name and row.get("调用方") == caller
                    and row.get("计费周期") == billing_cycle):
                if source_call_id and source_call_id in (
                        row.get("source_call_ids") or []):
                    return row
                old_count = int(row.get("调用次数", "0") or 0)
                old_rows = int(row.get("数据行数", "0") or 0)
                old_amount = float(row.get("费用金额", 0) or 0)
                row["调用次数"] = str(old_count + call_count)
                row["数据行数"] = str(old_rows + data_rows)
                row["费用金额"] = old_amount + amount
                if source_call_id:
                    row.setdefault("source_call_ids", []).append(source_call_id)
                write_usage(row.get("id", 0), {
                    "usage_id": row.get("id"),
                    "record_no": row.get("记录编号"),
                    "caller": caller,
                    "api_name": api_name,
                    "call_count": old_count + call_count,
                    "data_rows": old_rows + data_rows,
                    "amount": old_amount + amount,
                    "billing_cycle": billing_cycle,
                    "settlement_status": row.get("status"),
                    "source_call_ids": row.get("source_call_ids", []),
                })
                return row

        # 新记录
        new_id = max((int(r.get("id", 0)) for r in rows), default=0) + 1
        new_record = {
            "id": new_id,
            "记录编号": f"USAG-{new_id:04d}",
            "调用方": caller,
            "API名称": api_name,
            "调用次数": str(call_count),
            "数据行数": str(data_rows),
            "计费周期": billing_cycle,
            "费用金额": amount,
            "结算状态": self.STATUS_ORDER[0],
            "status": self.STATUS_ORDER[0],
            "pending": True,
            "abnormal": False,
            "source_call_ids": [source_call_id] if source_call_id else [],
        }
        rows.append(new_record)
        write_usage(new_id, {
            "usage_id": new_id,
            "record_no": f"USAG-{new_id:04d}",
            "caller": caller,
            "api_name": api_name,
            "call_count": call_count,
            "data_rows": data_rows,
            "amount": amount,
            "billing_cycle": billing_cycle,
            "settlement_status": self.STATUS_ORDER[0],
            "source_call_ids": [source_call_id] if source_call_id else [],
        })
        return new_record

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None, f"{self.ENTITY} {entry_id} 不存在或已归档"

        result = super().run_action(entry_id, action)

        if action == "完成结算" and entry:
            from app.services.data_manager import settle_billing
            billing_cycle = entry.get("计费周期", self._current_billing_cycle())
            settlement = settle_billing(
                billing_cycle, use_auto_settlement=True,
                idempotency_key=f"usage:{entry.get('id')}:{action}")

            # 联动增强：结算完成写入调用审计记录
            from app.services.data_manager import write_audit, DATA_ROOT
            audit_id = max((int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")), default=0) + 1
            write_audit(audit_id, {
                "audit_id": audit_id,
                "api_name": entry.get("API名称", ""),
                "caller": entry.get("调用方", ""),
                "event": "billing_settled",
                "billing_cycle": billing_cycle,
                "settlement_no": settlement.get("settlement_no"),
                "settlement_version": settlement.get("version"),
                "incremental_amount": settlement.get("incremental_amount", 0),
                "total_amount": settlement.get("total_amount", 0),
                "total_calls": settlement.get("total_calls", 0),
            })

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    def settle_cycle(
        self, billing_cycle: str, *, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        """从用量计量侧发起账期结算；重复请求按幂等键返回已有版本。"""
        from app.services.data_manager import settle_billing
        settlement = settle_billing(
            billing_cycle, use_auto_settlement=True,
            idempotency_key=idempotency_key)
        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return settlement

    def _current_billing_cycle(self) -> str:
        from datetime import datetime, timedelta, timezone
        return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m")

    def domain_snapshot(self) -> dict[str, Any]:
        from app.services.data_manager import (
            list_reconciliation_notices, list_usage, read_settlement_record,
        )

        rows = store.rows(self.MODULE)
        usage_list = []
        for row in rows:
            usage_list.append({
                "id": row.get("id"),
                "record_no": row.get("记录编号"),
                "caller": row.get("调用方"),
                "api_name": row.get("API名称"),
                "call_count": row.get("调用次数"),
                "data_rows": row.get("数据行数"),
                "billing_cycle": row.get("计费周期"),
                "amount": row.get("费用金额"),
                "status": row.get("status"),
            })

        # 结算凭证按账期版本化输出，供账期更正核对和下游对账消费。
        settlements = []
        cycles = sorted({
            r.get("billing_cycle") for r in list_usage() if r.get("billing_cycle")
        })
        for cycle in cycles:
            record = read_settlement_record(cycle)
            versions = record.get("versions", [])
            current = versions[-1] if versions else {}
            settlements.append({
                "billing_cycle": cycle,
                "current_version": record.get("current_version", 0),
                "settlement_no": current.get("settlement_no"),
                "total_amount": current.get("total_amount", 0),
                "reopened": record.get("reopened", False),
                "reopen_corrections": record.get("reopen_corrections", []),
                "version_count": len(versions),
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
            })

        correction_usage = [
            r for r in list_usage() if r.get("correction_id")
        ]
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(usage_list),
            "usages": usage_list,
            "total_amount": sum(float(r.get("amount", 0) or 0) for r in usage_list),
            "settled_count": sum(1 for r in usage_list if r.get("status") == "已结算"),
            "settlements": settlements,
            "correction_usage_count": len(correction_usage),
            "correction_usage_ids": [
                f"USAG-{int(r.get('usage_id', 0)):04d}" for r in correction_usage],
            "notices": len(list_reconciliation_notices()),
        }