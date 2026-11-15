<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'
import { api, type MailboxPayload } from '../api'

const rows = ref<any[]>([])
const selected = ref<any | null>(null)
const messages = ref<any[]>([])
const folder = ref('all')
const q = ref('')
const recipient = ref('')
const page = ref(1)
const pageSize = 10
const total = ref(0)
const pageInput = ref('')
const detail = ref<any | null>(null)
const showAdd = ref(false)
const saving = ref(false)
const formError = ref('')
const form = reactive({
  address: '',
  username: '',
  imap_host: 'imap.gmail.com',
  imap_port: 993,
  app_password: '',
  folders: 'INBOX',
  proxy_mode: 'direct' as 'direct' | 'socks5' | 'socks5h',
  proxy_host: '',
  proxy_port: 1080,
  proxy_username: '',
  proxy_password: '',
})
const actionBusy = ref(false)
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))

function proxyStatusText(value: string | undefined) {
  return ({
    direct: '直连',
    proxy_checking: '正在检查代理',
    waiting_for_proxy: '等待代理',
    proxy_unavailable: '代理不可用',
    idle: '代理正常',
  } as Record<string, string>)[value || ''] || value || '未知'
}

function connectionStateText(value: string | undefined) {
  return ({
    stopped: '已停止',
    connecting: '连接中',
    idle: '同步中',
    reconnecting: '重新连接中',
    auth_failed: '认证失败',
  } as Record<string, string>)[value || ''] || value || '未知'
}

function folderRoleText(value: string | undefined, name: string) {
  return ({ inbox: '收件箱', spam: '垃圾邮件', other: name } as Record<string, string>)[value || ''] || name
}

function folderStateText(value: string | undefined) {
  return value === 'idle' ? '正常' : value === 'baseline_required' ? '初始化中' : value || '未知'
}

function errorText(value: string | undefined) {
  return ({
    imap_auth_failed: '邮箱认证失败，请检查 App Password',
    imap_connection_failed: 'IMAP 连接失败，正在重试',
    proxy_unavailable: '代理不可用，正在重试',
    proxy_auth_failed: '代理认证失败',
  } as Record<string, string>)[value || ''] || value || ''
}

function addressesText(value: unknown) {
  return Array.isArray(value) && value.length ? value.join(', ') : '未知收件人'
}

async function load() {
  try {
    rows.value = await api.mailboxes()
    if (rows.value.length && !selected.value) await select(rows.value[0])
    else if (selected.value) {
      const current = rows.value.find((row) => row.id === selected.value.id)
      if (current) selected.value = current
      else {
        selected.value = null
        messages.value = []
      }
    }
  } catch (error) {
    if ((error as Error).message !== 'AUTH_REQUIRED') throw error
  }
}

async function loadMessages() {
  if (!selected.value) return
  try {
    const result = await api.messages(selected.value.id, folder.value, q.value, recipient.value, page.value, pageSize)
    messages.value = result.items
    total.value = result.total
  } catch (error) {
    if ((error as Error).message !== 'AUTH_REQUIRED') throw error
  }
}

async function select(row: any) {
  selected.value = row
  page.value = 1
  await loadMessages()
}

async function search() {
  page.value = 1
  await loadMessages()
}

async function changePage(nextPage: number) {
  if (nextPage < 1 || nextPage > totalPages.value || nextPage === page.value) return
  page.value = nextPage
  await loadMessages()
}

async function jumpToPage() {
  const target = Number.parseInt(pageInput.value, 10)
  if (!Number.isInteger(target) || target < 1 || target > totalPages.value) return
  pageInput.value = ''
  await changePage(target)
}

async function refreshAfterAction() {
  await load()
  if (selected.value) {
    const row = rows.value.find((item) => item.id === selected.value.id)
    if (row) await select(row)
  }
}

async function startSelected() {
  if (!selected.value) return
  actionBusy.value = true
  try {
    await api.startMailbox(selected.value.id)
    await refreshAfterAction()
  } finally {
    actionBusy.value = false
  }
}

async function stopSelected() {
  if (!selected.value) return
  actionBusy.value = true
  try {
    await api.stopMailbox(selected.value.id)
    await refreshAfterAction()
  } finally {
    actionBusy.value = false
  }
}

async function deleteSelected() {
  if (!selected.value) return
  if (!window.confirm(`确定删除邮箱 ${selected.value.address} 吗？该邮箱的本地邮件和同步记录也会删除。`)) return
  actionBusy.value = true
  try {
    await api.deleteMailbox(selected.value.id)
    selected.value = null
    messages.value = []
    total.value = 0
    await load()
  } finally {
    actionBusy.value = false
  }
}

async function openMessage(item: any) {
  detail.value = await api.detail(item.id)
}

async function logout() {
  await api.logout()
  window.location.href = '/login'
}

function openCreate() {
  formError.value = ''
  showAdd.value = true
}

function closeCreate() {
  if (!saving.value) showAdd.value = false
}

async function createMailbox() {
  formError.value = ''
  if (!form.address.trim() || !form.username.trim() || !form.app_password.replace(/\s/g, '')) {
    formError.value = '请填写邮箱地址、用户名和 App Password'
    return
  }
  const payload: MailboxPayload = {
    address: form.address.trim(),
    username: form.username.trim(),
    imap_host: form.imap_host.trim() || 'imap.gmail.com',
    imap_port: Number(form.imap_port),
    app_password: form.app_password.replace(/\s/g, ''),
    proxy_mode: form.proxy_mode,
    proxy_host: form.proxy_mode !== 'direct' ? form.proxy_host.trim() || null : null,
    proxy_port: form.proxy_mode !== 'direct' ? Number(form.proxy_port) : null,
    proxy_username: form.proxy_mode !== 'direct' ? form.proxy_username.trim() || null : null,
    proxy_password: form.proxy_mode !== 'direct' ? form.proxy_password.trim() || null : null,
    proxy_dns_mode: form.proxy_mode === 'socks5h' ? 'remote' : form.proxy_mode === 'socks5' ? 'local' : null,
    folders: form.folders.split(',').map((name) => name.trim()).filter(Boolean),
  }
  saving.value = true
  try {
    const created = await api.createMailbox(payload)
    showAdd.value = false
    form.address = ''
    form.username = ''
    form.app_password = ''
    form.proxy_password = ''
    form.folders = 'INBOX'
    await load()
    const createdRow = rows.value.find((row) => row.id === created.id)
    if (createdRow) await select(createdRow)
  } catch (error) {
    formError.value = (error as Error).message || '添加邮箱失败'
  } finally {
    saving.value = false
  }
}

let refreshTimer: number | undefined
onMounted(async () => {
  await load()
  refreshTimer = window.setInterval(load, 3000)
})
onUnmounted(() => {
  if (refreshTimer !== undefined) window.clearInterval(refreshTimer)
})
</script>
<template>
  <main class="layout">
    <aside class="sidebar">
      <div class="sidebar-heading"><div><p class="eyebrow">账户列表</p><h2>邮箱账户</h2></div><button class="icon-button" title="添加邮箱账户" @click="openCreate">＋</button></div>
      <button v-for="row in rows" :key="row.id" class="mailbox-item" :class="{ active: selected?.id === row.id }" @click="select(row)">
        <strong>{{ row.address }}</strong><small>{{ proxyStatusText(row.proxy_status) }} / {{ connectionStateText(row.connection_state) }}</small>
      </button>
      <p v-if="!rows.length" class="sidebar-empty">还没有邮箱账户，点击右上角添加。</p>
    </aside>
    <section class="content">
      <header class="page-header">
        <div><p class="eyebrow">邮箱管理</p><h1>{{ selected?.address || '邮箱管理' }}</h1><span v-if="selected">代理：{{ proxyStatusText(selected.proxy_status) }}　同步：{{ connectionStateText(selected.connection_state) }}</span><p v-if="selected?.last_error_code" class="error">{{ errorText(selected.last_error_code) }}</p></div>
        <div class="header-actions"><button v-if="selected && !selected.enabled" class="primary" :disabled="actionBusy" @click="startSelected">启动同步</button><button v-if="selected?.enabled" class="secondary" :disabled="actionBusy" @click="stopSelected">停止同步</button><button v-if="selected" class="danger" :disabled="actionBusy" @click="deleteSelected">删除邮箱</button><button class="secondary" @click="openCreate">添加邮箱</button><button class="secondary" @click="logout">登出</button></div>
      </header>
      <div v-if="selected" class="status"><span v-for="f in selected.folders" :key="f.name">{{ folderRoleText(f.role, f.name) }}：{{ folderStateText(f.last_success_at ? 'idle' : 'baseline_required') }}<em v-if="f.last_error_code">（{{ errorText(f.last_error_code) }}）</em></span></div>
      <div v-if="selected" class="filters"><select v-model="folder" @change="search"><option value="all">全部邮件</option><option value="inbox">收件箱</option><option value="spam">垃圾邮件</option></select><input v-model="q" @keyup.enter="search" placeholder="搜索主题、发件人或正文"><input v-model="recipient" @keyup.enter="search" placeholder="按收件人筛选，例如 xxx.111@gmail.com"><button class="secondary" @click="search">搜索</button></div>
      <div v-if="!selected" class="empty-state"><h2>添加第一个邮箱账户</h2><p>每个邮箱独立保存 IMAP、文件夹和代理配置。</p><button class="primary" @click="openCreate">添加邮箱</button></div>
      <article v-for="item in messages" :key="item.id" class="message-card" @click="openMessage(item)"><h3>{{ item.subject || '(无主题)' }}</h3><p>发件人：{{ item.from_address || '未知发件人' }} · {{ item.received_at || '时间未知' }}</p><p>收件人：{{ addressesText(item.to_addresses) }}</p><p>{{ item.preview }}</p></article>
      <p v-if="selected && !messages.length" class="empty-state">没有找到邮件</p>
      <div v-if="selected && total" class="pagination"><button class="secondary" :disabled="page <= 1" @click="changePage(1)">首页</button><button class="secondary" :disabled="page <= 1" @click="changePage(page - 1)">上一页</button><span>第 {{ page }} / {{ totalPages }} 页，共 {{ total }} 封</span><button class="secondary" :disabled="page >= totalPages" @click="changePage(page + 1)">下一页</button><button class="secondary" :disabled="page >= totalPages" @click="changePage(totalPages)">尾页</button><label class="page-jump">跳转到 <input v-model="pageInput" type="number" min="1" :max="totalPages" @keyup.enter="jumpToPage"><button class="secondary" @click="jumpToPage">跳转</button></label></div>
      <article v-if="detail" class="message-detail"><button class="secondary" @click="detail = null">关闭邮件</button><h2>{{ detail.subject || '(无主题)' }}</h2><p>发件人：{{ detail.from_address || '未知发件人' }}</p><p>收件人：{{ addressesText(detail.to_addresses) }}</p><p v-if="detail.cc_addresses?.length">抄送：{{ addressesText(detail.cc_addresses) }}</p><div v-if="detail.html_body_sanitized" class="mail-body" v-html="detail.html_body_sanitized"></div><pre v-else>{{ detail.text_body || '邮件没有正文' }}</pre></article>
    </section>

    <div v-if="showAdd" class="modal-backdrop" @click.self="closeCreate">
      <form class="modal" @submit.prevent="createMailbox">
        <div class="modal-header"><div><p class="eyebrow">新增账户</p><h2>添加邮箱账户</h2></div><button type="button" class="icon-button" title="关闭" @click="closeCreate">×</button></div>
        <p class="muted">邮箱密码只提交给受保护的 API，不会保存到浏览器 Local Storage。</p>
        <div class="form-grid"><label>邮箱地址<input v-model="form.address" type="email" autocomplete="off" placeholder="mailbox@example.com"></label><label>邮箱用户名<input v-model="form.username" autocomplete="off" placeholder="通常与邮箱地址相同"></label><label>IMAP 服务器<input v-model="form.imap_host" autocomplete="off"></label><label>IMAP 端口<input v-model.number="form.imap_port" type="number" min="1" max="65535"></label></div>
        <label>Gmail 应用专用密码<input v-model="form.app_password" type="password" autocomplete="new-password" placeholder="填写 Gmail App Password"><small>只提交给 API，服务端加密保存，不写入浏览器。</small></label>
        <label>监控文件夹<input v-model="form.folders" autocomplete="off" placeholder="可留空，首次连接会自动发现全部文件夹"><small>首次连接会自动发现并监控 Gmail LIST 返回的全部文件夹。</small></label>
        <label>代理模式<select v-model="form.proxy_mode"><option value="direct">直连</option><option value="socks5">SOCKS5（本地 DNS）</option><option value="socks5h">SOCKS5h（代理 DNS）</option></select></label>
        <div v-if="form.proxy_mode !== 'direct'" class="form-grid"><label>代理主机<input v-model="form.proxy_host" autocomplete="off" placeholder="例如 127.0.0.1"></label><label>代理端口<input v-model.number="form.proxy_port" type="number" min="1" max="65535"></label><label>代理用户名<input v-model="form.proxy_username" autocomplete="off"></label><label>代理密码<input v-model="form.proxy_password" type="password" autocomplete="new-password"></label></div>
        <p v-if="formError" class="error">{{ formError }}</p><div class="modal-actions"><button type="button" class="secondary" @click="closeCreate">取消</button><button class="primary" type="submit" :disabled="saving">{{ saving ? '保存中…' : '保存邮箱' }}</button></div>
      </form>
    </div>
  </main>
</template>
