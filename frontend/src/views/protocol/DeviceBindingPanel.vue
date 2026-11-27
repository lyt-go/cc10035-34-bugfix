<template>
  <section class="lifecycle-panel" data-testid="device-binding-panel">
    <header class="panel-head">
      <h3>设备注册 · 协议首次绑定</h3>
      <p class="page-desc">
        注册页只按协议族定位实际生效项：选择待验证升级时绑定申请挂起等待新版本，
        已废弃条目不会被冒用；绑定确认仅在实际使用的配置生效后放行。
      </p>
    </header>

    <div class="panel-grid">
      <form class="panel-card" @submit.prevent="locateFamily">
        <h4>1. 定位协议族生效项</h4>
        <label class="create-field"><span>协议编号（任一族内编号）</span>
          <input v-model="lookupId" data-testid="bind-lookup-id" required placeholder="如 P-11 / P-11-v2" />
        </label>
        <button class="btn primary" data-testid="bind-locate" type="submit">解析生效版本</button>
        <table v-if="familyVersions.length" class="data-table family-table">
          <thead>
            <tr><th>配置编号</th><th>配置版本</th><th>状态</th></tr>
          </thead>
          <tbody>
            <tr
              v-for="version in familyVersions"
              :key="version.配置编号"
              :data-testid="`family-row-${version.配置编号}`"
              :data-status="version.status"
            >
              <td>
                <label class="radio-cell">
                  <input
                    type="radio"
                    name="family-pick"
                    :value="version.配置编号"
                    v-model="bindForm.protocolId"
                    :disabled="version.status === '已废弃'"
                  />
                  {{ version.配置编号 }}
                  <em v-if="version.status === '已废弃'" class="tag deprecated">已废弃不可绑定</em>
                  <em v-else-if="version.status === '已生效'" class="tag active">当前生效</em>
                  <em v-else class="tag pending">待验证</em>
                </label>
              </td>
              <td>{{ version.配置版本 }}</td>
              <td>{{ version.status }}</td>
            </tr>
          </tbody>
        </table>
      </form>

      <form class="panel-card" @submit.prevent="submitBind">
        <h4>2. 提交首次绑定</h4>
        <label class="create-field"><span>设备编号</span>
          <input v-model="bindForm.deviceId" data-testid="bind-device-id" required placeholder="如 D-0001" />
        </label>
        <label class="create-field"><span>设备类型</span>
          <input v-model="bindForm.deviceType" data-testid="bind-device-type" required />
        </label>
        <label class="create-field"><span>选择的协议编号</span>
          <input v-model="bindForm.protocolId" data-testid="bind-protocol-id" required />
        </label>
        <button class="btn primary" data-testid="bind-submit" type="submit">提交注册绑定</button>
        <button class="btn" data-testid="bind-confirm" type="button" :disabled="!bindForm.deviceId" @click="submitConfirm">
          确认绑定
        </button>
        <button class="btn ghost" data-testid="bind-report" type="button" :disabled="!bindForm.deviceId" @click="submitReport">
          上报实际有效配置
        </button>
      </form>
    </div>

    <p v-if="message" class="hint" :class="{ 'error-text': !lastOk }" data-testid="bind-message">{{ message }}</p>

    <div class="panel-card" v-if="detail">
      <div class="panel-title-row">
        <h4>注册详情（重新打开即可核对绑定走向）</h4>
        <button class="btn ghost" type="button" data-testid="bind-refresh" @click="loadDetail">刷新详情</button>
      </div>
      <dl class="detail-grid">
        <div><dt>原有效版本（注册时仍在服务）</dt><dd>{{ detail.original_active.配置编号 || '—' }}</dd></div>
        <div><dt>用户选择的升级</dt><dd>{{ detail.selected_upgrade.配置编号 || '—' }}
          <em v-if="detail.selected_upgrade.等待票据" class="tag pending">等待票据 {{ detail.selected_upgrade.等待票据 }}</em>
        </dd></div>
        <div><dt>尚未成功的绑定</dt><dd>{{ detail.pending_binding.目标配置编号 || '—' }}（{{ detail.pending_binding.绑定状态 }}）</dd></div>
        <div><dt>当前生效项</dt><dd>{{ detail.current_active.配置编号 || '—' }}（{{ detail.current_active.status }}）</dd></div>
        <div><dt>设备绑定确认</dt><dd>{{ detail.device.绑定确认 }}</dd></div>
        <div><dt>最近上报配置</dt><dd>{{ detail.device.上报协议编号 || '未上报' }}</dd></div>
      </dl>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from 'vue'

import {
  bindDevice,
  confirmDeviceBinding,
  fetchBindingDetail,
  reportDeviceProtocol,
  resolveActiveProtocol,
  type BindingDetail,
  type FamilyVersion,
} from '@/api/protocol-lifecycle'

const lookupId = ref('P-11')
const familyVersions = ref<FamilyVersion[]>([])
const bindForm = ref({ deviceId: '', deviceType: '', protocolId: '' })
const detail = ref<BindingDetail | null>(null)
const message = ref('')
const lastOk = ref(true)

function flash(ok: boolean, text: string) {
  lastOk.value = ok
  message.value = text
}

async function locateFamily() {
  try {
    const result = await resolveActiveProtocol(lookupId.value)
    familyVersions.value = result.family_versions
    const active = result.family_versions.find((item) => item.status === '已生效')
    if (active && !bindForm.value.protocolId) bindForm.value.protocolId = active.配置编号
    flash(true, `协议族 ${result.协议族} 当前生效项：${result.active ? result.active.配置编号 : '无'}`)
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '生效项解析失败')
  }
}

async function submitBind() {
  try {
    const result = await bindDevice(
      bindForm.value.deviceId,
      bindForm.value.deviceType,
      bindForm.value.protocolId,
    )
    flash(result.ok, result.message)
    if (result.ok) {
      familyVersions.value = result.family_versions
      await loadDetail()
    }
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '注册绑定失败')
  }
}

async function submitConfirm() {
  try {
    const result = await confirmDeviceBinding(bindForm.value.deviceId)
    flash(result.ok, result.message)
    if (result.ok) await loadDetail()
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '绑定确认失败')
  }
}

async function submitReport() {
  try {
    const result = await reportDeviceProtocol(bindForm.value.deviceId)
    flash(result.ok, result.message)
    if (result.ok) await loadDetail()
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '配置上报失败')
  }
}

async function loadDetail() {
  if (!bindForm.value.deviceId) return
  try {
    detail.value = await fetchBindingDetail(bindForm.value.deviceId)
  } catch (error) {
    flash(false, error instanceof Error ? error.message : '注册详情读取失败')
  }
}
</script>
