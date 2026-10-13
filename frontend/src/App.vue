<template>
  <el-card>
    <template #header>
      <div class="app-title">🌐 汉盈域名检测智能体</div>
    </template>

    <DomainForm :loading="loading" @generate="handleGenerate" />

    <ThinkingBox v-if="thinking" :content="thinking" :auto-expand="loading" />

    <el-divider v-if="domains.length > 0" />

    <DomainTable v-if="domains.length > 0" :domains="domains" :checking="checking" :compliance-checking="complianceChecking" />

    <el-empty v-if="!loading && domains.length === 0" description="还没有生成结果" />
  </el-card>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'
import DomainForm from './components/DomainForm.vue'
import ThinkingBox from './components/ThinkingBox.vue'
import DomainTable from './components/DomainTable.vue'
import { generateDomainStream, checkDomains, checkCompliance } from './api'

const loading = ref(false)
const checking = ref(false)
const complianceChecking = ref(false)
const thinking = ref('')
const domains = ref([])
let abortController = null

async function handleGenerate({ theme, desc }) {
  console.log('[handleGenerate] 点击生成，参数：', theme, desc)
  if (abortController) abortController.abort()
  abortController = new AbortController()

  loading.value = true
  checking.value = false
  domains.value = []
  thinking.value = ''

  let generatedDomains = []

  try {
    await generateDomainStream(theme, desc, {
      onThinking: (text) => {
        thinking.value += text
      },
      onDone: (list) => {
        console.log('[onDone] 收到域名列表：', list)
        generatedDomains = list || []
        domains.value = generatedDomains.map((d) => ({ ...d, registrable: null }))
        if (generatedDomains.length === 0) {
          ElMessage.warning('返回结果为空，可能解析失败')
        } else {
          ElMessage.success(`成功生成 ${generatedDomains.length} 个域名`)
        }
      },
      onError: (msg) => {
        ElMessage.error(msg)
      },
    }, abortController.signal)

    // 流式生成完成后，主动调用 Cloudflare 检测可注册性
    if (generatedDomains.length > 0) {
      await runDomainCheck(generatedDomains)
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      ElMessage.error('请求失败：' + (e.message || '未知错误'))
    }
  } finally {
    loading.value = false
    abortController = null
  }
}

async function runDomainCheck(list) {
  checking.value = true
  try {
    const domainNames = list.map((d) => d.domain)
    const result = await checkDomains(domainNames)
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
      const available = domains.value.filter((d) => d.registrable).length
      ElMessage.success(`检测完成：${available} 个可注册，${domains.value.length - available} 个已注册`)

      // 对可注册域名进行合规性检测
      const registrableDomains = domains.value.filter((d) => d.registrable)
      if (registrableDomains.length > 0) {
        await runComplianceCheck(registrableDomains)
      }
    } else {
      ElMessage.warning('域名检测失败：' + (result.message || '未知错误'))
    }
  } catch (e) {
    console.error('域名检测出错：', e)
    ElMessage.error('域名检测请求失败：' + (e.message || '未知错误'))
  } finally {
    checking.value = false
  }
}

async function runComplianceCheck(registrableList) {
  complianceChecking.value = true
  try {
    const result = await checkCompliance(
      registrableList.map((d) => ({ domain: d.domain, reason: d.reason }))
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
      ElMessage.success('合规检测完成')
    } else {
      ElMessage.warning('合规检测失败：' + (result.message || '未知错误'))
    }
  } catch (e) {
    console.error('合规检测出错：', e)
    ElMessage.error('合规检测请求失败：' + (e.message || '未知错误'))
  } finally {
    complianceChecking.value = false
  }
}
</script>

<style scoped>
.app-title {
  font-size: 20px;
  font-weight: bold;
}
</style>
