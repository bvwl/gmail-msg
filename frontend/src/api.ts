const base = import.meta.env.VITE_API_BASE || ''

async function request(path: string, options: RequestInit = {}) {
  const response = await fetch(`${base}${path}`, {
    ...options,
    credentials: 'include',
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  })
  if (response.status === 401) {
    window.location.href = '/login'
    throw new Error('AUTH_REQUIRED')
  }
  const text = response.status === 204 ? '' : await response.text()
  let body: unknown = null
  if (text) {
    try { body = JSON.parse(text) } catch { body = text }
  }
  if (!response.ok) {
    const detail = body && typeof body === 'object' && 'detail' in body
      ? (body as { detail: unknown }).detail : body
    let message = `请求失败（HTTP ${response.status}）`
    if (typeof detail === 'string' && detail.trim()) {
      message = detail.startsWith('<') ? message : detail
    } else if (detail && typeof detail === 'object') {
      message = 'message' in detail && typeof detail.message === 'string'
        ? detail.message : JSON.stringify(detail)
    }
    throw new Error(message)
  }
  return body
}

export type MailboxPayload = {
  address: string
  imap_host: string
  imap_port: number
  username: string
  app_password: string
  proxy_mode: 'direct' | 'socks5' | 'socks5h'
  proxy_host: string | null
  proxy_port: number | null
  proxy_username: string | null
  proxy_password: string | null
  proxy_dns_mode: 'local' | 'remote' | null
  folders: string[]
}

export const api = {
  login: (username: string, password: string) =>
    request('/api/admin/login', { method: 'POST', body: JSON.stringify({ username, password }) }),
  logout: () => request('/api/admin/logout', { method: 'POST' }),
  mailboxes: () => request('/api/mailboxes'),
  createMailbox: (payload: MailboxPayload) =>
    request('/api/mailboxes', { method: 'POST', body: JSON.stringify(payload) }),
  startMailbox: (id: string) => request(`/api/mailboxes/${id}/start`, { method: 'POST' }),
  stopMailbox: (id: string) => request(`/api/mailboxes/${id}/stop`, { method: 'POST' }),
  deleteMailbox: (id: string) => request(`/api/mailboxes/${id}`, { method: 'DELETE' }),
  messages: (id: string, folder = 'all', q = '', recipient = '', page = 1, pageSize = 10) =>
    request(`/api/mailboxes/${id}/messages?folder=${folder}&q=${encodeURIComponent(q)}&recipient=${encodeURIComponent(recipient)}&page=${page}&page_size=${pageSize}`),
  detail: (id: string) => request(`/api/messages/${id}`),
  codes: (id: string) => request(`/api/mailboxes/${id}/codes?include_value=true`),
}
