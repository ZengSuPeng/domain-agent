import axios from 'axios'
import { ref } from 'vue'

// 登录态（飞书 OAuth）：
// 后端签发 HMAC 签名的 session token，前端存 localStorage 并按
// `Authorization: Bearer <token>` 携带。跨域部署（Cloudflare Pages -> Render）
// 下第三方 Cookie 不可靠，所以不用 Cookie。
const baseURL = import.meta.env.VITE_API_BASE_URL || '/api'

const TOKEN_KEY = 'domain_agent_token'
const USER_KEY = 'domain_agent_user'

// 全局响应式登录态，App.vue / LoginPage.vue 共用
export const currentUser = ref(readStoredUser())
export const authEnabled = ref(true)
export const feishuConfigured = ref(true)
export const sessionExpiresAt = ref(0)

// 登录相关请求单独放宽超时：Render 免费实例休眠后首个请求要 30~60s 才被唤醒
const authClient = axios.create({ baseURL, timeout: 90000 })

function safeGet(key) {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function safeSet(key, value) {
  try {
    localStorage.setItem(key, value)
  } catch {
    // 隐私模式/存储被禁用：退化为仅当前页有效
  }
}

function safeRemove(key) {
  try {
    localStorage.removeItem(key)
  } catch {
    // ignore
  }
}

function readStoredUser() {
  try {
    return JSON.parse(safeGet(USER_KEY) || 'null')
  } catch {
    return null
  }
}

export function getToken() {
  return safeGet(TOKEN_KEY) || ''
}

export function hasStoredSession() {
  return !!getToken() && !!currentUser.value
}

export function authHeaders() {
  const token = getToken()
  return token ? { Authorization: `Bearer ${token}` } : {}
}

export function setSession(token, user, expiresAt = 0) {
  if (token) safeSet(TOKEN_KEY, token)
  if (user) {
    safeSet(USER_KEY, JSON.stringify(user))
    currentUser.value = user
  }
  sessionExpiresAt.value = Number(expiresAt) || 0
}

export function clearSession() {
  safeRemove(TOKEN_KEY)
  safeRemove(USER_KEY)
  currentUser.value = null
  sessionExpiresAt.value = 0
}

/**
 * 查询后端登录态。
 * - auth_enabled=false：后端没启用登录，直接返回本地调试用户
 * - authenticated=false：未登录/已过期，清掉本地缓存
 */
export async function fetchMe() {
  const { data } = await authClient.get('/auth/me', { headers: authHeaders() })
  authEnabled.value = data?.auth_enabled !== false
  feishuConfigured.value = data?.feishu_configured !== false

  if (data?.auth_enabled === false) {
    currentUser.value = data.user || { name: '本地开发' }
    return { ...data, authenticated: true }
  }
  if (data?.authenticated && data.user) {
    currentUser.value = data.user
    if (data.expires_at) sessionExpiresAt.value = data.expires_at
    return data
  }

  clearSession()
  return data
}

/**
 * 获取飞书授权页地址（需要整页跳转，授权页自带扫码/账密登录）。
 */
export async function getFeishuLoginUrl() {
  const redirect = `${window.location.origin}${window.location.pathname}`
  const { data } = await authClient.get('/auth/feishu/url', { params: { redirect } })
  if (!data?.success || !data.data?.url) {
    throw new Error(data?.message || '获取飞书登录地址失败')
  }
  return data.data.url
}

/**
 * 用回调带回的一次性 ticket 换正式 session token。
 */
export async function exchangeTicket(ticket) {
  const { data } = await authClient.post('/auth/feishu/exchange', { ticket })
  if (data?.success && data.data) {
    setSession(data.data.token, data.data.user, data.data.expires_at)
  }
  return data
}

export async function logout() {
  try {
    await authClient.post('/auth/logout', null, { headers: authHeaders(), timeout: 10000 })
  } catch {
    // 无状态 token：即使请求失败，本地清掉也算退出
  }
  clearSession()
}
