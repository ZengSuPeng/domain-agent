<template>
  <div class="login-page">
    <main class="login-card">
      <div class="login-brand">
        <span class="login-logo">🌐</span>
        <span class="login-brand-text">汉盈域名检测</span>
      </div>
      <p class="login-sub">域名生成 · 可注册性 · 合规风险 · 历史痕迹</p>

      <el-alert
        v-if="error"
        class="login-alert"
        :title="error"
        type="error"
        :closable="false"
        show-icon
      />

      <el-alert
        v-else-if="!configured"
        class="login-alert"
        title="飞书登录尚未配置"
        description="请在后端配置 FEISHU_APP_ID 与 FEISHU_APP_SECRET 后再登录。"
        type="warning"
        :closable="false"
        show-icon
      />

      <el-button
        class="feishu-btn"
        :loading="loading"
        :disabled="!configured"
        @click="$emit('login')"
      >
        <span v-if="!loading" class="feishu-mark" aria-hidden="true">飞</span>
        <span>使用飞书账号登录</span>
      </el-button>

      <div class="login-links">
        <el-button link size="small" :disabled="loading" @click="$emit('retry')">
          连接异常？点此重试
        </el-button>
      </div>

      <p class="login-hint">
        仅限企业成员访问。登录时由飞书确认你的身份，系统不会接触你的飞书密码。
      </p>
    </main>
    <footer class="login-footer">Domain Agent · 内部运营工具</footer>
  </div>
</template>

<script setup>
defineProps({
  error: { type: String, default: '' },
  loading: { type: Boolean, default: false },
  configured: { type: Boolean, default: true },
})
defineEmits(['login', 'retry'])
</script>

<style scoped>
.login-page {
  position: relative;
  min-height: 100vh;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  padding: 24px;
  overflow: hidden;
  background:
    radial-gradient(900px 520px at 16% 6%, rgba(51, 112, 255, 0.18), transparent 62%),
    radial-gradient(760px 460px at 88% 94%, rgba(0, 196, 163, 0.14), transparent 66%),
    #f5f7fa;
}

/* 淡网格，让纯色背景不至于太空 */
.login-page::before {
  content: '';
  position: absolute;
  inset: 0;
  background-image:
    linear-gradient(rgba(31, 45, 61, 0.035) 1px, transparent 1px),
    linear-gradient(90deg, rgba(31, 45, 61, 0.035) 1px, transparent 1px);
  background-size: 44px 44px;
  mask-image: radial-gradient(circle at 50% 45%, #000 0%, transparent 78%);
  pointer-events: none;
}

.login-card {
  position: relative;
  z-index: 1;
  width: 100%;
  max-width: 420px;
  padding: 36px 34px 26px;
  text-align: center;
  background: #fff;
  border: 1px solid rgba(255, 255, 255, 0.8);
  border-radius: 20px;
  box-shadow:
    0 24px 60px rgba(23, 43, 99, 0.12),
    0 2px 6px rgba(23, 43, 99, 0.06);
}

.login-brand {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 10px;
  font-size: 20px;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #1f2d3d;
}

.login-logo {
  font-size: 24px;
  line-height: 1;
}

.login-sub {
  margin: 10px 0 26px;
  font-size: 13px;
  letter-spacing: 0.4px;
  color: #8a94a6;
}

.login-alert {
  margin-bottom: 16px;
  text-align: left;
}

.feishu-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  width: 100%;
  height: 46px;
  margin-top: 6px;
  border: none;
  border-radius: 10px;
  background: #3370ff;
  color: #fff;
  font-size: 15px;
  font-weight: 600;
  letter-spacing: 0.3px;
  box-shadow: 0 8px 18px rgba(51, 112, 255, 0.28);
  transition:
    background-color 0.18s ease,
    box-shadow 0.18s ease,
    transform 0.18s ease;
}

.feishu-btn:hover,
.feishu-btn:focus {
  background: #245bdb;
  color: #fff;
  box-shadow: 0 10px 22px rgba(51, 112, 255, 0.34);
}

.feishu-btn:active {
  transform: translateY(1px);
}

.feishu-btn.is-disabled,
.feishu-btn.is-disabled:hover,
.feishu-btn.is-disabled:focus {
  background: #a8c0ff;
  color: #fff;
  box-shadow: none;
  cursor: not-allowed;
}

.feishu-mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 6px;
  background: #fff;
  color: #3370ff;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
}

.login-links {
  margin-top: 8px;
}

.login-hint {
  margin: 18px 0 0;
  font-size: 12px;
  line-height: 1.7;
  color: #a8b0bf;
}

.login-footer {
  position: relative;
  z-index: 1;
  font-size: 12px;
  color: #b3bac7;
}
</style>
