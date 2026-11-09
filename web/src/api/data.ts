import type { DataJob, DataOperation, DataOverrides, DataPlan, DataStatus } from './types'

export class DataApiError extends Error {
  readonly status: number
  constructor(status: number, message: string) {
    super('HTTP ' + status + ': ' + message)
    this.name = 'DataApiError'
    this.status = status
  }
}

async function request<T>(path: string, signal?: AbortSignal, body?: object, key?: string): Promise<T> {
  const headers: Record<string, string> = {}
  if (body) headers['Content-Type'] = 'application/json'
  if (key) headers['Idempotency-Key'] = key
  const response = await fetch('/api/data' + path, {
    method: body ? 'POST' : 'GET', cache: 'no-store', signal, headers,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = payload?.detail
    let message = response.statusText || '请求失败'
    if (typeof detail === 'string') message = detail
    else if (Array.isArray(detail)) message = detail.map(d => typeof d?.msg === 'string' ? d.msg : '参数无效').join('；')
    else if (detail && typeof detail === 'object' && 'message' in detail) message = String(detail.message)
    throw new DataApiError(response.status, message)
  }
  return response.json() as Promise<T>
}

export const dataApi = {
  status: (signal?: AbortSignal) => request<DataStatus>('/status', signal),
  plan: (overrides: DataOverrides, signal?: AbortSignal) => request<DataPlan>('/plan', signal, { overrides }),
  jobs: (signal?: AbortSignal) => request<{ jobs: DataJob[] }>('/jobs', signal),
  job: (id: string, signal?: AbortSignal) => request<DataJob>('/jobs/' + encodeURIComponent(id), signal),
  start: (operation: DataOperation, overrides: DataOverrides, key: string, signal?: AbortSignal) =>
    request<DataJob>('/jobs', signal, { operation, overrides }, key),
  cancel: (id: string, signal?: AbortSignal) => request<DataJob>('/jobs/' + encodeURIComponent(id) + '/cancel', signal, {}),
}
