<script setup lang="ts">
import { ref } from 'vue'
import { useRouter } from 'vue-router'
import { api } from '../api'

const username = ref('')
const password = ref('')
const error = ref('')
const submitting = ref(false)
const router = useRouter()

async function submit() {
  error.value = ''
  if (!username.value.trim() || !password.value) {
    error.value = '请输入管理员用户名和密码'
    return
  }
  submitting.value = true
  try {
    await api.login(username.value.trim(), password.value)
    password.value = ''
    await router.push('/')
  } catch {
    error.value = '用户名或密码错误'
  } finally {
    submitting.value = false
  }
}
</script>
<template>
  <main class="login">
    <form class="login-card" @submit.prevent="submit">
      <div>
        <p class="eyebrow">邮箱同步管理</p>
        <h1>管理员登录</h1>
        <p class="muted">登录后管理多个邮箱账户和邮件同步状态。</p>
      </div>
      <label>用户名<input v-model="username" autocomplete="username" placeholder="管理员用户名"></label>
      <label>密码<input v-model="password" type="password" autocomplete="current-password" placeholder="管理员密码"></label>
      <button class="primary" type="submit" :disabled="submitting">{{ submitting ? '登录中…' : '登录' }}</button>
      <p v-if="error" class="error">{{ error }}</p>
    </form>
  </main>
</template>
