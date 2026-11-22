<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="meta.fields.length + 2" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <section v-if="meta.key === 'usage_billing'" class="correction-panel">
      <h3>结算版本与账期更正</h3>
      <p class="page-desc">结算按账期版本化追加，旧版本可按原样取数；更正只对修订部分出账。</p>
      <form class="filter-bar" @submit.prevent="loadSettlement">
        <label class="filter-item">
          <span>账期</span>
          <input v-model="settlementCycle" data-testid="settlement-cycle" placeholder="如 2026-10" />
        </label>
        <button class="btn" data-testid="load-settlement" type="submit">查看结算版本</button>
      </form>
      <div v-if="settlement" class="settlement-summary">
        <span>当前版本：v{{ settlement.current_version }}</span>
        <span>实际应收：{{ settlement.actual_receivable }}</span>
        <span>{{ settlement.reopened ? '账期需重开' : '账期无需重开' }}</span>
      </div>
      <table v-if="settlement" class="data-table">
        <thead>
          <tr><th>版本</th><th>结算编号</th><th>类型</th><th>上次金额</th><th>本次增量</th><th>累计金额</th><th>更正单</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in settlement.versions" :key="item.settlement_no" :data-testid="`version-${item.version}`">
            <td>v{{ item.version }}</td>
            <td>{{ item.settlement_no }}</td>
            <td>{{ item.kind }}</td>
            <td>{{ item.previous_total_amount }}</td>
            <td>{{ item.incremental_amount }}</td>
            <td>{{ item.total_amount }}</td>
            <td>{{ item.correction_id ?? '—' }}</td>
          </tr>
        </tbody>
      </table>
      <div v-if="settlement?.notices?.length" class="notice-list">
        <h4>对账通知</h4>
        <p v-for="notice in settlement.notices" :key="notice.notice_id">
          {{ notice.notice_no }} · 增量 {{ notice.incremental_amount }} ·
          {{ notice.reopen_required ? '需重开账期' : '无需重开' }} · {{ notice.remark || '无备注' }}
        </p>
      </div>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  loadReconciliation,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import type { Reconciliation } from '@/api/backend-service'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = moduleMeta(props.moduleKey)
const settlementCycle = ref('')
const settlement = ref<Reconciliation | null>(null)
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = meta.fields.slice(0, 3)
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})
const stats = computed(() => [
  { label: meta.metrics[0], value: total.value },
  { label: meta.metrics[1], value: rows.value.filter((row) => row.status === meta.statuses[1]).length },
  {
    label: meta.metrics[2],
    value: rows.value.filter((row) => row.status === meta.statuses[meta.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.key, Number(row.id), action)
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

async function loadSettlement() {
  errorMessage.value = ''
  const cycle = settlementCycle.value.trim()
  if (!cycle) {
    errorMessage.value = '请先填写账期，例如 2026-10'
    return
  }
  try {
    settlement.value = await loadReconciliation(cycle)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '结算记录读取失败'
  }
}

onMounted(reload)
</script>
