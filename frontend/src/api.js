import axios from 'axios'

// 生产环境用环境变量指定后端地址，开发环境走 Vite 代理 (/api -> 127.0.0.1:8001)
const baseURL = import.meta.env.VITE_API_BASE_URL || '/api'

const request = axios.create({
  baseURL,
  timeout: 300000,
})

/**
 * 非流式生成域名（已废弃，推荐用 generateDomainStream）
 */
export async function generateDomain(theme, desc) {
  const { data } = await request.post('/generate', { theme, desc })
  return data
}

/**
 * 流式生成域名，通过回调实时返回思考过程和最终结果
 * @param {string} theme - 主题
 * @param {string} desc - 风格描述
 * @param {Object} callbacks
 * @param {(text: string) => void} callbacks.onThinking - 思考片段回调（增量）
 * @param {(domains: Array) => void} callbacks.onDone - 最终域名列表回调
 * @param {(msg: string) => void} callbacks.onError - 错误回调
 * @param {AbortSignal} signal - 取消信号
 * @param {Array<string>} exclude - 需要避开的不可用域名列表
 */
export async function generateDomainStream(theme, desc, callbacks, signal, exclude = []) {
  const resp = await fetch(`${baseURL}/generate/stream`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ theme, desc, exclude }),
    signal,
  })

  if (!resp.ok) {
    callbacks.onError?.(`HTTP ${resp.status}`)
    return
  }

  const reader = resp.body.getReader()
  const decoder = new TextDecoder('utf-8')
  let buffer = ''
  let currentEvent = ''

  while (true) {
    const { done, value } = await reader.read()
    if (done) {
      console.log('[SSE] 流结束，最后 buffer:', JSON.stringify(buffer))
      break
    }
    buffer += decoder.decode(value, { stream: true })

    let idx
    while ((idx = buffer.indexOf('\n')) >= 0) {
      const line = buffer.slice(0, idx).trim()
      buffer = buffer.slice(idx + 1)
      if (!line) continue
      if (line.startsWith('event:')) {
        currentEvent = line.slice(6).trim()
      } else if (line.startsWith('data:')) {
        const raw = line.slice(5).trim()
        if (!raw) continue
        let payload
        try {
          payload = JSON.parse(raw)
        } catch {
          payload = raw
        }
        if (currentEvent === 'thinking') {
          callbacks.onThinking?.(payload)
        } else if (currentEvent === 'done') {
          console.log('[SSE] 触发 onDone, payload 类型:', typeof payload, '长度:', Array.isArray(payload) ? payload.length : 'N/A')
          callbacks.onDone?.(payload || [])
        } else if (currentEvent === 'error') {
          callbacks.onError?.(payload)
        }
        currentEvent = ''
      }
    }
  }
}

/**
 * 域名可注册性检测（Cloudflare）
 */
export async function checkDomains(domains, signal) {
  console.log('[checkDomains] 发起请求，域名列表：', domains)
  const { data } = await request.post('/check-domains', { domains }, { signal })
  console.log('[checkDomains] 响应：', data)
  return data
}

/**
 * 域名合规性及侵权风险检测（GPT-4o-mini）
 * @param {Array} domains - [{domain, reason}, ...]
 */
export async function checkCompliance(domains, signal) {
  console.log('[checkCompliance] 发起请求，域名：', domains)
  const { data } = await request.post('/check-compliance', { domains }, { signal })
  console.log('[checkCompliance] 响应：', data)
  return data
}

/**
 * 域名历史使用痕迹检测（全量，已不推荐：慢源会阻塞整批）
 * @param {Array<string>} domains - 域名列表
 * @param {AbortSignal} signal
 */
export async function checkHistory(domains, signal) {
  console.log('[checkHistory] 发起请求，域名：', domains)
  const { data } = await request.post('/check-history', { domains }, { signal })
  console.log('[checkHistory] 响应：', data)
  return data
}

// 每个源对应独立后端端点，前端 Network 面板可直接看到 4 个不同 URL 的请求
const SOURCE_ENDPOINTS = {
  wayback: '/check-wayback',
  virustotal: '/check-virustotal',
  trustpilot: '/check-trustpilot',
  gridinsoft: '/check-gridinsoft',
}

/**
 * 单源历史痕迹检测：每个源打到独立 URL，慢源只阻塞自己那一列，
 * 其他源照常返回并即时渲染，不再「一个失败全部卡死」。
 * @param {Array<string>} domains - 域名列表
 * @param {string} source - 源名：wayback / trustpilot / gridinsoft / virustotal
 * @param {AbortSignal} signal
 */
export async function checkHistorySource(domains, source, signal) {
  const path = SOURCE_ENDPOINTS[source]
  if (!path) throw new Error(`未知的历史检测源: ${source}`)
  const { data } = await request.post(path, { domains }, { signal, timeout: 150000 })
  return data
}

/**
 * 查询 DeepSeek 账户余额
 * @param {AbortSignal} [signal] - 取消信号
 */
export async function getDeepSeekBalance(signal) {
  const { data } = await request.get('/deepseek-balance', { signal, timeout: 20000 })
  return data
}

/**
 * 查询 DMXAPI（模型中转平台）账户余额
 * @param {AbortSignal} [signal] - 取消信号
 */
export async function getDmxapiBalance(signal) {
  const { data } = await request.get('/dmxapi-balance', { signal, timeout: 20000 })
  return data
}

/**
 * 获取不可用域名库列表
 * @param {AbortSignal} [signal] - 取消信号
 */
export async function getUnavailableDomains(signal) {
  const { data } = await request.get('/unavailable-domains', { signal, timeout: 20000 })
  return data
}

/**
 * 批量添加域名到不可用域名库
 */
export async function addUnavailableDomains(items) {
  const { data } = await request.post('/unavailable-domains', { items })
  return data
}

/**
 * 从不可用域名库移除单个域名
 */
export async function removeUnavailableDomain(domain) {
  const { data } = await request.delete(`/unavailable-domains/${domain}`)
  return data
}
