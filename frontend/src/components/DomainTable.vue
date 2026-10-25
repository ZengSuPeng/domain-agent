<template>
  <div class="result-table">
    <el-table :data="domains" border stripe v-loading="checking || complianceChecking || historyChecking">
      <el-table-column type="index" label="#" width="55" align="center" />
      <el-table-column prop="domain" label="域名" width="150" align="center">
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
          <el-tag
            v-else-if="row.risk_level === '未知'"
            type="warning"
          >检测失败</el-tag>
          <el-tag v-else-if="complianceChecking" type="warning">检测中...</el-tag>
          <el-tag v-else type="info">未检测</el-tag>
        </template>
      </el-table-column>

      <el-table-column label="历史使用痕迹" align="center">
        <el-table-column
          v-for="s in SOURCES"
          :key="s.key"
          :label="s.label"
          width="94"
          align="center"
        >
          <template #default="{ row }">
            <el-tooltip
              v-if="row.registrable === true && srcOf(row, s.key)"
              :content="srcTip(row, s.key)"
              placement="top"
              :show-after="120"
            >
              <el-tag :type="srcTag(row, s.key).type" size="small" effect="plain">
                {{ srcTag(row, s.key).text }}
              </el-tag>
            </el-tooltip>
            <el-tag
              v-else-if="row.registrable === true && historyChecking"
              type="warning"
              size="small"
            >检测中</el-tag>
            <el-tag v-else-if="row.registrable === true" type="info" size="small">未检测</el-tag>
            <el-tag v-else type="info" size="small">—</el-tag>
          </template>
        </el-table-column>
      </el-table-column>

      <el-table-column label="Trustpilot" width="230" align="center">
        <template #default="{ row }">
          <div v-if="row.registrable !== true" class="src-empty">—</div>
          <div v-else-if="!srcOf(row, 'trustpilot')" class="src-empty">
            {{ historyChecking ? '检测中' : '未检测' }}
          </div>
          <div v-else class="src-cell">
            <div class="src-line">
              <span class="src-key">网页链接</span>
              <a
                v-if="srcOf(row, 'trustpilot').url"
                :href="srcOf(row, 'trustpilot').url"
                target="_blank"
                class="trace-link"
              >打开 →</a>
              <span v-else>无</span>
            </div>
            <div class="src-line">
              <span class="src-key">是否冲突</span>
              <span :class="srcConflictClass(row, 'trustpilot')">{{ srcConflict(row, 'trustpilot') }}</span>
            </div>
            <div class="src-line">
              <span class="src-key">信息</span>
              <span>{{ srcMessage(row, 'trustpilot') }}</span>
            </div>
          </div>
        </template>
      </el-table-column>

      <el-table-column label="Gridinsoft" width="230" align="center">
        <template #default="{ row }">
          <div v-if="row.registrable !== true" class="src-empty">—</div>
          <div v-else-if="!srcOf(row, 'gridinsoft')" class="src-empty">
            {{ historyChecking ? '检测中' : '未检测' }}
          </div>
          <div v-else class="src-cell">
            <div class="src-line">
              <span class="src-key">网页链接</span>
              <a
                v-if="srcOf(row, 'gridinsoft').url"
                :href="srcOf(row, 'gridinsoft').url"
                target="_blank"
                class="trace-link"
              >打开 →</a>
              <span v-else>无</span>
            </div>
            <div class="src-line">
              <span class="src-key">是否冲突</span>
              <span :class="srcConflictClass(row, 'gridinsoft')">{{ srcConflict(row, 'gridinsoft') }}</span>
            </div>
            <div class="src-line">
              <span class="src-key">信息</span>
              <span>{{ srcMessage(row, 'gridinsoft') }}</span>
            </div>
          </div>
        </template>
      </el-table-column>

      <el-table-column prop="reason" label="生成理由" min-width="200" show-overflow-tooltip />
      <el-table-column label="风险/痕迹说明" min-width="280">
        <template #default="{ row }">
          <div v-if="hasDetail(row)" class="risk-detail">
            <div v-if="row.target_brand && row.target_brand !== '无'" class="risk-brand">
              关联品牌：{{ row.target_brand }}
            </div>
            <div v-if="row.risk_reason" class="risk-reason" :class="{ 'risk-failed': row.risk_level === '未知' }">
              {{ row.risk_reason }}
            </div>

            <div v-if="row.ever_used === true" class="history-summary history-used">
              综合：曾被发现使用痕迹（{{ row.source_count }}/{{ ALL_SOURCES.length }} 个源命中），大概率曾被注册过
            </div>
            <div v-else-if="row.ever_used === false" class="history-summary history-clean">
              综合：{{ ALL_SOURCES.length }} 个源均无痕迹，大概率从未被注册使用
            </div>
            <div v-else-if="row.history_status === 'error'" class="history-summary history-error">
              历史痕迹检测失败（网络不可达，非「无痕迹」）
            </div>
            <div v-else-if="row.history_status === 'partial'" class="history-summary history-partial">
              部分痕迹源检测失败，结论可能不完整
            </div>

            <div v-for="s in hitSources(row)" :key="s.source" class="history-line">
              <span class="history-name">{{ s.label }}</span>
              <span>{{ s.message }}</span>
              <span v-if="s.earliest" class="history-date">最早 {{ s.earliest }}</span>
              <span v-if="s.latest" class="history-date">最近 {{ s.latest }}</span>
              <a v-if="s.url" :href="s.url" target="_blank" class="trace-link">查看 →</a>
            </div>

            <div v-for="s in failedSources(row)" :key="s.source" class="history-line history-failed">
              <span class="history-name">{{ s.label }}</span>
              <span>{{ s.detail || '检测失败' }}</span>
            </div>
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
  historyChecking: { type: Boolean, default: false },
})

// 痕迹源列表（新增源时只需在这里和 sources/base.py 的 SOURCE_ORDER 同步）
// Trustpilot 单独成列（展示链接/是否冲突/信息三项），不在此列表内
const SOURCES = [
  { key: 'wayback', label: 'Wayback' },
  { key: 'crt', label: '证书日志' },
  { key: 'virustotal', label: 'VirusTotal' },
]

// 全部源（含独立成列的 Trustpilot / Gridinsoft），用于命中统计与明细展示
const ALL_SOURCES = [
  { key: 'wayback', label: 'Wayback' },
  { key: 'crt', label: '证书日志' },
  { key: 'trustpilot', label: 'Trustpilot' },
  { key: 'gridinsoft', label: 'Gridinsoft' },
  { key: 'virustotal', label: 'VirusTotal' },
]

function srcOf(row, key) {
  return (row.sources && row.sources[key]) || null
}

function srcTag(row, key) {
  const s = srcOf(row, key)
  if (!s) return { text: '未检测', type: 'info' }
  if (s.status === 'skipped') return { text: '跳过', type: 'info' }
  if (s.status !== 'ok') return { text: '失败', type: 'danger' }
  if (s.hit) return { text: s.count ? `有 ${s.count}` : '有', type: 'warning' }
  return { text: '无', type: 'success' }
}

function srcTip(row, key) {
  const s = srcOf(row, key)
  if (!s) return '未检测'
  if (s.status === 'skipped') return `${s.label}：${s.detail || '已跳过'}`
  if (s.status !== 'ok') return `${s.label} 检测失败：${s.detail || '未知原因'}`
  const parts = [s.message || (s.hit ? '发现历史痕迹' : '无记录')]
  if (s.earliest) parts.push(`最早 ${s.earliest}`)
  if (s.latest) parts.push(`最近 ${s.latest}`)
  return `${s.label}：${parts.join('｜')}`
}

function sourceList(row) {
  if (!row.sources) return []
  return ALL_SOURCES.map((s) => row.sources[s.key]).filter(Boolean)
}

// 独立成列的源（Trustpilot / Gridinsoft）三行展示：是否冲突（hit 为 True 即为「是」）
// 请求不可达/被拦截时显示「失败」，绝不显示「否」——否则会被误读成「确认无冲突」
function srcConflict(row, key) {
  const s = srcOf(row, key)
  if (!s) return '—'
  if (s.status === 'skipped') return '—'
  if (s.status !== 'ok') return '失败'
  return s.hit === true ? '是' : '否'
}

function srcConflictClass(row, key) {
  const text = srcConflict(row, key)
  if (text === '是') return 'src-yes'
  if (text === '否') return 'src-no'
  return 'src-na'
}

// 独立成列的源：信息（message 字段，没有则「无」）
function srcMessage(row, key) {
  const s = srcOf(row, key)
  if (!s) return '无'
  const msg = s.message || '无'
  if (s.status !== 'ok' && s.detail) return `${msg}（${s.detail}）`
  return msg
}

function hitSources(row) {
  return sourceList(row).filter((s) => s.status === 'ok' && s.hit)
}

function failedSources(row) {
  return sourceList(row).filter((s) => s.status === 'error')
}

function hasDetail(row) {
  return Boolean(
    row.target_brand ||
      row.risk_reason ||
      row.source_count ||
      row.ever_used !== undefined ||
      row.history_status
  )
}
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

.risk-reason.risk-failed {
  color: #e6a23c;
  font-style: italic;
}

.history-summary {
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed #e4e7ed;
  font-size: 12px;
  line-height: 1.6;
}

.src-cell {
  font-size: 12px;
  line-height: 1.7;
  text-align: left;
}

.src-line {
  display: flex;
  gap: 6px;
}

.src-key {
  flex: 0 0 56px;
  color: #909399;
}

.src-empty {
  color: #999;
  font-size: 12px;
}

.src-yes {
  color: #f56c6c;
  font-weight: 600;
}

.src-no {
  color: #67c23a;
}

.src-na {
  color: #909399;
}

.history-used {
  color: #f56c6c;
}

.history-clean {
  color: #67c23a;
}

.history-error {
  color: #f56c6c;
}

.history-partial {
  color: #e6a23c;
}

.history-line {
  font-size: 12px;
  color: #909399;
  line-height: 1.7;
  margin-top: 2px;
}

.history-name {
  display: inline-block;
  min-width: 76px;
  color: #606266;
}

.history-date {
  margin-left: 6px;
  color: #b1b3b8;
}

.history-failed {
  color: #f56c6c;
}

.trace-link {
  margin-left: 6px;
  color: #409eff;
  text-decoration: none;
}

.trace-link:hover {
  text-decoration: underline;
}
</style>
