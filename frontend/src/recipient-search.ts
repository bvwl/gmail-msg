import type { MessagePage, MessageSummary } from './api'

const gmailSuffix = '@gmail.com'
const normalize = (address: string) => address.trim().toLowerCase()

export function canIncludeDottedAliases(recipient: string) {
  return /^[a-z0-9]+@gmail\.com$/.test(normalize(recipient))
}

export async function collectRecipientMatches(
  readPage: (page: number, pageSize: number) => Promise<MessagePage>,
  recipient: string,
  includeDottedAliases: boolean,
): Promise<MessageSummary[]> {
  const target = normalize(recipient)
  const includeAliases = includeDottedAliases && canIncludeDottedAliases(target)
  const matches = new Map<string, MessageSummary>()
  const matchesAddress = (address: string) => {
    const value = normalize(address)
    if (value === target) return true
    if (!includeAliases || !value.endsWith(gmailSuffix)) return false
    return value.slice(0, -gmailSuffix.length).replaceAll('.', '') + gmailSuffix === target
  }

  // The legacy API paginates candidates, not exact matches. Filter before UI pagination.
  const requestedPageSize = 100
  for (let page = 1; ; page++) {
    const result = await readPage(page, requestedPageSize)
    for (const item of result.items) {
      if (item.to_addresses?.some(matchesAddress)) matches.set(item.id, item)
    }
    if (result.items.length < result.page_size || page * result.page_size >= result.total) break
  }
  return [...matches.values()]
}
