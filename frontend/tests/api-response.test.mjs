import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

// Node.js 24+; no browser, API credentials, or real mailbox required.
let source = await readFile(new URL('../src/api.ts', import.meta.url), 'utf8')
source = source.replace('import.meta.env.VITE_API_BASE', 'undefined')
source = stripTypeScriptTypes(source, { mode: 'strip' }) + '\nexport { request };'
const { api, request } = await import('data:text/javascript;base64,' + Buffer.from(source).toString('base64'))

test('response bodies are consumed only once', async (t) => {
  const originalFetch = globalThis.fetch
  const originalWindow = globalThis.window
  try {
    for (const [status, body, expected] of [
      [500, 'Internal Server Error', 'Internal Server Error'],
      [400, JSON.stringify({ detail: 'INVALID_INPUT' }), 'INVALID_INPUT'],
      [422, JSON.stringify({ detail: [{ msg: 'invalid value' }] }), 'invalid value'],
      [502, '<html>Bad Gateway</html>', 'HTTP 502'],
      [503, '', 'HTTP 503'],
    ]) {
      await t.test(`error response ${status}`, async () => {
        globalThis.fetch = async () => new Response(body, { status })
        await assert.rejects(() => request('/test'), (error) => error.message.includes(expected))
      })
    }
    globalThis.fetch = async () => new Response(JSON.stringify({ id: 'ok' }), { status: 201 })
    assert.deepEqual(await request('/test'), { id: 'ok' })
    globalThis.fetch = async () => new Response(null, { status: 204 })
    assert.equal(await request('/test'), null)
    globalThis.fetch = async () => new Response('null', { status: 200 })
    assert.equal(await request('/test'), null)
    globalThis.window = { location: { href: '' } }
    globalThis.fetch = async () => new Response('', { status: 401 })
    await assert.rejects(() => request('/test'), /AUTH_REQUIRED/)
    assert.equal(globalThis.window.location.href, '/login')
  } finally {
    globalThis.fetch = originalFetch
    if (originalWindow === undefined) delete globalThis.window
    else globalThis.window = originalWindow
  }
})

test('message search preserves the legacy HTTP parameters and supports local cancellation', async () => {
  const originalFetch = globalThis.fetch
  const signal = new AbortController().signal
  try {
    globalThis.fetch = async (path, options) => {
      const url = new URL(path, 'http://example.test')
      assert.equal(url.pathname, '/api/mailboxes/test-id/messages')
      assert.deepEqual(Object.fromEntries(url.searchParams), {
        folder: 'inbox', q: 'query & filter', recipient: 'mail.box123+tag@gmail.com',
        page: '2', page_size: '100',
      })
      assert.equal(options.signal, signal)
      return Response.json({ total: 0, items: [] })
    }
    const result = await api.messages('test-id', 'inbox', 'query & filter', 'mail.box123+tag@gmail.com', 2, 100, signal)
    assert.deepEqual(result.items, [])
  } finally {
    globalThis.fetch = originalFetch
  }
})
