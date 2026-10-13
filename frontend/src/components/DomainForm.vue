<template>
  <el-form :model="form" label-width="100px" @submit.prevent="onSubmit">
    <el-form-item label="主题">
      <el-input v-model="form.theme" placeholder="例如：衣服" />
    </el-form-item>

    <el-form-item label="风格描述">
      <el-input
        v-model="form.desc"
        type="textarea"
        :rows="3"
        placeholder="例如：女装，连衣裙，短裙系列"
      />
    </el-form-item>

    <el-form-item>
      <el-button type="primary" :loading="loading" @click="onSubmit">
        {{ loading ? '生成中...' : '开始生成' }}
      </el-button>
    </el-form-item>
  </el-form>
</template>

<script setup>
import { ref } from 'vue'
import { ElMessage } from 'element-plus'

const props = defineProps({
  loading: { type: Boolean, default: false },
})

const emit = defineEmits(['generate'])

const form = ref({
  theme: '衣服',
  desc: '女装，连衣裙，短裙系列',
})

function onSubmit() {
  if (!form.value.theme || !form.value.desc) {
    ElMessage.warning('请填写主题和风格描述')
    return
  }
  emit('generate', { theme: form.value.theme, desc: form.value.desc })
}
</script>
