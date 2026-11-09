// 主题工具：浅色/深色切换 + localStorage 持久化（默认深色）
export type Theme = 'dark' | 'light'
export const THEME_STORAGE_KEY = 'ptrade-local-backtest-theme'

export function getTheme(): Theme {
  if (typeof localStorage === 'undefined') return 'dark'
  return localStorage.getItem(THEME_STORAGE_KEY) === 'light' ? 'light' : 'dark'
}

export function applyTheme(t: Theme): void {
  document.documentElement.classList.toggle('dark', t === 'dark')
  try {
    localStorage.setItem(THEME_STORAGE_KEY, t)
  } catch {
    /* 隐私模式等场景忽略 */
  }
}

/** 初始化（main.ts 挂载前调用）：读偏好并应用 */
export function initTheme(): Theme {
  const t = getTheme()
  applyTheme(t)
  return t
}
