import type { RunDetail, SeriesData } from '../api/types'

function sameArray<T>(left: T[], right: T[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index])
}
function sameSeries(left: SeriesData, right: SeriesData): boolean {
  return sameArray(left.dates, right.dates) && sameArray(left.equity, right.equity)
    && sameArray(left.drawdown, right.drawdown) && sameArray(left.bench, right.bench)
}
function keep<T>(previous: T, incoming: T): T {
  return JSON.stringify(previous) === JSON.stringify(incoming) ? previous : incoming
}

/** O(points + metadata bytes): retain unchanged chart inputs, not an incomplete revision key. */
export function mergeRunDetail(previous: RunDetail | null, incoming: RunDetail): RunDetail {
  if (!previous || previous.name !== incoming.name) return incoming
  return {
    ...incoming,
    series: sameSeries(previous.series, incoming.series) ? previous.series : incoming.series,
    summary: keep(previous.summary, incoming.summary),
    monthly_ext: keep(previous.monthly_ext, incoming.monthly_ext),
    alpha_beta: keep(previous.alpha_beta, incoming.alpha_beta),
    logs: sameArray(previous.logs, incoming.logs) ? previous.logs : incoming.logs,
  }
}
