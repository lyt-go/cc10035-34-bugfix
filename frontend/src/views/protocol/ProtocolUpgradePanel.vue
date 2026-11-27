<template>
  <section class="lifecycle-panel" data-testid="protocol-upgrade-panel">
    <header class="panel-head">
      <h3>协议版本切换工作台</h3>
      <p class="page-desc">
        维护员凭据校验后发起升版：新版本待验证期间旧版本继续在服务，确认后才切换；
        同一笔票据重复提交取回同一办理结果，同后缀编号不会重复生成。
      </p>
    </header>

    <div class="panel-grid">
      <form class="panel-card" @submit.prevent="saveCredential">
        <h4>1. 维护员切换凭据</h4>
        <label class="create-field"><span>维护员账号</span>
          <input v-model="operator" data-testid="cred-operator" required />
        </label>
        <label class="create-field"><span>切换凭据</span>
          <input v-model="credential" data-testid="cred-token" required />
        </label>
        <label class="create-field"><span>授权协议族（可选）</span>
          <input v-model="scopeRoot" data-testid="cred-scope" placeholder="留空表示不限制协议族" />
        </label>
        <button class="btn primary" data-testid="cred-save" type="submit">配置切换凭据</button>
      </form>

      <form class="panel-card" @submit.prevent="submitApply">
        <h4>2. 发起版本升级</h4>
        <label class="create-field"><span>生效配置编号</span>
          <input v-model="applyForm.configId" data-testid="up-config-id" required placeholder="如 P-11" />
        </label>
        <label class="create-field"><span>新版本号</span>
          <input v-model="applyForm.newVersion" data-testid="up-new-version" required placeholder="如 v3" />
        </label>
        <p class="hint" v-if="currentTicket">
          当前办理票据：<code>{{ currentTicket }}</code>（响应丢失后凭此票据取回同一笔申请）
        </p>
        <button class="btn primary" data-testid="up-apply" type="submit">发起升版申请</button>
        <button
          class="btn"
          data-testid="up-confirm"
          type="button"
          :disabled="!currentTicket"
          @click="submitConfirm"
        >
          确认版本切换
        </button>
      </form>
    </div>

    <p v-if="message" class="hint" :class="{ 'error-text': !lastOk }" data-testid="up-message">{{ message }}</p>

    <div class="panel-card">
      <div class="panel-title-row">
        <h4>升版办理记录</h4>
        <button class="btn ghost" type="button" data-testid="up-refresh" @click="loadHistory">刷新记录</button>
      </div>
      <table class="data-table">
        <thead>
          <tr>
            <th>票据</th><th>协议族</th><th>原编号</th><th>新编号</th><th>维护员</th><th>状态</th><th>确认时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in history" :key="item.票据" :data-testid="`up-row-${item.票据}`">
            <td>{{ item.票据 }}</td>
            <td>{{ item.协议族 }}</td>
            <td>{{ item.原配置编号 }}</td>
            <td>{{ item.新配置编号 }}</td>
            <td>{{ item.维护员 }}</td>
            <td>{{ item.状态 }}</td>
            <td>{{ item.确认时间 ?? '—' }}</td>
          </tr>
          <tr v-if="!history.length">
            <td colspan="7" class="empty-state">暂无升版办理记录</td>
          </tr>
        </tbody>
      </table>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  applyUpgrade,
  configureOperator,
  confirmUpgrade,
  fetchUpgradeHistory,
  type UpgradeTicket,
} from '@/api/protocol-lifecycle'

const STORAGE_KEY = 'protocol-upgrade-context'

const operator = ref('')
const credential = ref('')
const scopeRoot = ref('')
const applyForm = ref({ configId: 'P-11', newVersion: '' })
const currentTicket = ref('')
const history = ref<UpgradeTicket[]>([])
const message = ref('')
const lastOk = ref(true)

function persistContext() {
  localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({
      operator: operator.value,
      credential: credential.value,
      scopeRoot: scopeRoot.value,
      ticket: currentTicket.value,
      configId: applyForm.value.configId,
    }),
  )
}

function restoreContext() {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) return
  try {
    const ctx = JSON.parse(raw) as Record<string, string>
    operator.value = ctx.operator ?? ''
    credential.value = ctx.credential ?? ''
    scopeRoot.value = ctx.scopeRoot ?? ''
    currentTicket.value = ctx.ticket ?? ''
    applyForm.value.configId = ctx.configId ?? 'P-11'
  } catch {
    localStorage.removeItem(STORAGE_KEY)
  }
}

function flash(ok: boolean, text: string) {
  lastOk.value = ok
  message.value = text
}

async function saveCredential() {
  try {
    const result = await configureOperator(operator.value, credential.value, scopeRoot.value)
    flash(result.ok, result.message)
    if (result.ok) persistContext()
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '凭据配置失败')
  }
}

async function submitApply() {
  try {
    // 沿用未办结票据，保证响应丢失后回到页面再提交仍是同一笔申请。
    const ticket = currentTicket.value || `T-${Date.now()}`
    const result = await applyUpgrade(
      applyForm.value.configId,
      applyForm.value.newVersion,
      operator.value,
      credential.value,
      ticket,
    )
    flash(result.ok, result.idempotent ? `${result.message}（幂等取回）` : result.message)
    if (result.ok) {
      currentTicket.value = result.ticket.票据
      applyForm.value.configId = result.ticket.原配置编号
      persistContext()
      await loadHistory()
    }
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '升版申请失败')
  }
}

async function submitConfirm() {
  try {
    const result = await confirmUpgrade(currentTicket.value, operator.value, credential.value)
    flash(result.ok, result.idempotent ? `${result.message}（幂等取回）` : result.message)
    if (result.ok) {
      // 办结后清理票据，下一次升版将生成新的版本身份。
      currentTicket.value = ''
      persistContext()
      await loadHistory()
    }
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '升版确认失败')
  }
}

async function loadHistory() {
  try {
    const result = await fetchUpgradeHistory()
    history.value = result.applications
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '升版记录读取失败')
  }
}

onMounted(() => {
  restoreContext()
  void loadHistory()
})
</script>
