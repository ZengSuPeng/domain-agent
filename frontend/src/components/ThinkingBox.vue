<template>
  <div class="thinking-box">
    <div class="thinking-header" @click="expanded = !expanded">
      <span>
        💭 思考过程
        <span class="thinking-badge">点击{{ expanded ? '收起' : '展开' }}</span>
      </span>
      <span class="arrow" :class="{ expanded }">▶</span>
    </div>
    <div v-show="expanded" class="thinking-body" ref="bodyRef">{{ content }}</div>
  </div>
</template>

<script setup>
import { ref, watch, nextTick } from 'vue'

const props = defineProps({
  content: { type: String, default: '' },
  autoExpand: { type: Boolean, default: false },
})

const expanded = ref(true)
const bodyRef = ref(null)

// 有新内容时自动展开并滚动到底部
watch(
  () => props.content,
  async () => {
    if (props.autoExpand) expanded.value = true
    await nextTick()
    if (bodyRef.value) {
      bodyRef.value.scrollTop = bodyRef.value.scrollHeight
    }
  }
)
</script>

<style scoped>
.thinking-box {
  margin: 16px 0;
  border: 1px solid #ebeef5;
  border-radius: 6px;
}

.thinking-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 10px 14px;
  background: #f4f4f5;
  cursor: pointer;
  font-size: 14px;
  color: #606266;
  user-select: none;
  border-radius: 6px 6px 0 0;
}

.thinking-header:hover {
  background: #e9e9eb;
}

.thinking-header .arrow {
  display: inline-block;
  transition: transform 0.2s;
  font-size: 12px;
  margin-left: 6px;
}

.thinking-header .arrow.expanded {
  transform: rotate(90deg);
}

.thinking-body {
  padding: 12px 14px;
  background: #fafafa;
  font-size: 13px;
  line-height: 1.7;
  color: #909399;
  white-space: pre-wrap;
  word-break: break-all;
  overflow-wrap: break-word;
  max-height: 300px;
  overflow-y: auto;
  border-radius: 0 0 6px 6px;
}

.thinking-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  background: #ecf5ff;
  color: #409eff;
  font-size: 12px;
  margin-left: 8px;
}
</style>
