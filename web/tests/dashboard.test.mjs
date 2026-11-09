import assert from 'node:assert/strict'
import test from 'node:test'
import { RequestSlot } from '../src/lib/requestSlot.ts'
import { mergeRunDetail } from '../src/lib/runDetail.ts'
import { binaryStatusLabel, displayLocalPath, hideLocalPaths } from '../src/lib/dataDisplay.ts'
import { api, ApiError } from '../src/api/client.ts'
import { readFileSync } from 'node:fs'
import { product } from '../src/lib/product.ts'
import { THEME_STORAGE_KEY } from '../src/lib/theme.ts'

const makeDetail = (name = 'synthetic-run') => ({
  name, status: 'done', progress: { phase: 'finished', day_done: 2 },
  summary: { total_return: 0.1, final_value: 110000, trade_count: 1 },
  series: { dates: ['2025-01-02', '2025-01-03'], equity: [100000, 110000], drawdown: [0, 0], bench: [3500, 3510] },
  logs: ['first', 'second'], monthly_ext: { '2025-01': { ret: 0.1 } }, alpha_beta: { beta: 1, alpha: 0.01 },
})
const clone = value => JSON.parse(JSON.stringify(value))

test('independent project identity matches its package name and version', () => {
  const pkg = JSON.parse(readFileSync(new globalThis.URL('../package.json', import.meta.url), 'utf8'))
  assert.equal(pkg.name, 'ptrade-local-backtest-web')
  assert.equal(pkg.version, product.version)
  assert.equal(product.name, 'ptrade 本地回测框架')
  assert.equal(product.repository, 'ptrade-local-backtest')
  assert.equal(product.firstDeveloper, 'JonePoon')
  assert.equal(product.repositoryUrl, 'https://github.com/JonePoon/ptrade-local-backtest')
})
test('HTML carries the new Chinese brand and responsive viewport', () => {
  const html = readFileSync(new globalThis.URL('../index.html', import.meta.url), 'utf8')
  assert.ok(html.includes('<title>' + product.name + '</title>'))
  assert.ok(html.includes('width=device-width, initial-scale=1.0'))
  assert.ok(html.includes('第一开发者 ' + product.firstDeveloper))
})
test('local theme preferences use an independent product key', () => {
  assert.equal(THEME_STORAGE_KEY, 'ptrade-local-backtest-theme')
  assert.notEqual(THEME_STORAGE_KEY, 'dsh-theme')
})

test('starting a new request aborts and invalidates the old owner', () => {
  const slot = new RequestSlot(), first = slot.begin(), second = slot.begin()
  assert.equal(first.signal.aborted, true)
  assert.equal(slot.current(first), false)
  assert.equal(slot.current(second), true)
})
test('unmount cancellation removes the response owner', () => {
  const slot = new RequestSlot(), controller = slot.begin()
  slot.cancel()
  assert.equal(controller.signal.aborted, true)
  assert.equal(slot.current(controller), false)
})
test('a late response that ignores AbortSignal cannot replace the current run', async () => {
  const slot = new RequestSlot(), stale = slot.begin()
  let resolveOld
  const oldResult = new Promise(resolve => { resolveOld = resolve })
  let displayed = 'new-run'
  const oldTask = oldResult.then(value => { if (slot.current(stale)) displayed = value })
  slot.begin()
  resolveOld('old-run')
  await oldTask
  assert.equal(displayed, 'new-run')
})
test('identical poll data retains expensive chart and metadata references', () => {
  const previous = makeDetail(), merged = mergeRunDetail(previous, clone(previous))
  assert.equal(merged.series, previous.series)
  assert.equal(merged.summary, previous.summary)
  assert.equal(merged.monthly_ext, previous.monthly_ext)
  assert.equal(merged.logs, previous.logs)
})
test('equal-length log tails update when their contents change', () => {
  const previous = makeDetail(), incoming = clone(previous)
  incoming.logs = ['second', 'third']
  const merged = mergeRunDetail(previous, incoming)
  assert.deepEqual(merged.logs, ['second', 'third'])
  assert.equal(merged.series, previous.series)
})
test('same-size series corrections are not hidden by a length-only revision', () => {
  const previous = makeDetail(), incoming = clone(previous)
  incoming.series.equity[1] = 111000
  const merged = mergeRunDetail(previous, incoming)
  assert.equal(merged.series.equity[1], 111000)
  assert.notEqual(merged.series, previous.series)
})
test('metadata-only corrections are applied', () => {
  const previous = makeDetail(), incoming = clone(previous)
  incoming.summary.final_value = 111000
  incoming.alpha_beta.alpha = 0.02
  const merged = mergeRunDetail(previous, incoming)
  assert.equal(merged.summary.final_value, 111000)
  assert.equal(merged.alpha_beta.alpha, 0.02)
})
test('run changes never reuse the previous run response', () => {
  const previous = makeDetail(), incoming = makeDetail('next-run')
  assert.equal(mergeRunDetail(previous, incoming), incoming)
  assert.equal(mergeRunDetail(null, incoming), incoming)
})
test('progress-only updates retain unchanged chart inputs', () => {
  const previous = makeDetail(), incoming = clone(previous)
  incoming.progress.day_done = 3
  const merged = mergeRunDetail(previous, incoming)
  assert.equal(merged.progress.day_done, 3)
  assert.equal(merged.series, previous.series)
})
test('Windows paths are hidden without changing their basename', () => {
  assert.equal(displayLocalPath(String.raw`C:\demo\private\demo.duckdb`, true), '…/demo.duckdb')
})
test('Unix paths and trailing separators are presented consistently', () => {
  assert.equal(displayLocalPath('/demo/private/lake/', true), '…/lake')
  assert.equal(displayLocalPath('/demo/data.duckdb', false), '/demo/data.duckdb')
  assert.equal(displayLocalPath(undefined, true), '路径未知')
})
test('nested privacy rendering never mutates API data', () => {
  const source = { db: 'D:/demo/private/data.duckdb', steps: [{ path: '/demo/private/lake' }], symbol: '000001.SZ', enabled: true }
  const hidden = hideLocalPaths(source)
  assert.deepEqual(hidden, { db: '…/data.duckdb', steps: [{ path: '…/lake' }], symbol: '000001.SZ', enabled: true })
  assert.equal(source.db, 'D:/demo/private/data.duckdb')
})
test('doctor availability is distinct from file presence', () => {
  assert.equal(binaryStatusLabel({ exists: true, available: false }), '文件已发现 · 未就绪')
  assert.equal(binaryStatusLabel({ exists: true, available: true }), '运行验证通过')
})
test('missing and unobserved binary states remain distinct', () => {
  assert.equal(binaryStatusLabel({ exists: false, available: false }), '未发现')
  assert.equal(binaryStatusLabel({ path: 'unverified.exe' }), '状态未知')
  assert.equal(binaryStatusLabel(undefined), '状态未知')
})

test('Python download rechecks the latest blocked snapshot at the server', async context => {
  const report = { ready: false, params: {}, diagnostics: [{ severity: 'error', code: 'snapshot_digest_mismatch', message: 'changed snapshot' }] }
  context.mock.method(globalThis, 'fetch', async () => new globalThis.Response(JSON.stringify({ detail: { message: 'blocked', report } }), { status: 422 }))
  await assert.rejects(api.downloadExport('synthetic-run', 'python'), error => error instanceof ApiError && error.status === 422 && error.report.ready === false)
})
test('UTF-8 download filenames and exact source bytes come from the response', async context => {
  context.mock.method(globalThis, 'fetch', async () => new globalThis.Response('print("synthetic")\n', { headers: { 'Content-Disposition': "attachment; filename=\"fallback.py\"; filename*=UTF-8''%E5%90%88%E6%88%90.py" } }))
  const result = await api.downloadExport('synthetic-run', 'python')
  assert.equal(result.filename, '合成.py')
  assert.equal(await result.blob.text(), 'print("synthetic")\n')
})
test('a successful preview cannot bypass a later rejected download', async context => {
  const calls = []
  context.mock.method(globalThis, 'fetch', async (url, options) => {
    calls.push({ url, cache: options.cache })
    return calls.length === 1
      ? new globalThis.Response(JSON.stringify({ filename: 'demo.py', source: 'demo', report: { ready: true, params: {}, diagnostics: [] } }))
      : new globalThis.Response(JSON.stringify({ detail: { message: 'not ready', report: { ready: false, params: {}, diagnostics: [] } } }), { status: 422 })
  })
  assert.equal((await api.exportStrategy('synthetic-run')).report.ready, true)
  await assert.rejects(api.downloadExport('synthetic-run', 'python'), error => error.status === 422)
  assert.equal(calls.length, 2)
  assert.ok(calls.every(call => call.cache === 'no-store'))
  assert.ok(calls[1].url.endsWith('format=python'))
})
test('malformed encoded filenames retain the response fallback', async context => {
  context.mock.method(globalThis, 'fetch', async () => new globalThis.Response('demo', { headers: { 'Content-Disposition': "attachment; filename=\"fallback.py\"; filename*=UTF-8''%INVALID" } }))
  assert.equal((await api.downloadExport('synthetic-run', 'python')).filename, 'fallback.py')
})
test('detail requests forward the current AbortSignal to fetch', async context => {
  const controller = new globalThis.AbortController()
  let seen
  context.mock.method(globalThis, 'fetch', async (_url, options) => { seen = options.signal; return new globalThis.Response(JSON.stringify(makeDetail())) })
  await api.detail('synthetic-run', controller.signal)
  assert.equal(seen, controller.signal)
})
