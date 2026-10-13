<template>
  <div class="result-table">
    <el-table :data="domains" border stripe v-loading="checking || complianceChecking">
      <el-table-column type="index" label="#" width="55" align="center" />
      <el-table-column prop="domain" label="域名" width="160" align="center">
        <template #default="{ row }">
          <el-tag type="success">{{ row.domain }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="可注册性" width="100" align="center">
        <template #default="{ row }">
          <el-tag
            v-if="row.registrable === true"
            type="success"
            effect="dark"
          >可注册</el-tag>
          <el-tag
            v-else-if="row.registrable === false"
            type="info"
          >已注册</el-tag>
          <el-tag v-else type="warning">检测中...</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="合规风险" width="100" align="center">
        <template #default="{ row }">
          <el-tag
            v-if="row.registrable !== true"
            type="info"
          >—</el-tag>
          <el-tag
            v-else-if="row.risk_level === '高'"
            type="danger"
            effect="dark"
          >高风险</el-tag>
          <el-tag
            v-else-if="row.risk_level === '中'"
            type="warning"
          >中风险</el-tag>
          <el-tag
            v-else-if="row.risk_level === '低'"
            type="success"
          >低风险</el-tag>
          <el-tag v-else-if="complianceChecking" type="warning">检测中...</el-tag>
          <el-tag v-else type="info">未检测</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="reason" label="生成理由" min-width="220" show-overflow-tooltip />
      <el-table-column label="风险说明" min-width="260">
        <template #default="{ row }">
          <div v-if="row.risk_level" class="risk-detail">
            <div v-if="row.target_brand && row.target_brand !== '无'" class="risk-brand">
              关联品牌：{{ row.target_brand }}
            </div>
            <div class="risk-reason">{{ row.risk_reason }}</div>
          </div>
          <span v-else style="color: #999;">—</span>
        </template>
      </el-table-column>
    </el-table>
  </div>
</template>

<script setup>
defineProps({
  domains: { type: Array, default: () => [] },
  checking: { type: Boolean, default: false },
  complianceChecking: { type: Boolean, default: false },
})
</script>

<style scoped>
.result-table {
  margin: 16px 0;
  width: 100%;
}

:deep(.el-table .cell) {
  word-break: break-all;
  overflow-wrap: break-word;
  line-height: 1.6;
}

.risk-detail {
  font-size: 13px;
  line-height: 1.6;
}

.risk-brand {
  color: #e6a23c;
  margin-bottom: 4px;
}

.risk-reason {
  color: #606266;
}
</style>
