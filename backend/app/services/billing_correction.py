"""账期更正领域服务：已确认结算凭证的更正用量登记、重新聚合与对账通知。

业务链路：
- 原结算凭证按 `settlement_no` / `settlement_id` 版本化保存，可按原版本回查
- 更正用量逐条关联「原账单 + 来源调用」，同一来源调用只入账一次
- 状态流转：草稿 → 已申请 → 确认中 → 已确认 → 已通知；可中断后从断点恢复
- 确认时只对本次更正新增的金额出账，原已出账明细的 `billed_amount` 不变
- 重新结算生成新版本金额凭据，旧版本仍可核对
- 通知对账方后记录是否触发账期重开，供财务追踪
"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store

# 确认更正时的可恢复步骤；持久化到记录的 `_completed_steps` 中。
CORRECTION_STEPS = ["校验原凭证", "入账更正用量", "重新结算", "标记账期重开"]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


class BillingCorrectionService(BaseModuleService):
    MODULE = "billing_correction"
    ENTITY = "账期更正明细"
    OPERATION_TERM = "核对"
    LIST_FIELDS = [
        "更正编号", "原账单编号", "账期", "对账方", "更正类型",
        "原确认金额", "更正金额", "重开追踪",
    ]
    REQUIRED_FIELDS = ["原账单编号", "账期"]
    STATUS_ORDER = ["草稿", "已申请", "确认中", "已确认", "已通知", "已中断"]
    ACTION_RULES = {
        "提交申请": "已申请",
        "确认更正": "已确认",
        "恢复更正": "已确认",
        "中断更正": "已中断",
        "通知对账方": "已通知",
    }
    NEGATIVE_ACTIONS = ["中断更正"]

    # ---------- 登记 ----------

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        entry["更正编号"] = values.get("更正编号") or f"CORR-{entry['id']:04d}"
        entry["原确认金额"] = _as_float(values.get("原确认金额"))
        entry["更正金额"] = _as_float(values.get("更正金额"))
        entry["重开追踪"] = entry.get("重开追踪") or "待确认"
        entry["_completed_steps"] = []
        entry["_lines"] = self._parse_lines(values.get("来源明细"))
        entry["_source_call_ids"] = [
            line["source_call_id"] for line in entry["_lines"]]
        entry["_idempotency_key"] = values.get("幂等键")
        entry["_correction_type"] = values.get("更正类型") or "迟调用补录"
        self._persist(entry)
        return entry, []

    def apply_correction(
        self,
        lines: list[dict[str, Any]],
        *,
        original_bill_no: str | None = None,
        billing_cycle: str | None = None,
        counterparty: str = "",
        correction_type: str = "迟调用补录",
        idempotency_key: str | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """为已确认结算凭证登记更正用量。

        每条来源明细都会写入用量计量的共享目录，并关联原账单编号与来源调用；
        同一 `idempotency_key` 重放时直接返回原更正单，不会重复入账。
        """
        if idempotency_key:
            for row in store.rows(self.MODULE):
                if row.get("_idempotency_key") == idempotency_key:
                    row["_replayed"] = True
                    return row, "更正申请已存在，按幂等键返回原记录"

        cycle, voucher = self._resolve_voucher(
            original_bill_no=original_bill_no, billing_cycle=billing_cycle)
        if not voucher:
            return None, "原结算凭证不存在或尚未确认，无法登记更正用量"

        normalized, error = self._normalize_lines(lines)
        if error:
            return None, error
        if not normalized:
            return None, "至少需要一条来源调用明细才能申请账期更正"

        duplicate = self._duplicate_source_calls(
            cycle, [line["source_call_id"] for line in normalized])
        if duplicate:
            return None, f"来源调用已入账，不能重复更正：{'、'.join(duplicate)}"

        rows = store.rows(self.MODULE)
        new_id = max((_as_int(r.get("id")) for r in rows), default=0) + 1
        correction_amount = round(
            sum(line["amount"] for line in normalized), 2)
        entry: dict[str, Any] = {
            "id": new_id,
            "更正编号": f"CORR-{new_id:04d}",
            "原账单编号": voucher.get("settlement_no", original_bill_no or ""),
            "账期": cycle,
            "对账方": counterparty or voucher.get("counterparty", "对账方"),
            "更正类型": correction_type,
            "原确认金额": round(_as_float(voucher.get("total_amount")), 2),
            "更正金额": correction_amount,
            "重开追踪": "待确认",
            "status": "已申请",
            "pending": True,
            "abnormal": False,
            "原结算ID": voucher.get("settlement_id"),
            "原结算版本": voucher.get("version"),
            "原结算编号": voucher.get("settlement_no"),
            "_lines": normalized,
            "_source_call_ids": [line["source_call_id"] for line in normalized],
            "_completed_steps": [],
            "_idempotency_key": idempotency_key,
            "_correction_type": correction_type,
        }
        rows.append(entry)
        # 申请即入账：更正用量写入用量计量共享目录，金额尚未结算，
        # 确认时只对本次修订部分生成新的结算版本。
        self._book_usage(entry)
        self._persist(entry)
        return entry, f"更正申请已登记，共{len(normalized)}条来源调用"

    # ---------- 状态流转 ----------

    def submit_correction(self, correction_id: int) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, correction_id)
        if entry is None:
            return None, f"{self.ENTITY} {correction_id} 不存在或已归档"
        if entry.get("status") not in ("草稿", "已中断"):
            return None, f"当前状态「{entry.get('status')}」不能提交更正申请"
        _, voucher = self._resolve_voucher(
            original_bill_no=entry.get("原账单编号"),
            billing_cycle=entry.get("账期"))
        if not voucher:
            return None, "原结算凭证不存在或尚未确认，无法提交更正申请"
        if not self._ensure_lines(entry):
            return None, "更正明细缺少来源调用，补齐后重新提交"
        entry["status"] = "已申请"
        entry["pending"] = True
        entry["abnormal"] = False
        self._persist(entry)
        return entry, "更正申请已提交"

    def interrupt_correction(
        self, correction_id: int, reason: str = "人工中断"
    ) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(self.MODULE, correction_id)
        if entry is None:
            return None, f"{self.ENTITY} {correction_id} 不存在或已归档"
        if entry.get("status") in ("已确认", "已通知"):
            return None, "更正已完成，不能回退为中断"
        entry["status"] = "已中断"
        entry["pending"] = True
        entry["_interrupted_reason"] = reason
        self._persist(entry)
        return entry, f"更正已中断：{reason}"

    def resume_correction(self, correction_id: int) -> tuple[dict[str, Any] | None, str]:
        """从断点恢复意外中断的更正，已完成的步骤不会重复执行。"""
        entry = store.find(self.MODULE, correction_id)
        if entry is None:
            return None, f"{self.ENTITY} {correction_id} 不存在或已归档"
        if entry.get("status") not in ("确认中", "已中断", "已申请"):
            return None, f"当前状态「{entry.get('status')}」无需恢复"
        return self.confirm_correction(correction_id, resumed=True)

    def confirm_correction(
        self,
        correction_id: int,
        *,
        resumed: bool = False,
        interrupt_after_stage: int | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """确认更正并重新聚合结算；按步骤断点持久化，可安全恢复。

        `interrupt_after_stage` 仅供演练/测试模拟第 N 步后意外中断。
        """
        entry = store.find(self.MODULE, correction_id)
        if entry is None:
            return None, f"{self.ENTITY} {correction_id} 不存在或已归档"

        status = entry.get("status")
        if status == "已通知":
            return entry, "更正已确认并已通知对账方"
        if status == "已确认":
            return entry, "更正已确认，无需重复结算"
        if status not in ("已申请", "确认中", "已中断"):
            return None, f"当前状态「{status}」不能确认更正"

        # 先恢复明细，避免把内存中丢失的 `_lines` 覆写回持久化记录。
        self._ensure_lines(entry)
        entry["status"] = "确认中"
        entry["pending"] = True
        self._persist(entry)

        completed: list[str] = list(entry.get("_completed_steps") or [])
        voucher: dict[str, Any] = {}

        for index, step in enumerate(CORRECTION_STEPS):
            if step in completed:
                continue
            try:
                voucher = self._run_step(entry, step)
            except ValueError as error:
                entry["status"] = "确认中"
                entry["_resume_stage"] = step
                entry["_last_error"] = str(error)
                entry["pending"] = True
                self._persist(entry)
                return None, f"更正执行中断于「{step}」：{error}；可恢复后继续"

            completed.append(step)
            entry["_completed_steps"] = completed
            entry["_resume_stage"] = (
                CORRECTION_STEPS[index + 1]
                if index + 1 < len(CORRECTION_STEPS) else "")
            self._persist(entry)

            if interrupt_after_stage is not None and index + 1 >= interrupt_after_stage:
                entry["status"] = "已中断"
                entry["_interrupted_reason"] = "模拟意外中断"
                entry["pending"] = True
                self._persist(entry)
                return entry, f"更正已在「{step}」后中断，可从断点恢复"

        # 汇总重新聚合后的金额凭据（恢复场景也从已生成版本回读）
        if not voucher.get("settlement_no") and entry.get("结算版本"):
            from app.services.data_manager import get_settlement_voucher
            voucher = get_settlement_voucher(
                entry.get("账期", ""), _as_int(entry.get("结算版本")) or None)
        if voucher and voucher.get("settlement_no"):
            entry["结算编号"] = voucher.get("settlement_no")
            entry["结算版本"] = voucher.get("version")
            entry["结算ID"] = voucher.get("settlement_id")
            entry["原确认金额"] = round(
                _as_float(voucher.get("previous_total_amount")), 2)
            entry["更正金额"] = round(
                _as_float(voucher.get("incremental_amount")), 2)
            entry["_settlement_total"] = round(
                _as_float(voucher.get("total_amount")), 2)
            entry["_settlement_line_count"] = len(voucher.get("line_items") or [])
            entry["_reopen_required"] = bool(
                _as_float(voucher.get("incremental_amount")))
            entry["重开追踪"] = (
                "需重开账期" if entry["_reopen_required"] else "无需重开")

        entry["status"] = "已确认"
        entry["pending"] = False
        entry["abnormal"] = False
        entry["_last_error"] = ""
        self._persist(entry)
        prefix = "更正已恢复并确认" if resumed else "更正已确认"
        return entry, f"{prefix}，已生成结算凭证 {entry.get('结算编号', '')}"

    def notify_counterparty(
        self, correction_id: int, remark: str = ""
    ) -> tuple[dict[str, Any] | None, str]:
        """向对账方再发一次对账结果；重复通知按幂等处理。"""
        from app.services.data_manager import (
            DATA_ROOT, list_reconciliation_notices,
            write_audit, write_reconciliation_notice,
        )

        entry = store.find(self.MODULE, correction_id)
        if entry is None:
            return None, f"{self.ENTITY} {correction_id} 不存在或已归档"
        if entry.get("status") == "已通知":
            entry["_notice_replayed"] = True
            return entry, "对账结果已发送，按幂等处理未重复通知"
        if entry.get("status") != "已确认":
            return None, "更正尚未确认，不能向对账方发送结果"

        notices = list_reconciliation_notices(correction_id=correction_id)
        notice_id = (max((_as_int(n.get("notice_id")) for n in notices), default=0)
                     if notices else 0) + 1
        if not notices:
            # 首次通知，全局编号
            existing = list_reconciliation_notices()
            notice_id = max(
                (_as_int(n.get("notice_id")) for n in existing), default=0) + 1
        notice = {
            "notice_id": notice_id,
            "notice_no": f"NOTI-{notice_id:04d}",
            "correction_id": correction_id,
            "correction_no": entry.get("更正编号"),
            "billing_cycle": entry.get("账期"),
            "counterparty": entry.get("对账方"),
            "original_bill_no": entry.get("原账单编号"),
            "settlement_no": entry.get("结算编号"),
            "previous_total_amount": entry.get("原确认金额"),
            "incremental_amount": entry.get("更正金额"),
            "total_amount": entry.get("_settlement_total"),
            "reopen_required": entry.get("_reopen_required", False),
            "notice_status": "已发送",
            "remark": remark,
        }
        write_reconciliation_notice(notice_id, notice)

        audit_id = max(
            (_as_int(f.stem) for f in DATA_ROOT.glob("audit/[0-9]*.json")),
            default=0) + 1
        write_audit(audit_id, {
            "audit_id": audit_id,
            "api_name": "",
            "caller": entry.get("对账方", ""),
            "event": "billing_correction_notified",
            "billing_cycle": entry.get("账期"),
            "original_bill_no": entry.get("原账单编号"),
            "settlement_no": entry.get("结算编号"),
            "incremental_amount": entry.get("更正金额"),
            "reopen_required": entry.get("_reopen_required", False),
        })

        entry["通知编号"] = notice["notice_no"]
        entry["通知状态"] = "已发送"
        entry["status"] = "已通知"
        entry["pending"] = False
        entry["_notice_ids"] = [notice_id]
        self._persist(entry)
        return entry, f"对账结果已发送给{entry.get('对账方', '对账方')}"

    # ---------- 查询 ----------

    def list_corrections(
        self, billing_cycle: str | None = None,
        original_bill_no: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = store.rows(self.MODULE)
        result = []
        for row in rows:
            if billing_cycle and row.get("账期") != billing_cycle:
                continue
            if original_bill_no and row.get("原账单编号") != original_bill_no:
                continue
            result.append(row)
        return result

    def correction_detail(self, correction_id: int) -> dict[str, Any]:
        """返回更正明细及可核对的凭据链路。"""
        from app.services.data_manager import (
            get_settlement_voucher, list_reconciliation_notices, list_usage,
        )

        entry = store.find(self.MODULE, correction_id)
        if entry is None:
            return {}
        cycle = entry.get("账期", "")
        usage_records = [
            r for r in list_usage(cycle)
            if str(r.get("correction_id")) == str(correction_id)
        ]
        voucher = get_settlement_voucher(
            cycle, _as_int(entry.get("结算版本")) or None)
        original = get_settlement_voucher(
            cycle, _as_int(entry.get("原结算版本")) or None)
        return {
            "correction": entry,
            "source_lines": usage_records,
            "original_voucher": original,
            "settlement_voucher": voucher,
            "notices": list_reconciliation_notices(correction_id=correction_id),
            "reopen_required": entry.get("_reopen_required", False),
        }

    # ---------- 动作分派 ----------

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        if action == "提交申请":
            result = self.submit_correction(entry_id)
        elif action == "确认更正":
            result = self.confirm_correction(entry_id)
        elif action == "恢复更正":
            result = self.resume_correction(entry_id)
        elif action == "中断更正":
            result = self.interrupt_correction(entry_id)
        elif action == "通知对账方":
            result = self.notify_counterparty(entry_id)
        else:
            result = super().run_action(entry_id, action)

        from app.services.data_manager import write_module_snapshot
        write_module_snapshot(self.MODULE, self.domain_snapshot())
        return result

    # ---------- 内部 ----------

    def _run_step(self, entry: dict[str, Any], step: str) -> dict[str, Any]:
        from app.services.data_manager import (
            get_settlement_voucher, mark_cycle_reopened, settle_billing,
        )

        cycle = entry.get("账期", "")
        if step == "校验原凭证":
            _, voucher = self._resolve_voucher(
                original_bill_no=entry.get("原账单编号"), billing_cycle=cycle)
            if not voucher:
                raise ValueError("原结算凭证不存在或尚未确认")
            entry["原结算版本"] = voucher.get("version")
            entry["原结算ID"] = voucher.get("settlement_id")
            entry["原结算编号"] = voucher.get("settlement_no")
            entry["原确认金额"] = round(_as_float(voucher.get("total_amount")), 2)
            self._persist(entry)
            return voucher

        if step == "入账更正用量":
            if not self._ensure_lines(entry):
                raise ValueError("更正明细缺少来源调用")
            self._book_usage(entry)
            return {}

        if step == "重新结算":
            key = entry.get("_idempotency_key") or entry.get("更正编号")
            voucher = settle_billing(
                cycle,
                use_auto_settlement=True,
                correction_id=_as_int(entry.get("id")),
                idempotency_key=f"correction:{key}:settle",
            )
            if voucher.get("empty") or not voucher.get("settlement_no"):
                raise ValueError("账期没有可结算的更正用量")
            entry["结算编号"] = voucher.get("settlement_no")
            entry["结算版本"] = voucher.get("version")
            entry["结算ID"] = voucher.get("settlement_id")
            self._persist(entry)
            return voucher

        if step == "标记账期重开":
            reopened = bool(_as_float(entry.get("更正金额")))
            if reopened:
                mark_cycle_reopened(cycle, _as_int(entry.get("id")))
            entry["_reopen_required"] = reopened
            entry["重开追踪"] = "需重开账期" if reopened else "无需重开"
            self._persist(entry)
            return {}

        raise ValueError(f"未知更正步骤：{step}")

    def _resolve_voucher(
        self, *, original_bill_no: str | None = None,
        billing_cycle: str | None = None,
    ) -> tuple[str, dict[str, Any]]:
        from app.services.data_manager import (
            list_usage, read_settlement_versions,
        )

        cycles: list[str] = []
        if billing_cycle:
            cycles = [billing_cycle]
        else:
            # 从共享目录枚举全部账期，支持只给账单号时回查。
            from app.services.data_manager import DATA_ROOT
            cycles = sorted(
                f.stem for f in DATA_ROOT.glob("usage/settlements/*.json")
                if f.stem and not f.stem.startswith("_"))
        for cycle in cycles:
            for voucher in read_settlement_versions(cycle):
                if original_bill_no:
                    if str(voucher.get("settlement_no")) == str(original_bill_no) or \
                            str(voucher.get("settlement_id")) == str(original_bill_no):
                        return cycle, voucher
                else:
                    return cycle, voucher
        if billing_cycle and original_bill_no:
            return billing_cycle, {}
        # 账单号未命中时再按用量文件兜底识别账期
        if original_bill_no and not billing_cycle:
            for record in list_usage():
                if record.get("settlement_no") == original_bill_no:
                    cycle = record.get("billing_cycle", "")
                    for voucher in read_settlement_versions(cycle):
                        if voucher.get("settlement_no") == original_bill_no:
                            return cycle, voucher
        return (billing_cycle or ""), {}

    def _normalize_lines(
        self, lines: list[dict[str, Any]] | None
    ) -> tuple[list[dict[str, Any]], str]:
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in lines or []:
            if not isinstance(raw, dict):
                return [], "来源明细格式不正确"
            source = (
                raw.get("source_call_id") or raw.get("来源调用")
                or raw.get("调用编号") or raw.get("call_id")
                or raw.get("call_no"))
            source = str(source or "").strip()
            if not source:
                return [], "每条更正明细都必须关联来源调用"
            if source in seen:
                return [], f"来源调用重复：{source}"
            seen.add(source)
            normalized.append({
                "source_call_id": source,
                "call_count": _as_int(
                    raw.get("call_count") or raw.get("调用次数")),
                "data_rows": _as_int(
                    raw.get("data_rows") or raw.get("数据行数")),
                "amount": round(_as_float(
                    raw.get("amount") if raw.get("amount") is not None
                    else raw.get("费用金额")), 2),
                "caller": raw.get("caller") or raw.get("调用方") or "",
                "api_name": raw.get("api_name") or raw.get("API名称") or "",
            })
        return normalized, ""

    def _duplicate_source_calls(
        self, billing_cycle: str, source_call_ids: list[str]
    ) -> list[str]:
        from app.services.data_manager import list_usage

        if not source_call_ids:
            return []
        existing: set[str] = set()
        for record in list_usage(billing_cycle):
            for source in (record.get("source_call_ids") or []):
                existing.add(str(source))
            if record.get("source_call_id"):
                existing.add(str(record["source_call_id"]))
        return [source for source in source_call_ids if source in existing]

    def _ensure_lines(self, entry: dict[str, Any]) -> bool:
        """内存丢失时从持久化记录或已入账用量恢复明细。"""
        if entry.get("_lines"):
            return True
        from app.services.data_manager import (
            list_usage, read_correction_record,
        )

        stored = read_correction_record(_as_int(entry.get("id")))
        lines = stored.get("_lines") or stored.get("lines") or []
        if lines:
            entry["_lines"] = lines
            entry["_source_call_ids"] = [
                line.get("source_call_id") for line in lines if line.get("source_call_id")]
            return True
        booked = [
            r for r in list_usage(entry.get("账期", ""))
            if str(r.get("correction_id")) == str(entry.get("id"))]
        if booked:
            entry["_lines"] = [{
                "source_call_id": (r.get("source_call_ids") or [""])[0],
                "call_count": _as_int(r.get("call_count")),
                "data_rows": _as_int(r.get("data_rows")),
                "amount": _as_float(r.get("amount")),
                "caller": r.get("caller", ""),
                "api_name": r.get("api_name", ""),
            } for r in booked]
            entry["_source_call_ids"] = [
                line["source_call_id"] for line in entry["_lines"]]
            return True
        return bool(entry.get("_lines"))

    def _book_usage(self, entry: dict[str, Any]) -> list[int]:
        """把更正来源明细写入用量共享目录；同一来源调用只入账一次。"""
        from app.services.data_manager import (
            DATA_ROOT, list_usage, write_usage,
        )

        cycle = entry.get("账期", "")
        existing = [
            r for r in list_usage(cycle)
            if str(r.get("correction_id")) == str(entry.get("id"))]
        booked_sources = {
            str((r.get("source_call_ids") or [""])[0]) for r in existing}
        usage_ids: list[int] = [
            _as_int(r.get("usage_id")) for r in existing if r.get("usage_id")]

        max_id = max(
            [r.get("usage_id", 0) for r in list_usage()]
            + [_as_int(row.get("id")) for row in store.rows("usage_billing")],
            default=0)
        # 现有文件 id 也要计入，避免与内存列表不同步
        max_id = max(
            [max_id] + [_as_int(f.stem) for f in DATA_ROOT.glob("usage/[0-9]*.json")])

        original_line = self._original_line(entry)
        for line in entry.get("_lines") or []:
            source = str(line.get("source_call_id"))
            if source in booked_sources:
                continue
            max_id += 1
            usage_id = max_id
            record_no = f"USAG-{usage_id:04d}"
            payload = {
                "usage_id": usage_id,
                "record_no": record_no,
                "caller": line.get("caller") or entry.get("对账方", ""),
                "api_name": line.get("api_name")
                or original_line.get("api_name", ""),
                "call_count": _as_int(line.get("call_count")),
                "data_rows": _as_int(line.get("data_rows")),
                "amount": round(_as_float(line.get("amount")), 2),
                "billed_amount": 0.0,
                "billing_cycle": cycle,
                "settlement_status": "计量中",
                "correction_id": _as_int(entry.get("id")),
                "correction_no": entry.get("更正编号"),
                "original_bill_no": entry.get("原账单编号"),
                "source_call_ids": [source],
                "source_call_id": source,
            }
            write_usage(usage_id, payload)
            usage_ids.append(usage_id)
            booked_sources.add(source)

            store.rows("usage_billing").append({
                "id": usage_id,
                "记录编号": record_no,
                "调用方": payload["caller"],
                "API名称": payload["api_name"],
                "调用次数": str(payload["call_count"]),
                "数据行数": str(payload["data_rows"]),
                "计费周期": cycle,
                "费用金额": payload["amount"],
                "结算状态": "计量中",
                "status": "计量中",
                "pending": True,
                "abnormal": False,
                "correction_id": _as_int(entry.get("id")),
                "original_bill_no": entry.get("原账单编号"),
                "source_call_ids": [source],
            })

        # 先把已入账的来源调用写回更正单再持久化，恢复时可据此回填明细。
        entry["更正用量编号"] = [f"USAG-{uid:04d}" for uid in usage_ids]
        entry["_usage_ids"] = usage_ids
        entry["来源调用"] = sorted(booked_sources)
        self._persist(entry)
        return usage_ids

    def _original_line(self, entry: dict[str, Any]) -> dict[str, Any]:
        from app.services.data_manager import get_settlement_voucher

        voucher = get_settlement_voucher(
            entry.get("账期", ""), _as_int(entry.get("原结算版本")) or None)
        for line in voucher.get("line_items") or []:
            if line.get("kind") == "usage":
                return line
        return {}

    def _parse_lines(self, raw: Any) -> list[dict[str, Any]]:
        if isinstance(raw, list):
            normalized, _ = self._normalize_lines(raw)
            return normalized
        if isinstance(raw, str) and raw.strip():
            import json
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                return []
            if isinstance(data, list):
                normalized, _ = self._normalize_lines(data)
                return normalized
        return []

    def _persist(self, entry: dict[str, Any]) -> None:
        from app.services.data_manager import write_correction_record
        payload = {
            key: value for key, value in entry.items()
            if not key.startswith("_replayed")
        }
        write_correction_record(_as_int(entry.get("id")), payload)

    def domain_snapshot(self) -> dict[str, Any]:
        from app.services.data_manager import list_reconciliation_notices

        rows = store.rows(self.MODULE)
        corrections = []
        for row in rows:
            corrections.append({
                "id": row.get("id"),
                "correction_no": row.get("更正编号"),
                "original_bill_no": row.get("原账单编号"),
                "billing_cycle": row.get("账期"),
                "counterparty": row.get("对账方"),
                "correction_type": row.get("更正类型"),
                "previous_amount": row.get("原确认金额"),
                "correction_amount": row.get("更正金额"),
                "settlement_no": row.get("结算编号"),
                "settlement_version": row.get("结算版本"),
                "reopen_required": row.get("_reopen_required", False),
                "reopen_tracking": row.get("重开追踪"),
                "status": row.get("status"),
            })
        return {
            "module": self.MODULE,
            "entity": self.ENTITY,
            "artifact": f"{self.ENTITY}数据集",
            "count": len(corrections),
            "corrections": corrections,
            "pending_count": sum(
                1 for r in corrections if r.get("status") in ("已申请", "确认中", "已中断")),
            "confirmed_count": sum(
                1 for r in corrections if r.get("status") in ("已确认", "已通知")),
            "reopen_required_count": sum(
                1 for r in corrections if r.get("reopen_required")),
            "notices": len(list_reconciliation_notices()),
        }
