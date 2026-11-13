<template>
  <section class="page" data-module="datasource">
    <header class="page-head">
      <div>
        <h2>采集源登记管理</h2>
        <p class="page-desc">维护待采集数据源的注册、连通性探测与生命周期管理，确保每个采集源有可验证的接入配置。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记采集源</button>
        <button class="btn" type="button" @click="exportRows">导出采集源登记清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>接入验证状态</th>
          <th>最近成功时间</th>
          <th>有效到期时间</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td>
            <span class="state-tag" :class="row['可调度'] ? 'valid' : 'stale'">
              {{ row['接入验证状态'] ?? '—' }}
            </span>
            <div v-if="row['不可用原因']" class="reason-cell">{{ row['不可用原因'] }}</div>
          </td>
          <td>{{ formatTime(row['最近成功时间']) }}</td>
          <td>{{ formatTime(row['有效到期时间']) }}</td>
          <td class="row-actions">
            <button
              class="link"
              type="button"
              :disabled="testingIds.has(Number(row.id))"
              @click="runAction('发起连通测试', row)"
            >
              {{ testingIds.has(Number(row.id)) ? '测试执行中…' : '发起连通测试' }}
            </button>
            <button class="link" type="button" @click="openEdit(row)">修改配置</button>
            <button class="link" type="button" @click="runAction('标记上线', row)">标记上线</button>
            <button class="link" type="button" @click="runAction('归档源配置', row)">归档源配置</button>
            <button class="link" type="button" @click="openHistory(row)">验证记录</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 4" class="empty-state">暂无采集源登记数据，可先登记采集源</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集源登记记录</span>
      <span v-if="infoMessage" class="ok-text">{{ infoMessage }}</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>

    <div v-if="editVisible" class="modal-mask" @click.self="editVisible = false">
      <div class="modal-card">
        <div class="modal-head">
          <h3 class="modal-title">修改采集源配置 · {{ editForm['源端编号'] }}</h3>
          <button class="btn ghost" type="button" @click="editVisible = false">关闭</button>
        </div>
        <p class="page-desc">入口URL、请求方式、字符编码、超时秒数任一项变化都会改变采集指纹，未过期的连通结论立即失效，历史验证记录仍按当时有效期保留。</p>
        <form class="filter-bar" @submit.prevent="submitEdit">
          <label v-for="field in editFields" :key="field" class="filter-item">
            <span>{{ field }}</span>
            <input v-model="editForm[field]" :placeholder="field" />
          </label>
          <button class="btn primary" type="submit">保存配置</button>
        </form>
      </div>
    </div>

    <div v-if="historyVisible" class="modal-mask" @click.self="historyVisible = false">
      <div class="modal-card">
        <div class="modal-head">
          <h3 class="modal-title">历史验证记录 · {{ historySourceCode }}</h3>
          <button class="btn ghost" type="button" @click="historyVisible = false">关闭</button>
        </div>
        <p class="page-desc">历史记录全部保留，按各记录当时的有效期与当前采集指纹标注有效/已失效。</p>
        <table class="data-table">
          <thead>
            <tr>
              <th v-for="column in historyColumns" :key="column">{{ column }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in historyRows" :key="String(item['记录编号'])">
              <td v-for="column in historyColumns" :key="column">
                <span v-if="column === '记录状态'" class="state-tag" :class="item[column] === '有效' ? 'valid' : 'stale'">
                  {{ item[column] }}
                </span>
                <template v-else>{{ column.includes('时间') ? formatTime(item[column]) : (item[column] ?? '—') }}</template>
              </td>
            </tr>
            <tr v-if="!historyRows.length">
              <td :colspan="historyColumns.length" class="empty-state">该采集源暂无历史验证记录</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | boolean | null>

const ENDPOINT = '/api/datasource'
const columns = ["源端编号", "目标域名", "入口URL", "请求方式", "字符编码", "访问频率", "超时秒数", "归属项目"]
const historyColumns = ["记录编号", "记录状态", "探测时间", "有效到期时间", "采集指纹", "请求方式", "字符编码", "超时秒数", "探测结论"]
const stats = ref([{"label": "采集源总数", "value": 0}, {"label": "验证有效", "value": 0}, {"label": "待重新验证", "value": 0}])

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const infoMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

// 行级测试锁：重复点击或连续触发时按钮置灰，后端同样做并发去重，双保险。
const testingIds = ref<Set<number>>(new Set())
const historyVisible = ref(false)
const historyRows = ref<Row[]>([])
const historySourceCode = ref('')

// 配置编辑：仅指纹四项与访问频率等可改字段，保存后由后端实时重算指纹。
const editFields = ["目标域名", "入口URL", "请求方式", "字符编码", "访问频率", "超时秒数", "归属项目"]
const editVisible = ref(false)
const editId = ref(0)
const editForm = ref<Record<string, string>>({})

function openEdit(row: Row) {
  editId.value = Number(row.id)
  editForm.value = Object.fromEntries(
    editFields.map((field) => [field, String(row[field] ?? '')]),
  )
  editVisible.value = true
}

async function submitEdit() {
  errorMessage.value = ''
  infoMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${editId.value}`, {
      method: 'PUT',
      body: JSON.stringify({ values: editForm.value }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message ?? '采集源配置更新失败')
    }
    infoMessage.value = payload.message ?? '配置已更新'
    editVisible.value = false
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集源配置更新失败'
  }
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '采集源登记入口尚未接入审批流'
}

function formatTime(value: unknown): string {
  if (!value || typeof value !== 'string') return '—'
  // 后端返回 ISO（UTC），展示成本地可读时间，去掉秒以下精度。
  const dt = new Date(value)
  if (Number.isNaN(dt.getTime())) return value
  return dt.toLocaleString('zh-CN', { hour12: false })
}

async function openHistory(row: Row) {
  historySourceCode.value = String(row['源端编号'] ?? '')
  try {
    const response = await request(`${ENDPOINT}/${row.id}/verifications`)
    if (!response.ok) {
      throw new Error('历史验证记录读取失败')
    }
    const payload = await response.json()
    historyRows.value = payload.items ?? []
    historyVisible.value = true
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '历史验证记录读取失败'
  }
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  infoMessage.value = ''
  const rowId = Number(row.id)
  if (action === '发起连通测试') {
    if (testingIds.value.has(rowId)) {
      return
    }
    testingIds.value.add(rowId)
  }
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await response.json().catch(() => null)
    if (!response.ok || !payload?.ok) {
      throw new Error(payload?.message ?? '采集源登记动作未生效，请稍后重试')
    }
    // 复用/失败诊断等结论由后端给出，原样透出，让用户感知这次是否真的重发了探测。
    infoMessage.value = payload.message ?? '操作已生效'
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集源登记操作失败'
  } finally {
    testingIds.value.delete(rowId)
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('采集源列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats.value[0].value = total.value
    stats.value[1].value = rows.value.filter((row: Row) => row['可调度'] === true).length
    stats.value[2].value = rows.value.filter((row: Row) => row['可调度'] === false).length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集源登记列表读取失败'
  }
}

onMounted(reload)
</script>
