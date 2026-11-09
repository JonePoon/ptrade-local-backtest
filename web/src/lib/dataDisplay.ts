import type { DataBinary } from '../api/types'

/** Presentation-only privacy switch; API values and request parameters remain unchanged. */
export function displayLocalPath(path: string | null | undefined, hidden: boolean): string {
  if (!path) return '路径未知'
  if (!hidden) return path
  const name = path.replaceAll(String.fromCharCode(92), '/').split('/').filter(Boolean).at(-1)
  return name ? '…/' + name : '（本机路径已隐藏）'
}

export function hideLocalPaths(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(hideLocalPaths)
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, hideLocalPaths(item)]))
  }
  if (typeof value === 'string') {
    const normalized = value.replaceAll(String.fromCharCode(92), '/')
    if (normalized.startsWith('/') || /^[A-Za-z]:/.test(normalized)) return displayLocalPath(value, true)
  }
  return value
}

export function binaryStatusLabel(binary: DataBinary | undefined): string {
  if (binary?.available === true) return '运行验证通过'
  if (binary?.exists === true) return '文件已发现 · 未就绪'
  if (binary?.exists === false) return '未发现'
  return '状态未知'
}
