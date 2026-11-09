// FastAPI 后端 API 客户端
import type {
  RunsResp,
  RunDetail,
  TradesResp,
  SourceResp,
  StrategyExportResp,
  StrategyExportReport,
  StrategyExportFormat,
  StrategyExportDownload,
} from './types'

const BASE = '/api'

export class ApiError extends Error {
  readonly status: number
  readonly report?: StrategyExportReport

  constructor(status: number, message: string, report?: StrategyExportReport) {
    super(`HTTP ${status}: ${message}`)
    this.name = 'ApiError'
    this.status = status
    this.report = report
  }
}

async function responseError(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => null) as {
    detail?: string | { message?: string; report?: StrategyExportReport }
  } | null
  const detail = body?.detail
  const message = typeof detail === 'string' ? detail : detail?.message
  const report = typeof detail === 'object' ? detail?.report : undefined
  return new ApiError(response.status, message || response.statusText || '请求失败', report)
}

async function req<T>(path: string, signal?: AbortSignal): Promise<T> {
  const r = await fetch(`${BASE}${path}`, { cache: 'no-store', signal })
  if (!r.ok) throw await responseError(r)
  return r.json() as Promise<T>
}

function exportPath(name: string, format: 'json' | StrategyExportFormat): string {
  return `/run/${encodeURIComponent(name)}/export?format=${format}`
}

async function downloadExport(
  name: string,
  format: StrategyExportFormat,
  signal?: AbortSignal,
): Promise<StrategyExportDownload> {
  // 重新请求下载端点，不能用旧预览绕过服务端的最新 ready/快照校验。
  const response = await fetch(`${BASE}${exportPath(name, format)}`, { cache: 'no-store', signal })
  if (!response.ok) throw await responseError(response)
  const disposition = response.headers.get('content-disposition') ?? ''
  const encodedName = /filename\*=UTF-8''([^;]+)/i.exec(disposition)?.[1]
  const fallback = /filename="([^"]+)"/i.exec(disposition)?.[1]
    ?? (format === 'python' ? 'strategy.py' : 'strategy.py.manifest.json')
  let filename = fallback
  if (encodedName) {
    try { filename = decodeURIComponent(encodedName) } catch { filename = fallback }
  }
  return { blob: await response.blob(), filename }
}

export const api = {
  ping: () => req<{ ok: boolean; root: string }>('/ping'),
  runs: () => req<RunsResp>('/runs'),
  detail: (name: string, signal?: AbortSignal) =>
    req<RunDetail>(`/run/${encodeURIComponent(name)}/detail`, signal),
  trades: (name: string, page = 1, pageSize = 20) =>
    req<TradesResp>(
      `/run/${encodeURIComponent(name)}/trades?page=${page}&page_size=${pageSize}`,
    ),
  log: (name: string, lines = 50, offset = 0, signal?: AbortSignal) =>
    req<{ lines: string[]; total?: number }>(
      `/run/${encodeURIComponent(name)}/log?lines=${lines}&offset=${offset}`,
      signal,
    ),
  source: (name: string) =>
    req<SourceResp>(`/run/${encodeURIComponent(name)}/source`),
  exportStrategy: (name: string, signal?: AbortSignal) =>
    req<StrategyExportResp>(exportPath(name, 'json'), signal),
  downloadExport,
}
