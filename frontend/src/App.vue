<template>
  <el-card>
    <template #header>
      <div class="app-title">🌐 汉盈域名检测智能体</div>
    </template>

    <DomainForm :loading="loading" @generate="handleGenerate" @abort="handleAbort" />

    <ThinkingBox v-if="thinking" :content="thinking" :auto-expand="loading" />

    <!-- 进度提示：正在检测的域名与源 -->
    <el-alert
      v-if="progressText"
      :title="progressText"
      type="info"
      :closable="false"
      show-icon
      class="progress-alert"
    />

    <el-divider v-if="domains.length > 0" />

    <DomainTable v-if="domains.length > 0" :domains="domains" :checking="checking" :compliance-checking="complianceChecking" :history-checking="historyChecking" />

    <el-empty v-if="!loading && domains.length === 0" description="还没有生成结果" />
  </el-card>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import DomainForm from './components/DomainForm.vue'
import ThinkingBox from './components/ThinkingBox.vue'
import DomainTable from './components/DomainTable.vue'
import {
  generateDomainStream,
  checkDomains,
  checkCompliance,
  checkHistory,
  getUnavailableDomains,
  addUnavailableDomains,
} from './api'

const loading = ref(false)
const checking = ref(false)
const complianceChecking = ref(false)
const historyChecking = ref(false)
const thinking = ref('')
const domains = ref([])
const progressText = ref('')
let abortController = null

// 每轮追加时的轮次标记，用于给 AI 换 prompt
const MAX_ROUNDS = 3
const MIN_REGISTRABLE = 3

// 不可用域名库（生成时作为黑名单传给 DeepSeek）
const unavailableDomains = ref([])

async function loadUnavailableDomains() {
  try {
    const data = await getUnavailableDomains()
    unavailableDomains.value = Array.isArray(data) ? data : []
  } catch (e) {
    console.warn('加载不可用域名库失败：', e)
    unavailableDomains.value = []
  }
}

async function handleGenerate({ theme, desc }) {
  console.log('[handleGenerate] 点击生成，参数：', theme, desc)
  if (abortController) abortController.abort()
  abortController = new AbortController()

  loading.value = true
  checking.value = false
  domains.value = []
  thinking.value = ''
  progressText.value = ''

  // 生成前加载不可用域名库
  await loadUnavailableDomains()
  const excludeList = unavailableDomains.value.map((d) => d.domain)

  try {
    // 第一轮：正常生成
    const generatedDomains = await generateAndCollect(theme, desc, '初始生成', excludeList)
    if (generatedDomains.length === 0) {
      ElMessage.warning('返回结果为空')
      return
    }

    // 统一初始化
    domains.value = generatedDomains.map((d) => ({ ...d, registrable: null }))

    // Cloudflare 检测
    await runDomainCheck()

    // 可注册不足则补全（最多 3 轮）
    let round = 1
    while (countRegistrable() < MIN_REGISTRABLE && round < MAX_ROUNDS) {
      round++
      ElMessage.info(
        `可注册域名不足 ${MIN_REGISTRABLE} 个（当前 ${countRegistrable()} 个），正在进行第 ${round} 轮补全...`
      )
      const extra = await generateAndCollect(
        theme,
        desc,
        `第 ${round} 轮补全（请换一批全新的域名，不要与之前重复）`,
        excludeList
      )
      if (extra.length === 0) {
        break
      }
      // 合并新域名
      const existingNames = new Set(domains.value.map((d) => d.domain))
      const newOnes = extra
        .filter((d) => !existingNames.has(d.domain))
        .map((d) => ({ ...d, registrable: null }))
      domains.value.push(...newOnes)
      // 再次检测可注册性
      await runDomainCheck()
    }

    // 全部完成后，对可注册域名做合规 + 历史痕迹检测
    const registrableDomains = domains.value.filter((d) => d.registrable)
    if (registrableDomains.length > 0) {
      await runComplianceCheck(registrableDomains)
      await runHistoryCheck(registrableDomains)
    }

    // 检测完成后，将不可用域名入库
    await saveUnavailableDomains()
  } catch (e) {
    if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
      ElMessage.error('请求失败：' + (e.message || '未知错误'))
    } else {
      ElMessage.warning('已中断生成流程')
    }
  } finally {
    loading.value = false
    checking.value = false
    complianceChecking.value = false
    historyChecking.value = false
    progressText.value = ''
    abortController = null
  }
}

function handleAbort() {
  if (abortController) {
    abortController.abort()
  }
}

// 调用流式生成并收集域名列表
async function generateAndCollect(theme, desc, hint = '', exclude = []) {
  let collected = []
  let localThinking = ''

  progressText.value = `[${hint}] 正在调用 AI 生成域名...`

  await generateDomainStream(theme, desc, {
    onThinking: (text) => {
      localThinking += text
      thinking.value += text
    },
    onDone: (list) => {
      collected = list || []
      ElMessage.success(`[${hint}] 生成 ${collected.length} 个域名`)
    },
    onError: (msg) => {
      ElMessage.error(msg)
    },
  }, abortController?.signal, exclude)

  progressText.value = ''
  return collected
}

function countRegistrable() {
  return domains.value.filter((d) => d.registrable === true).length
}

async function runDomainCheck() {
  checking.value = true
  progressText.value = '正在检测域名可注册性（Cloudflare）...'
  try {
    const domainNames = domains.value.map((d) => d.domain)
    const result = await checkDomains(domainNames, abortController?.signal)
    if (result.success) {
      const statusMap = new Map(result.data.map((r) => [r.name, r]))
      domains.value = domains.value.map((d) => {
        const info = statusMap.get(d.domain)
        return {
          ...d,
          registrable: info ? info.registrable : null,
          checkReason: info ? info.reason : '',
          pricing: info ? info.pricing : null,
        }
      })
      const available = countRegistrable()
      ElMessage.success(
        `可注册检测：${available} 个可注册，${domains.value.length - available} 个已注册`
      )
    } else {
      ElMessage.warning('域名检测失败：' + (result.message || '未知错误'))
    }
  } catch (e) {
    if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
      console.error('域名检测出错：', e)
      ElMessage.error('域名检测请求失败：' + (e.message || '未知错误'))
    } else {
      throw e
    }
  } finally {
    checking.value = false
    progressText.value = ''
  }
}

async function runComplianceCheck(registrableList) {
  complianceChecking.value = true
  progressText.value = `正在检测 ${registrableList.length} 个域名的合规风险...`
  try {
    const result = await checkCompliance(
      registrableList.map((d) => ({ domain: d.domain, reason: d.reason })),
      abortController?.signal
    )
    if (result.success) {
      const complianceMap = new Map(result.data.map((r) => [r.domain, r]))
      domains.value = domains.value.map((d) => {
        const info = complianceMap.get(d.domain)
        return info
          ? {
              ...d,
              risk_level: info.risk_level,
              target_brand: info.target_brand,
              risk_reason: info.risk_reason,
              negative_review: info.negative_review,
            }
          : d
      })
      const hasRisk = result.data.filter((r) => r.risk_level === '高').length
      const unknown = result.data.filter((r) => r.risk_level === '未知').length
      if (unknown > 0) {
        ElMessage.warning(`合规检测完成：${unknown} 个域名检测失败`)
      } else {
        ElMessage.success(`合规检测完成：${hasRisk} 个高风险`)
      }
    } else {
      ElMessage.warning('合规检测批量失败，标记为未知：' + (result.message || '未知错误'))
      domains.value = domains.value.map((d) =>
        d.registrable
          ? {
              ...d,
              risk_level: '未知',
              risk_reason: '检测失败 - ' + (result.message || '批量请求异常'),
            }
          : d
      )
    }
  } catch (e) {
    if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
      console.error('合规检测出错：', e)
      ElMessage.error('合规检测请求失败：' + (e.message || '未知错误'))
      domains.value = domains.value.map((d) =>
        d.registrable
          ? { ...d, risk_level: '未知', risk_reason: '检测失败 - 网络异常' }
          : d
      )
    } else {
      throw e
    }
  } finally {
    complianceChecking.value = false
    progressText.value = ''
  }
}

async function runHistoryCheck(registrableList) {
  historyChecking.value = true
  const domainNames = registrableList.map((d) => d.domain)
  progressText.value = '历史使用痕迹检测中，请稍后...'

  try {
    const result = await checkHistory(domainNames, abortController?.signal)
    if (result.success) {
      const historyMap = new Map(result.data.map((r) => [r.domain, r]))
      domains.value = domains.value.map((d) => {
        const info = historyMap.get(d.domain)
        return info
          ? {
              ...d,
              history_status: info.status,
              ever_used: info.ever_used,
              source_count: info.source_count,
              sources: info.sources,
            }
          : d
      })
      const usedCount = result.data.filter((r) => r.ever_used === true).length
      if (usedCount > 0) {
        ElMessage.warning(`历史痕迹检测完成：${usedCount} 个域名曾被使用过`)
      } else {
        ElMessage.success('历史痕迹检测完成：未发现使用痕迹')
      }
    } else {
      ElMessage.warning('历史痕迹检测失败：' + (result.message || '未知错误'))
    }
  } catch (e) {
    if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
      console.error('历史痕迹检测出错：', e)
      ElMessage.error('历史痕迹检测请求失败：' + (e.message || '未知错误'))
    } else {
      throw e
    }
  } finally {
    historyChecking.value = false
    progressText.value = ''
  }
}

// 检测完成后，将不可用域名入库
async function saveUnavailableDomains() {
  const items = []
  for (const d of domains.value) {
    let reason = null
    if (d.registrable === false) {
      reason = '已注册'
    } else if (d.registrable === true) {
      if (d.risk_level === '高' || d.risk_level === '中') {
        reason = `合规风险-${d.risk_level}`
      } else if (d.ever_used === true) {
        reason = '曾被使用过'
      }
    }
    if (reason) {
      items.push({ domain: d.domain, reason })
    }
  }

  if (items.length === 0) return

  try {
    await addUnavailableDomains(items)
    ElMessage.success(`已将 ${items.length} 个不可用域名加入黑名单，下次生成时自动避开`)
  } catch (e) {
    console.warn('入库不可用域名失败：', e)
  }
}
</script>

<style scoped>
.app-title {
  font-size: 20px;
  font-weight: bold;
}

.progress-alert {
  margin-top: 16px;
}
</style>
