<template>
  <section class="recall-panel" data-testid="snapshot-recall">
    <header class="recall-head">
      <div>
        <h3>副本召回范围</h3>
        <p class="page-desc">
          按日期、宽表和可读层级筛选可召回分区；翻页依据冻结后位次不再漂移，
          选中的分区按身份、层级和版本记录，归档后仍对准同一个逻辑分区。
        </p>
      </div>
      <div class="recall-actions">
        <button class="btn" data-testid="recall-reset" type="button" @click="reset">重置条件</button>
        <button class="btn primary" data-testid="recall-query" type="button" @click="search">查询可召回清单</button>
      </div>
    </header>

    <form class="analysis-form" @submit.prevent="search">
      <label>
        <span>分区日期</span>
        <input v-model="form.partitionDate" data-testid="recall-filter-date" placeholder="2026-11-18" />
      </label>
      <label>
        <span>目标宽表</span>
        <input v-model="form.targetWideTable" data-testid="recall-filter-table" placeholder="WT_ORDER" />
      </label>
      <label>
        <span>可读层级</span>
        <select v-model="form.readableTier" data-testid="recall-filter-tier">
          <option value="">全部层级</option>
          <option value="热存储">热存储</option>
          <option value="冷存储">冷存储</option>
          <option value="归档">归档</option>
        </select>
      </label>
      <label>
        <span>每页条数</span>
        <input v-model.number="form.size" data-testid="recall-filter-size" type="number" min="1" max="200" />
      </label>
    </form>

    <p v-if="errorMessage" class="error-text" data-testid="recall-error">{{ errorMessage }}</p>

    <div class="recall-basis" v-if="catalog">
      <span>共 {{ catalog.total }} 个可召回分区</span>
      <span>清单依据：{{ catalog.list_token.slice(0, 12) }}…（第 {{ catalog.basis.catalog_generation }} 次目录变更时冻结）</span>
      <span>{{ catalog.basis.captured_at }}</span>
    </div>

    <table class="data-table">
      <thead>
        <tr>
          <th>选择</th>
          <th>位次</th>
          <th>分区编号</th>
          <th>目标宽表</th>
          <th>分区日期</th>
          <th>可读层级</th>
          <th>副本位置</th>
          <th>版本</th>
          <th>校验摘要</th>
          <th>行数</th>
          <th>可核对</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr
          v-for="item in catalog?.items ?? []"
          :key="item.partition_id"
          :data-testid="`recall-row-${item.partition_code}`"
        >
          <td>
            <input
              type="checkbox"
              :data-testid="`recall-select-${item.partition_code}`"
              :checked="isSelected(item)"
              @change="toggle(item, ($event.target as HTMLInputElement).checked)"
            />
          </td>
          <td>{{ item.list_position }}</td>
          <td>{{ item.partition_code }}</td>
          <td>{{ item.target_wide_table }}</td>
          <td>{{ item.partition_date }}</td>
          <td>{{ item.readable_tier }}</td>
          <td class="mono">{{ item.position }}</td>
          <td>v{{ item.version }}</td>
          <td class="mono">{{ item.digest ?? '—' }}</td>
          <td>{{ item.row_count }}</td>
          <td>{{ item.verifiable ? '可核对' : '待核' }}</td>
          <td>
            <button
              class="link"
              type="button"
              :data-testid="`recall-archive-${item.partition_code}`"
              @click="archive(item)"
            >
              归档{{ item.readable_tier }}副本
            </button>
          </td>
        </tr>
        <tr v-if="catalog && !catalog.items.length">
          <td colspan="12" class="empty-state">当前条件下没有可召回分区</td>
        </tr>
      </tbody>
    </table>

    <div class="recall-pager" v-if="catalog">
      <button
        class="btn"
        data-testid="recall-prev"
        type="button"
        :disabled="catalog.page <= 1"
        @click="goPage(catalog.page - 1)"
      >
        上一页
      </button>
      <span>第 {{ catalog.page }} / {{ lastPage }} 页 · 本页 {{ catalog.items.length }} 条</span>
      <button
        class="btn"
        data-testid="recall-next"
        type="button"
        :disabled="!catalog.has_more"
        @click="goPage(catalog.next_page ?? catalog.page + 1)"
      >
        下一页
      </button>
    </div>

    <section class="recall-selection" data-testid="recall-selection">
      <header class="recall-subhead">
        <h4>已选召回范围（{{ selections.length }}）</h4>
        <div class="recall-actions">
          <button class="btn ghost" data-testid="recall-clear" type="button" :disabled="!selections.length" @click="clearSelections">
            清空选择
          </button>
          <button class="btn primary" data-testid="recall-submit" type="button" :disabled="!selections.length || recalling" @click="submitRecall">
            {{ recalling ? '召回中…' : '提交召回范围并召回' }}
          </button>
        </div>
      </header>
      <table class="data-table">
        <thead>
          <tr>
            <th>分区编号</th>
            <th>分区身份</th>
            <th>选中层级</th>
            <th>选中位置</th>
            <th>选中版本</th>
            <th>清单位次</th>
            <th>记录时间</th>
            <th>依据</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="selection in selections"
            :key="selection.selection_id"
            :data-testid="`selection-${selection.partition_code}`"
          >
            <td>{{ selection.partition_code }}</td>
            <td class="mono">{{ selection.partition_id }}</td>
            <td>{{ selection.selected_tier }}</td>
            <td class="mono">{{ selection.selected_position }}</td>
            <td>v{{ selection.selected_version }}</td>
            <td>{{ selection.selected_list_position ?? '—' }}</td>
            <td>{{ selection.recorded_at }}</td>
            <td>{{ selection.basis_recorded ? '已记录' : '缺少位置/版本，召回待核' }}</td>
          </tr>
          <tr v-if="!selections.length">
            <td colspan="8" class="empty-state">先在清单里勾选分区，选择时的身份、层级和版本会立即记录</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="recall-results" data-testid="recall-results" v-if="summary">
      <header class="recall-subhead">
        <h4>召回结果</h4>
        <span class="recall-tally">
          命中 {{ summary.hit }} · 位置/层级变更 {{ summary.moved }} · 待核 {{ summary.pending }}
        </span>
      </header>
      <article
        v-for="result in summary.results"
        :key="result.selection_id ?? result.partition_code ?? 'unknown'"
        class="recall-result"
        :data-testid="`recall-result-${result.partition_code ?? 'unknown'}`"
        :data-status="result.status"
      >
        <div class="recall-result-head">
          <strong>{{ result.partition_code ?? '未知分区' }}</strong>
          <span class="recall-badge" :class="statusClass(result.status)">{{ result.status }}</span>
          <span class="mono">{{ result.partition_id ?? '无身份' }}</span>
        </div>
        <dl class="recall-facts">
          <div>
            <dt>选择记录</dt>
            <dd>{{ result.selected_tier ?? '—' }} · v{{ result.selected_version ?? '—' }} · 位次 {{ result.selected_list_position ?? '—' }}</dd>
          </div>
          <div>
            <dt>实际读取位置</dt>
            <dd class="mono">{{ result.actual_tier ?? '—' }} · {{ result.actual_position ?? '未读取' }}</dd>
          </div>
          <div>
            <dt>当前位次</dt>
            <dd>{{ result.current_list_position ?? '不在当前清单' }}</dd>
          </div>
          <div>
            <dt>明细行数</dt>
            <dd>{{ result.detail_row_count }} / 共 {{ result.row_count }} 行</dd>
          </div>
        </dl>
        <p v-if="result.conflict" class="recall-conflict" data-testid="recall-conflict">
          {{ result.conflict.message }}
        </p>
        <p v-if="result.pending_reason" class="recall-pending" data-testid="recall-pending">
          待核原因：{{ result.pending_reason }}
        </p>
        <details v-if="result.rows.length" class="recall-rows">
          <summary>查看 {{ result.rows.length }} 行明细</summary>
          <table class="data-table">
            <thead>
              <tr>
                <th v-for="column in detailColumns" :key="column">{{ column }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(row, index) in result.rows" :key="index">
                <td v-for="column in detailColumns" :key="column">{{ row[column] ?? '—' }}</td>
              </tr>
            </tbody>
          </table>
        </details>
      </article>
    </section>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import {
  archiveSnapshotReplica,
  captureRecallSelections,
  listRecallCatalog,
  recallSelections,
  type RecallCatalogItem,
  type RecallCatalogPage,
  type RecallSelection,
  type RecallSummary,
} from '@/api/snapshot-recall'

const form = ref({
  partitionDate: '2026-11-18',
  targetWideTable: 'WT_ORDER',
  readableTier: '冷存储',
  size: 1,
})
const catalog = ref<RecallCatalogPage | null>(null)
const selections = ref<RecallSelection[]>([])
const summary = ref<RecallSummary | null>(null)
const errorMessage = ref('')
const recalling = ref(false)

const lastPage = computed(() => {
  if (!catalog.value) return 1
  return Math.max(Math.ceil(catalog.value.total / catalog.value.size), 1)
})
const detailColumns = computed(() => {
  const first = summary.value?.results.find((result) => result.rows.length)?.rows[0]
  return first ? Object.keys(first) : []
})

function requireCatalog(): RecallCatalogPage {
  if (!catalog.value) throw new Error('请先查询可召回清单')
  return catalog.value
}

function filters() {
  return {
    partitionDate: form.value.partitionDate,
    targetWideTable: form.value.targetWideTable,
    readableTier: form.value.readableTier,
  }
}

async function search() {
  errorMessage.value = ''
  try {
    catalog.value = await listRecallCatalog({ ...filters(), page: 1, size: form.value.size })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '可召回清单读取失败'
  }
}

async function goPage(page: number) {
  errorMessage.value = ''
  try {
    catalog.value = await listRecallCatalog({
      ...filters(),
      page,
      size: requireCatalog().size,
      listToken: requireCatalog().list_token,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '翻页失败，请重新筛选'
  }
}

function reset() {
  form.value = { partitionDate: '', targetWideTable: '', readableTier: '', size: 10 }
  catalog.value = null
  errorMessage.value = ''
  void search()
}

function isSelected(item: RecallCatalogItem) {
  return selections.value.some((selection) => selection.partition_id === item.partition_id)
}

async function toggle(item: RecallCatalogItem, checked: boolean) {
  errorMessage.value = ''
  if (!checked) {
    selections.value = selections.value.filter((selection) => selection.partition_id !== item.partition_id)
    return
  }
  try {
    const payload = await captureRecallSelections([item])
    selections.value = [...selections.value, ...payload.selections]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '选择封存失败'
  }
}

function clearSelections() {
  selections.value = []
  summary.value = null
}

async function archive(item: RecallCatalogItem) {
  errorMessage.value = ''
  try {
    const result = await archiveSnapshotReplica({
      partitionId: item.partition_id,
      partitionCode: item.partition_code,
      fromTier: item.readable_tier,
    })
    if (!result.ok) throw new Error(result.message)
    await search()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '副本归档失败'
  }
}

async function submitRecall() {
  recalling.value = true
  errorMessage.value = ''
  try {
    summary.value = await recallSelections(selections.value.map((selection) => selection.selection_id))
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '快照召回失败'
  } finally {
    recalling.value = false
  }
}

function statusClass(status: string) {
  if (status === '命中') return 'ok'
  if (status === '待核') return 'pending'
  return 'moved'
}

onMounted(search)
</script>

<style scoped>
.recall-panel { margin-top: 20px; border-top: 2px dashed var(--border); padding-top: 16px; }
.recall-head, .recall-subhead { display: flex; justify-content: space-between; align-items: flex-start; gap: 12px; }
.recall-head h3, .recall-subhead h4 { margin: 0; }
.recall-actions { display: flex; gap: 8px; }
.recall-basis { display: flex; flex-wrap: wrap; gap: 16px; color: var(--muted); font-size: 12px; margin: 8px 0; }
.recall-pager { display: flex; align-items: center; gap: 12px; margin: 10px 0; font-size: 13px; color: var(--muted); }
.recall-selection { margin-top: 16px; }
.recall-results { margin-top: 16px; }
.recall-tally { color: var(--muted); font-size: 13px; }
.recall-result { border: 1px solid var(--border); border-radius: 8px; padding: 12px; margin-top: 10px; background: #fff; }
.recall-result[data-status='待核'] { border-color: #f0b429; background: #fffdf5; }
.recall-result[data-status='层级变更'], .recall-result[data-status='位置变更'] { border-color: #3b82f6; background: #f8fbff; }
.recall-result-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.recall-badge { font-size: 12px; border-radius: 999px; padding: 2px 10px; }
.recall-badge.ok { background: #dcfce7; color: #166534; }
.recall-badge.moved { background: #dbeafe; color: #1e40af; }
.recall-badge.pending { background: #fef3c7; color: #92400e; }
.recall-facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 8px; margin: 10px 0; }
.recall-facts dt { font-size: 12px; color: var(--muted); }
.recall-facts dd { margin: 2px 0 0; font-size: 13px; }
.recall-conflict { color: #1e40af; font-size: 13px; }
.recall-pending { color: #92400e; font-size: 13px; }
.recall-rows summary { cursor: pointer; color: var(--brand); font-size: 13px; margin: 6px 0; }
.mono { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; word-break: break-all; }
</style>
