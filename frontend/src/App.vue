<template>
  <el-container class="layout">
    <el-aside width="208px" class="sidebar">
      <div class="brand">
        <span class="brand-logo">🌐</span>
        <span class="brand-text">汉盈域名检测</span>
      </div>
      <el-menu :default-active="activeMenu" class="side-menu" @select="onSelect">
        <el-menu-item index="generate">域名生成检测</el-menu-item>
        <el-menu-item index="unavailable">
          <span class="menu-label">已收录的风险域名</span>
          <el-tag v-if="unavailableDomains.length" size="small" type="info" effect="dark" round>
            {{ unavailableDomains.length }}
          </el-tag>
        </el-menu-item>
      </el-menu>
    </el-aside>

    <el-container class="body">
      <el-header class="topbar">
        <div class="app-header">
          <div class="app-title">{{ pageTitle }}</div>
          <div class="balance-box">
            <el-tooltip
              v-for="c in balanceCards"
              :key="c.key"
              :content="c.tip"
              placement="bottom"
              :disabled="!c.tip"
            >
              <span class="balance-text" :class="{ 'balance-low': c.low, 'balance-err': c.err }">
                {{ c.text }}
              </span>
            </el-tooltip>
            <el-button link size="small" :loading="anyBalanceLoading" @click="loadBalances()">
              刷新
            </el-button>
          </div>
        </div>
      </el-header>

      <el-main class="main">
        <!-- 生成页用 v-show 保留表格状态，切换菜单不丢结果 -->
        <el-card v-show="activeMenu === 'generate'" shadow="never" class="page-card">
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

          <DomainTable
            v-if="domains.length > 0"
            :domains="domains"
            :checking="checking"
            :compliance-checking="complianceChecking"
            :history-checking="historyChecking"
          />

          <el-empty v-if="!loading && domains.length === 0" description="还没有生成结果" />
        </el-card>

        <UnavailableDomains v-if="activeMenu === 'unavailable'" @changed="onBlacklistChanged" />
      </el-main>
    </el-container>
  </el-container>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import DomainForm from './components/DomainForm.vue'
import ThinkingBox from './components/ThinkingBox.vue'
import DomainTable from './components/DomainTable.vue'
import UnavailableDomains from './components/UnavailableDomains.vue'
import {
  generateDomainStream,
  checkDomains,
  checkCompliance,
  checkHistorySource,
  getUnavailableDomains,
  addUnavailableDomains,
  getDeepSeekBalance,
  getDmxapiBalance,
} from './api'

const loading = ref(false)
const checking = ref(false)
const complianceChecking = ref(false)
const historyChecking = ref(false)
const thinking = ref('')
const domains = ref([])
const progressText = ref('')
let abortController = null

// ============ 侧边栏导航 ============
const activeMenu = ref('generate')
const pageTitle = computed(() =>
  activeMenu.value === 'unavailable' ? '已收录的风险域名' : '域名生成检测'
)

function onSelect(index) {
  activeMenu.value = index
}

// 风险域名库被增删后，刷新生成时用的黑名单缓存（菜单上的计数也跟着变）
function onBlacklistChanged() {
  blacklistPromise = loadUnavailableDomains()
}

// 每轮追加时的轮次标记，用于给 AI 换 prompt
const MAX_ROUNDS = 3
const MIN_REGISTRABLE = 3

// 不可用域名库（生成时作为黑名单传给 DeepSeek）
const unavailableDomains = ref([])
// 黑名单的后台加载任务；生成时最多等它 1.5s，绝不让它挡住生成请求
let blacklistPromise = null
const BLACKLIST_WAIT_MS = 1500

// ============ 余额（DeepSeek / DMXAPI） ============
// 两个平台各一套数据：DeepSeek 用于域名生成，DMXAPI 用于合规检测的 GPT 模型
const balanceData = ref({ deepseek: null, dmxapi: null })
const balanceLoading = ref({ deepseek: false, dmxapi: false })
const balanceError = ref({ deepseek: '', dmxapi: '' })

const anyBalanceLoading = computed(
  () => balanceLoading.value.deepseek || balanceLoading.value.dmxapi
)

function deepseekPrimary() {
  const list = balanceData.value.deepseek?.balances || []
  return list.find((b) => b.currency === 'CNY') || list[0] || null
}

function shortError(msg) {
  // 页头放不下长句：先砍掉括号里的补充说明，再按长度截断（完整内容在悬停提示里）
  const s = String(msg || '').split('（')[0].trim()
  return s.length > 20 ? `${s.slice(0, 20)}…` : s
}

function describeBalance(key) {
  if (key === 'deepseek') {
    const b = deepseekPrimary()
    if (!b) return { text: '—', detail: '', low: false }
    return {
      text: `${b.currency === 'CNY' ? '¥' : '$'}${b.total}`,
      detail: [
        `总额 ${b.total} ${b.currency}`,
        `赠金 ${b.granted}`,
        `充值 ${b.topped_up}`,
        balanceData.value.deepseek?.is_available === false ? '⚠️ 余额不足，API 调用会失败' : '',
      ]
        .filter(Boolean)
        .join('　'),
      low: b.total_value != null && b.total_value < 10,
    }
  }
  const d = balanceData.value.dmxapi
  if (!d) return { text: '—', detail: '', low: false }
  return {
    text: d.balance_cny != null ? `¥${d.balance_cny.toFixed(2)}` : '—',
    detail: [
      d.balance_cny != null ? `余额 ¥${d.balance_cny}` : '',
      d.used_cny != null ? `已用 ¥${d.used_cny}` : '',
      d.username || '',
    ]
      .filter(Boolean)
      .join('　'),
    low: d.balance_cny != null && d.balance_cny < 10,
  }
}

const balanceCards = computed(() =>
  [
    { key: 'deepseek', label: 'DeepSeek' },
    { key: 'dmxapi', label: 'DMXAPI' },
  ].map(({ key, label }) => {
    const loading = balanceLoading.value[key]
    const error = balanceError.value[key]
    const d = describeBalance(key)
    return {
      key,
      text: loading
        ? `${label} 查询中...`
        : error
          ? `${label} ${shortError(error)}`
          : `${label} ${d.text}`,
      tip: error || d.detail,
      low: !loading && !error && d.low,
      err: !!error,
    }
  })
)

async function loadBalanceOf(key, fetcher, signal) {
  balanceLoading.value[key] = true
  balanceError.value[key] = ''
  try {
    const res = await fetcher(signal)
    if (res && res.success) {
      balanceData.value[key] = res.data
    } else {
      balanceError.value[key] = (res && res.message) || '查询失败'
    }
  } catch (e) {
    if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
      balanceError.value[key] = e.message || '网络异常'
    }
  } finally {
    balanceLoading.value[key] = false
  }
}

function loadBalances(signal) {
  loadBalanceOf('deepseek', getDeepSeekBalance, signal)
  loadBalanceOf('dmxapi', getDmxapiBalance, signal)
}

async function loadUnavailableDomains(signal) {
  try {
    const data = await getUnavailableDomains(signal)
    unavailableDomains.value = Array.isArray(data) ? data : []
  } catch (e) {
    if (e.name !== 'AbortError' && e.code !== 'ERR_CANCELED') {
      console.warn('加载不可用域名库失败：', e)
    }
  }
}

// 挂载即拉一次：既预热黑名单，也让 Render 免费实例的冷启动在用户填表单时就开始唤醒，
// 而不是等用户点了「生成」才开始等 30~60s 的唤醒。
onMounted(() => {
  blacklistPromise = loadUnavailableDomains()
  // 余额也一并拉：既能显示，又顺带把休眠的后端叫醒
  loadBalances()
})

// 等黑名单就绪，但最多等 BLACKLIST_WAIT_MS。
// 原实现是在 handleGenerate 里 `await loadUnavailableDomains()`，而那次请求：
//   1) 没有超时控制，Render 冷启动时会挂 30~60s，按钮显示「生成中」却迟迟不发请求；
//   2) 没有接 abortController.signal，所以点「中断」根本停不下来。
function waitBlacklist() {
  if (!blacklistPromise) return Promise.resolve()
  return Promise.race([
    blacklistPromise,
    new Promise((resolve) => setTimeout(resolve, BLACKLIST_WAIT_MS)),
  ])
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

  // 黑名单只是给模型的提示，最多等 1.5s；没到就用现有缓存继续，
  // 不让它挡住生成（Render 冷启动时这一步曾把整条链路卡住几十秒）
  await waitBlacklist()
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
    // 一轮下来会消耗 token，顺手刷新余额
    loadBalances()
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

  // Render 免费实例会休眠，首个请求要等 30~60s 才被唤醒。
  // 这段时间没有任何 SSE 事件，界面看起来像卡死，所以给一句明确提示。
  const wakeHintTimer = setTimeout(() => {
    if (!localThinking && !collected.length) {
      progressText.value = `[${hint}] 正在等待服务响应（Render 免费实例冷启动约需 30~60s，请稍候）...`
    }
  }, 6000)

  try {
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
  } finally {
    clearTimeout(wakeHintTimer)
    progressText.value = ''
  }

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

// 与后端 SOURCE_ORDER 对齐的主源（免费、可并行）；
// VirusTotal 仅在主源都没命中时才查，省免费额度（与后端默认语义一致）。
// crt.sh 已下线（限流+超时的最坏路径超出预算），不再参与检测。
const PRIMARY_HISTORY_SOURCES = ['wayback', 'trustpilot', 'gridinsoft']

// 根据已合并的 sources 重算单域名的汇总结论（镜像后端 archive._summarize）
function recomputeHistorySummary(sources) {
  const vals = Object.values(sources || {}).filter(Boolean)
  const asked = vals.filter((s) => s.status === 'ok')
  const errored = vals.filter((s) => s.status === 'error')
  let ever_used
  if (asked.some((s) => s.hit === true)) ever_used = true
  else if (errored.length > 0) ever_used = null
  else if (asked.length > 0 && asked.every((s) => s.hit === false)) ever_used = false
  else ever_used = null
  let history_status
  if (vals.length === 0) history_status = null
  else if (errored.length === 0) history_status = 'ok'
  else if (errored.length === vals.length) history_status = 'error'
  else history_status = 'partial'
  return {
    ever_used,
    history_status,
    source_count: vals.filter((s) => s.hit === true).length,
    ok_count: asked.length,
  }
}

// 把某个源的返回增量合并进表格（不覆盖其他源已有的结果）
function mergeHistoryResult(result) {
  if (!result || !result.success || !Array.isArray(result.data)) return
  const map = new Map(result.data.map((r) => [r.domain, r]))
  domains.value = domains.value.map((d) => {
    const info = map.get(d.domain)
    if (!info) return d
    const merged = { ...(d.sources || {}), ...(info.sources || {}) }
    return { ...d, sources: merged, ...recomputeHistorySummary(merged) }
  })
}

// 某个源整个请求失败（网络/超时）时，把该源在所有域名上标记为失败，
// 但不影响其他已返回的源——这正是「分开发送请求」的核心收益。
function markSourceFailed(domainNames, source, msg) {
  const set = new Set(domainNames)
  domains.value = domains.value.map((d) => {
    if (!set.has(d.domain)) return d
    const merged = {
      ...(d.sources || {}),
      [source]: { source, status: 'error', hit: null, message: '检测失败', detail: msg },
    }
    return { ...d, sources: merged, ...recomputeHistorySummary(merged) }
  })
}

// 某个源被主动跳过（目前只有 VirusTotal 会因省额度而跳过）：
// 必须写成 status='skipped' 落到表格上，否则该列没有数据，会被渲染成「未检测」，
// 让人误以为是「还没查」或「查失败」，而实际是「本批不查」。
function markSourceSkipped(domainNames, source, detailOf) {
  const set = new Set(domainNames)
  domains.value = domains.value.map((d) => {
    if (!set.has(d.domain)) return d
    const merged = {
      ...(d.sources || {}),
      [source]: {
        source,
        label: source === 'virustotal' ? 'VirusTotal' : source,
        status: 'skipped',
        hit: null,
        count: 0,
        message: '已跳过',
        detail: detailOf(d.domain),
      },
    }
    return { ...d, sources: merged, ...recomputeHistorySummary(merged) }
  })
}

async function runHistoryCheck(registrableList) {
  historyChecking.value = true
  const domainNames = registrableList.map((d) => d.domain)
  progressText.value = '历史使用痕迹检测中（各源独立返回，请稍候）...'

  // 初始化空 sources，便于逐源增量合并渲染
  domains.value = domains.value.map((d) =>
    d.registrable ? { ...d, sources: d.sources || {} } : d
  )

  try {
    // 主源并行：每个源一个独立请求，慢源只阻塞自己那一列，快源即时渲染
    await Promise.all(
      PRIMARY_HISTORY_SOURCES.map(async (src) => {
        try {
          const result = await checkHistorySource(domainNames, src, abortController?.signal)
          mergeHistoryResult(result)
        } catch (e) {
          if (e.name === 'AbortError' || e.code === 'ERR_CANCELED') throw e
          console.error(`[history] ${src} 请求失败：`, e)
          markSourceFailed(domainNames, src, '请求失败 - ' + (e.message || '网络异常'))
        }
      })
    )

    // VirusTotal：按域名逐个判断，不能「一个命中就整批跳过」——
    // 命中与否是每个域名自己的结论，同批其他域名照样可能已被注册/使用过，
    // 整批跳过会直接漏检（这正是上一版的问题）。
    // 只对「已被其他源确认曾使用」的域名省掉 VT：那种情况下 VT 改变不了结论。
    const vtTargets = []
    const vtSkipped = []
    for (const d of domains.value) {
      if (!d.registrable) continue
      const hit = Object.values(d.sources || {}).some(
        (s) => s && s.status === 'ok' && s.hit === true
      )
      if (hit) vtSkipped.push(d.domain)
      else vtTargets.push(d.domain)
    }

    if (vtSkipped.length > 0) {
      markSourceSkipped(
        vtSkipped,
        'virustotal',
        () => '已有其他源确认曾被使用，无需再查（省额度）'
      )
    }
    if (vtTargets.length > 0) {
      // 免费额度 4 次/分钟：后端每个域名间隔 15s，几个域名就要几十秒
      progressText.value = `VirusTotal 逐个查询中（${vtTargets.length} 个，约 ${vtTargets.length * 15}s）...`
      try {
        const result = await checkHistorySource(
          vtTargets,
          'virustotal',
          abortController?.signal
        )
        mergeHistoryResult(result)
      } catch (e) {
        if (e.name === 'AbortError' || e.code === 'ERR_CANCELED') throw e
        console.error('[history] virustotal 请求失败：', e)
        markSourceFailed(vtTargets, 'virustotal', '请求失败 - ' + (e.message || '网络异常'))
      } finally {
        progressText.value = ''
      }
    }

    const usedCount = domains.value.filter((d) => d.ever_used === true).length
    if (usedCount > 0) {
      ElMessage.warning(`历史痕迹检测完成：${usedCount} 个域名曾被使用过`)
    } else {
      ElMessage.success('历史痕迹检测完成：未发现使用痕迹')
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
    // 刷新内存里的黑名单，下一次生成才能带上这批新域名
    blacklistPromise = loadUnavailableDomains()
  } catch (e) {
    console.warn('入库不可用域名失败：', e)
  }
}
</script>

<style scoped>
.layout {
  height: 100vh;
}

/* ============ 左侧菜单 ============ */
.sidebar {
  background: #fff;
  border-right: 1px solid #e4e7ed;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.brand {
  display: flex;
  align-items: center;
  gap: 8px;
  height: 56px;
  padding: 0 18px;
  font-size: 15px;
  font-weight: 700;
  color: #303133;
  border-bottom: 1px solid #f0f2f5;
}

.brand-logo {
  font-size: 18px;
}

.side-menu {
  border-right: none;
  flex: 1;
}

.menu-label {
  margin-right: 8px;
}

/* ============ 右侧主体 ============ */
.body {
  min-width: 0; /* 允许内部表格横向自适应，不被侧栏挤爆 */
}

.topbar {
  height: 56px;
  padding: 0 20px;
  background: #fff;
  border-bottom: 1px solid #e4e7ed;
  display: flex;
  align-items: center;
}

.main {
  padding: 16px 20px 20px;
  overflow: auto;
}

.page-card {
  min-height: calc(100vh - 104px);
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  width: 100%;
}

.app-title {
  font-size: 16px;
  font-weight: 600;
}

.balance-box {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 13px;
  color: #606266;
}

.balance-text {
  cursor: help;
  white-space: nowrap;
}

.balance-text.balance-low {
  color: #f56c6c;
  font-weight: 600;
}

.balance-text.balance-err {
  color: #e6a23c;
}

.progress-alert {
  margin-top: 16px;
}
</style>
