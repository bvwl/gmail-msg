import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { stripTypeScriptTypes } from 'node:module'
import test from 'node:test'

const source = await readFile(new URL('../src/recipient-search.ts', import.meta.url), 'utf8')
const code = stripTypeScriptTypes(source, { mode: 'strip' })
const { canIncludeDottedAliases, collectRecipientMatches } = await import(
  'data:text/javascript;base64,' + Buffer.from(code).toString('base64')
)

const candidates = [
  { id: 'alias', to_addresses: ['other@example.test', 'mail.box123@GMAIL.COM'] },
  { id: 'other-domain', to_addresses: ['mailbox123@gmailcom'] },
  { id: 'plain', to_addresses: [' MAILBOX123@gmail.com '] },
  { id: 'tagged', to_addresses: ['mail.box123+tag@gmail.com'] },
  { id: 'alias', to_addresses: ['mail.box123@gmail.com'] },
  { id: 'substring', to_addresses: ['othermailbox123@gmail.com'] },
  { id: 'no-address', to_addresses: [] },
]

function pagedCandidates(requests = []) {
  return async (page, requestedSize) => {
    requests.push([page, requestedSize])
    const pageSize = 2
    const start = (page - 1) * pageSize
    return {
      page, page_size: pageSize, total: candidates.length,
      items: candidates.slice(start, start + pageSize),
    }
  }
}

test('exact search scans past a page with no exact matches and respects the API page limit', async () => {
  const requests = []
  const exact = await collectRecipientMatches(pagedCandidates(requests), ' Mailbox123@gmail.com ', false)
  assert.deepEqual(exact.map((item) => item.id), ['plain'])
  assert.deepEqual(requests, [[1, 100], [2, 100], [3, 100], [4, 100]])
})

test('opt-in matches Gmail dot aliases only and does not duplicate messages', async () => {
  const expanded = await collectRecipientMatches(pagedCandidates(), 'mailbox123@gmail.com', true)
  assert.deepEqual(expanded.map((item) => item.id), ['alias', 'plain'])
  const dotted = await collectRecipientMatches(pagedCandidates(), 'mail.box123@gmail.com', true)
  assert.deepEqual(dotted.map((item) => item.id), ['alias'])
  for (const recipient of ['mail.box123@gmail.com', 'mailbox123@example.test', 'mailbox123+tag@gmail.com', '']) {
    assert.equal(canIncludeDottedAliases(recipient), false)
  }
  assert.equal(canIncludeDottedAliases(' Mailbox123@GMAIL.COM '), true)
})

test('a failed later page does not produce a partial result', async () => {
  await assert.rejects(collectRecipientMatches(async (page, size) => {
    if (page === 2) throw Error('PAGE_FAILED')
    return pagedCandidates()(page, size)
  }, 'mail.box123@gmail.com', false), /PAGE_FAILED/)
})
