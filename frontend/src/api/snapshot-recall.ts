/** 快照副本召回接口：可召回清单、选择封存、归档核对、按位置召回与明细读取。 */

export type RecallTier = '热存储' | '冷存储' | '归档'

export type RecallCatalogItem = {
  partition_id: string
  partition_code: string
  target_wide_table: string
  partition_date: string
  partition_key_value: string
  readable_tier: RecallTier | string
  replica_id: string
  position: string
  version: number
  digest: string | null
  verifiable: boolean
  row_count: number
  schema_digest: string
  list_position: number
  catalog_generation: number
  view_filters: {
    partition_date: string | null
    target_wide_table: string | null
    readable_tier: string | null
  }
}

export type RecallCatalogPage = {
  items: RecallCatalogItem[]
  total: number
  page: number
  size: number
  list_token: string
  has_more: boolean
  next_page: number | null
  basis: {
    captured_at: string
    catalog_generation: number
    filters: Record<string, string | null>
    total: number
  }
}

export type RecallSelection = {
  selection_id: string
  recorded_at: string
  partition_id: string | null
  partition_code: string | null
  target_wide_table: string | null
  partition_date: string | null
  selected_tier: string | null
  selected_position: string | null
  selected_version: number | null
  selected_digest: string | null
  selected_list_position: number | null
  list_token: string | null
  catalog_generation: number | null
  view_filters: Record<string, string | null>
  identity: {
    partition_id: string | null
    partition_code: string | null
    target_wide_table: string | null
    partition_date: string | null
  }
  basis_recorded: boolean
}

export type RecallRow = Record<string, string | number>

export type RecallResult = {
  selection_id: string | null
  partition_id: string | null
  partition_code: string | null
  target_wide_table: string | null
  partition_date: string | null
  selected_tier: string | null
  selected_position: string | null
  selected_version: number | null
  selected_digest: string | null
  selected_list_position: number | null
  recorded_at: string | null
  status: string
  pending_reason: string | null
  conflict: {
    kind: string
    message: string
    selected_tier: string
    actual_tier: string | null
    tier_changed: boolean
    selected_position: string
    actual_position: string | null
    position_changed: boolean
    selected_version: number | null
    actual_version: number | null
    version_changed: boolean
  } | null
  actual_tier: string | null
  actual_position: string | null
  actual_version: number | null
  actual_digest: string | null
  actual_replica_id: string | null
  current_list_position: number | null
  actual_list_position: number | null
  row_count: number
  detail_row_count: number
  rows: RecallRow[]
}

export type RecallSummary = {
  ok: boolean
  detail_size: number
  recalled: number
  hit: number
  moved: number
  pending: number
  results: RecallResult[]
}

export type ArchiveResult = {
  ok: boolean
  message: string
  partition_id?: string
  partition_code?: string
  from_tier?: string
  to_tier?: string
  old_position?: string
  new_position?: string
  version?: number
  verifiable?: boolean
}

async function readJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    let detail = fallback
    try {
      const payload = (await response.json()) as { detail?: string }
      if (payload.detail) detail = payload.detail
    } catch {
      // 保留兜底文案
    }
    throw new Error(detail)
  }
  return (await response.json()) as T
}

export async function listRecallCatalog(params: {
  partitionDate?: string
  targetWideTable?: string
  readableTier?: string
  page?: number
  size?: number
  listToken?: string
}): Promise<RecallCatalogPage> {
  const query = new URLSearchParams()
  if (params.partitionDate) query.set('partition_date', params.partitionDate)
  if (params.targetWideTable) query.set('target_wide_table', params.targetWideTable)
  if (params.readableTier) query.set('readable_tier', params.readableTier)
  query.set('page', String(params.page ?? 1))
  query.set('size', String(params.size ?? 10))
  if (params.listToken) query.set('list_token', params.listToken)
  const response = await fetch(`/api/snapshot-recall/catalog?${query.toString()}`)
  return readJson<RecallCatalogPage>(response, '可召回分区清单读取失败')
}

export async function captureRecallSelections(
  items: RecallCatalogItem[],
): Promise<{ ok: boolean; count: number; selections: RecallSelection[] }> {
  const response = await fetch('/api/snapshot-recall/selections', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items }),
  })
  return readJson(response, '选择封存失败')
}

export async function recallSelections(
  selectionIds: string[],
  detailSize = 11,
): Promise<RecallSummary> {
  const response = await fetch('/api/snapshot-recall/recall', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ selection_ids: selectionIds, detail_size: detailSize }),
  })
  return readJson<RecallSummary>(response, '快照召回失败')
}

export async function archiveSnapshotReplica(payload: {
  partitionId?: string
  partitionCode?: string
  fromTier?: string
  verifiable?: boolean
}): Promise<ArchiveResult> {
  const response = await fetch('/api/snapshot-recall/archive', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      partition_id: payload.partitionId ?? null,
      partition_code: payload.partitionCode ?? null,
      from_tier: payload.fromTier ?? '冷存储',
      verifiable: payload.verifiable ?? true,
    }),
  })
  return readJson<ArchiveResult>(response, '副本归档失败')
}
