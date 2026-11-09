import { createApp } from 'vue'
import { createRouter, createWebHistory } from 'vue-router'
import App from './App.vue'
import Login from './views/Login.vue'
import Mailboxes from './views/Mailboxes.vue'
import './style.css'

const apiBase = import.meta.env.VITE_API_BASE || ''

const router = createRouter({
  history: createWebHistory(),
  routes: [{ path: '/login', component: Login }, { path: '/', component: Mailboxes, meta: { requiresAuth: true } }],
})
router.beforeEach(async (to) => {
  if (!to.meta.requiresAuth) return true
  try {
    const response = await fetch(`${apiBase}/api/config/frontend`, { credentials: 'include' })
    return response.status === 401 ? '/login' : true
  } catch {
    return '/login'
  }
})
createApp(App).use(router).mount('#app')
