<template>
  <el-card shadow="never" class="page-card">
    <template #header>
      <div class="card-head">
        <div class="card-title">
          已收录的风险域名
          <el-tag type="info" size="small" effect="plain">{{ rows.length }} 条</el-tag>
        </div>
        <div class="card-actions">
          <el-input
            v-model="keyword"
            placeholder="搜索域名 / 原因"
            clearable
            size="small"
            class="search"
          />
          <el-select
            v-model="reasonFilter"
            placeholder="全部原因"
            clearable
            size="small"
            class="reason-filter"
          >
            <el-option v-for="r in reasons" :key="r" :label="r" :value="r" />
          </el-select>
          <el-button size="small" :loading="loading" @click="load">刷新</el-button>
        </div>
      </div>
    </template>

    <el-table
      :data="filtered"
      v-loading="loading"
      border
      stripe
      :max-height="tableHeight"
      empty-text="暂无收录记录"
      style="width: 100%"
    >
      <el-table-column type="index" label="#" width="60" align="center" />
      <el-table-column prop="domain" label="域名" min-width="280">
        <template #default="{ row }">
          <el-tag type="danger" effect="plain">{{ row.domain }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="reason" label="收录原因" width="170">
        <template #default="{ row }">
          <el-tag :type="reasonTag(row.reason)" size="small">{{ row.reason }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="added_at" label="收录时间" min-width="220">
        <template #default="{ row }">{{ fmtTime(row.added_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="110" align="center">
        <template #default="{ row }">
          <el-button link type="danger" size="small" @click="remove(row)">移除</el-button>
        </template>
      </el-table-column>
    </el-table>
  </el-card>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { getUnavailableDomains, removeUnavailableDomain } from '../api'

const emit = defineEmits(['changed'])

const rows = ref([])
const loading = ref(false)
const keyword = ref('')
const reasonFilter = ref('')
const tableHeight = 'calc(100vh - 300px)'

const reasons = computed(() => {
  const set = new Set(rows.value.map((r) => r.reason).filter(Boolean))
  return [...set].sort()
})

const filtered = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  return rows.value.filter((r) => {
    if (reasonFilter.value && r.reason !== reasonFilter.value) return false
    if (!kw) return true
    return (
      String(r.domain || '').toLowerCase().includes(kw) ||
      String(r.reason || '').toLowerCase().includes(kw)
    )
  })
})

function reasonTag(reason) {
  if (!reason) return 'info'
  if (reason.includes('已被使用') || reason.includes('曾被使用')) return 'warning'
  if (reason.includes('注册')) return 'info'
  if (reason.includes('风险')) return 'danger'
  return 'info'
}

function fmtTime(value) {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString('zh-CN', { hour12: false })
}

async function load() {
  loading.value = true
  try {
    const data = await getUnavailableDomains()
    rows.value = Array.isArray(data) ? data : []
  } catch (e) {
    ElMessage.error('加载风险域名库失败：' + (e.message || '网络异常'))
  } finally {
    loading.value = false
  }
}

async function remove(row) {
  try {
    await ElMessageBox.confirm(
      `确定把 ${row.domain} 从风险域名库移除吗？移除后它可能再次被生成。`,
      '移除确认',
      { confirmButtonText: '确定移除', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  try {
    const res = await removeUnavailableDomain(row.domain)
    if (res && res.success) {
      rows.value = rows.value.filter((r) => r.domain !== row.domain)
      ElMessage.success(`已移除 ${row.domain}`)
      emit('changed')
    } else {
      ElMessage.error('移除失败')
    }
  } catch (e) {
    ElMessage.error('移除失败：' + (e.message || '网络异常'))
  }
}

onMounted(load)
defineExpose({ load })
</script>

<style scoped>
.page-card {
  min-height: calc(100vh - 140px);
}

.card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
}

.card-title {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 16px;
  font-weight: 600;
}

.card-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.search {
  width: 220px;
}

.reason-filter {
  width: 150px;
}
</style>
