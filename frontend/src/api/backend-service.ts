import { request } from './client'
import { moduleMeta } from '@/data/modules'
import type { ActionResult, EntryRow, OverviewResult, PageResult } from '@/data/types'

export { moduleMeta }

export async function listEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<PageResult> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}?${query}`)
  if (!response.ok) throw new Error('业务数据读取失败')
  return await response.json()
}

export async function createEntry(
  key: string,
  values: Record<string, string>,
): Promise<ActionResult> {
  const response = await request(`/api/${key}`, {
    method: 'POST',
    body: JSON.stringify({ values }),
  })
  return await response.json()
}

export async function runAction(
  key: string,
  id: number,
  action: string,
): Promise<ActionResult> {
  const response = await request(`/api/${key}/${id}/actions`, {
    method: 'POST',
    body: JSON.stringify({ action }),
  })
  return await response.json()
}

export async function downloadEntries(
  key: string,
  filters: Record<string, string> = {},
): Promise<void> {
  const query = new URLSearchParams(filters).toString()
  const response = await request(`/api/${key}/export?${query}`)
  if (!response.ok) throw new Error('业务数据导出失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `${key}.csv`
  document.body.appendChild(anchor)
  anchor.click()
  document.body.removeChild(anchor)
  URL.revokeObjectURL(url)
}

export async function loadOverview(): Promise<OverviewResult> {
  const response = await request('/api/overview')
  if (!response.ok) throw new Error('运营概览读取失败')
  return await response.json()
}

export type SettlementVersion = {
  version: number
  settlement_no: string
  kind: string
  previous_total_amount: number
  incremental_amount: number
  total_amount: number
  correction_id: number | null
  settled_at: string
}

export type ReconciliationNotice = {
  notice_id: number
  notice_no: string
  incremental_amount: number
  reopen_required: boolean
  remark: string
}

export type Reconciliation = {
  billing_cycle: string
  reopened: boolean
  current_version: number
  actual_receivable: number
  versions: SettlementVersion[]
  notices: ReconciliationNotice[]
}

export async function loadReconciliation(billingCycle: string): Promise<Reconciliation> {
  const response = await request(
    `/api/usage_billing/settlements/${encodeURIComponent(billingCycle)}/reconciliation`,
  )
  if (!response.ok) throw new Error('账期结算记录读取失败')
  const payload = await response.json()
  return {
    ...payload,
    current_version: payload.versions?.length ?? 0,
  }
}
