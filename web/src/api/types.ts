// 后端 /api 返回类型定义（与 ptrade-sim/server.py 对应）

export type RunStatus = 'running' | 'done' | 'interrupted' | 'missing' | 'waiting'

export interface SlimMetrics {
  total_return: number | null
  annual_return: number | null
  sharpe: number | null
  max_drawdown: number | null
  calmar: number | null
  final_value: number | null
  benchmark_return: number | null
  trade_count: number | null
  total_commission: number | null
}

export interface RunInfo {
  name: string
  status: RunStatus
  phase: string | null
  strategy: string | null
  strategy_name: string | null
  start_date: string | null
  end_date: string | null
  capital_base: number | null
  benchmark: string | null
  total_days: number | null
  day_done: number | null
  current_date: string | null
  started_at: string | null
  elapsed_sec: number | null
  done_at: string | null
  metrics: SlimMetrics | null
  source_path: string | null
  source_exists: boolean
}

export interface FullSummary {
  total_return?: number
  annual_return?: number
  sharpe?: number
  max_drawdown?: number
  calmar?: number
  win_rate?: number
  profit_loss_ratio?: number
  final_value?: number
  benchmark_return?: number
  trade_count?: number
  total_commission?: number
  // 月度分布：{ "2024-01": 0.0696 }（比例）
  monthly_returns?: Record<string, number>
  monthly_stats?: {
    win_rate?: number
    best_month?: { month?: string; return?: number }
    worst_month?: { month?: string; return?: number }
    mean?: number
    median?: number
    std?: number
    skew?: number
    kurt?: number
  }
  config?: {
    strategy?: string
    strategy_name?: string
    start_date?: string
    end_date?: string
    capital_base?: number
    benchmark?: string
  }
  // 其余字段可忽略
  [k: string]: unknown
}

export interface SeriesData {
  dates: string[]
  equity: (number | null)[]
  drawdown: (number | null)[]
  bench: (number | null)[]
}

export interface RunDetail {
  name: string
  status: RunStatus
  progress: Record<string, unknown> | null
  summary: FullSummary | null
  series: SeriesData
  /** 按月聚合扩展：{ "2024-01": {ret, bench, excess, mdd, trades, commission, beta, alpha} }（比例） */
  monthly_ext?: Record<
    string,
    {
      ret?: number
      bench?: number | null
      excess?: number | null
      mdd?: number | null
      trades?: number
      commission?: number
      beta?: number | null
      alpha?: number | null
    }
  >
  /** 整段回测 Alpha（年化）/ Beta（日收益回归） */
  alpha_beta?: { beta: number | null; alpha: number | null }
  logs: string[]
}

export interface TradesResp {
  rows: Record<string, unknown>[]
  total?: number
  error?: string
}

/** trades.csv 行（后端 astype(object) 全字段字符串/数字混合） */
export interface TradesRow {
  time?: string
  security?: string
  side?: string // 'buy' | 'sell'
  amount?: number | string
  price?: number | string
  turnover?: number | string
  commission?: number | string
  order_id?: string
  trade_pnl?: number | string
  [k: string]: unknown
}

export interface SourceResp {
  path: string | null
  exists: boolean
  source: string | null
}

export interface RunsResp {
  runs: RunInfo[]
}

export interface StrategyExportDiagnostic {
  severity: 'error' | 'warning' | 'info'
  code: string
  path?: string | null
  line?: number | null
  column?: number | null
  message: string
}

export interface StrategyExportReport {
  ready: boolean
  diagnostics: StrategyExportDiagnostic[]
  params: Record<string, unknown>
  filename?: string
  target_python?: string
  validation?: string
  syntax_checked?: boolean
  export_sha256?: string
  source_sha256?: string
  sources?: Record<string, unknown>[]
  [key: string]: unknown
}

export interface StrategyExportResp {
  source: string
  filename: string
  report: StrategyExportReport
}

export type StrategyExportFormat = 'python' | 'manifest'

export interface StrategyExportDownload {
  blob: Blob
  filename: string
}

// 数据管理API DTO，独立于既有策略导出类型。
export type DataOperation = 'sync' | 'fetch' | 'ingest' | 'verify'
export type DataDataset = 'daily_bars' | 'index_bars' | 'minute_bars' | 'minute_bars_5m' | 'corporate_actions' | 'instruments' | 'trading_calendar' | 'adj_factors' | 'index_constituents' | 'trading_status'
export type DataJobStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled'
export interface DataOverrides { start?: string; end?: string; symbols?: string[]; datasets?: DataDataset[] }
export interface DataProgress { percent?: number; current?: number; total?: number; message?: string }
export interface DataJob {
  id: string
  status: DataJobStatus
  operation: DataOperation
  phase?: string | null
  progress?: DataProgress | number | null
  started_at?: string | null
  finished_at?: string | null
  counts?: Record<string, unknown>
  errors?: unknown[]
  coverage?: unknown
  report?: Record<string, unknown> | null
  log_tail?: string[] | string
  [key: string]: unknown
}
export interface DataPublicConfig extends Record<string, unknown> {
  lake?: string; db?: string; start?: string; end?: string
  symbols?: string[]; datasets?: DataDataset[]; provider?: string
}
export interface DataBinary { path?: string | null; exists?: boolean; available?: boolean; version?: string | null; error?: string | null }
export interface DataStatus {
  config?: DataPublicConfig | null
  config_public?: DataPublicConfig | null
  binaries?: { fetch?: DataBinary; store?: DataBinary }
  database?: { path?: string; exists?: boolean; busy?: boolean; tables?: { name: string; rows?: number | null; start?: string | null; end?: string | null }[] }
  lake?: { exists?: boolean; datasets?: unknown }
  jobs?: DataJob[]
  errors?: unknown[]
  error?: string
  [key: string]: unknown
}
export interface DataPlan {
  ready?: boolean; config?: DataPublicConfig; steps?: unknown[]; missing?: unknown
  datasets?: DataDataset[]; symbols?: string[]; start?: string; end?: string
  will_download?: boolean; will_write?: boolean
  [key: string]: unknown
}
