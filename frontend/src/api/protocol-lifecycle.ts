/**
 * 协议升版与设备绑定生命周期接口封装。
 * 对应后端 /api/protocol-lifecycle，承载工作台版本切换与注册页首次绑定。
 */
import { request } from './client'

export type FamilyVersion = {
  配置编号: string
  配置版本: string
  status: string
  来源编号?: string
  替代编号?: string
}

export type UpgradeTicket = {
  id: number
  票据: string
  协议族: string
  原配置编号: string
  原版本序号: number
  新配置编号: string
  新版本序号: number
  配置版本: string
  维护员: string
  状态: string
  申请时间: string
  确认时间?: string
}

export type BindingRecord = {
  id: number
  设备编号: string
  协议族: string
  用户选择编号: string
  生效版本编号: string
  目标配置编号: string
  等待票据: string
  绑定状态: string
  绑定时间: string
  确认时间?: string
}

export type BindingDetail = {
  ok: boolean
  message?: string
  device: Record<string, string>
  original_active: { 配置编号: string; 说明: string }
  selected_upgrade: { 配置编号: string; 等待票据: string; 说明: string }
  pending_binding: { 目标配置编号: string; 绑定状态: string; 说明: string }
  current_active: { 配置编号: string; status: string }
  family_versions: FamilyVersion[]
}

type LifecycleResponse<T = Record<string, unknown>> = T & {
  ok: boolean
  message: string
  idempotent?: boolean
}

async function postJson<T>(path: string, payload: Record<string, unknown>): Promise<LifecycleResponse<T>> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(payload) })
  return (await response.json()) as LifecycleResponse<T>
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}，数据未更新`)
  return (await response.json()) as T
}

export function configureOperator(operator: string, credential: string, scopeRoot = '') {
  return postJson('/api/protocol-lifecycle/operators/credential', {
    operator,
    credential,
    scope_root: scopeRoot,
  })
}

export function resolveActiveProtocol(configId: string) {
  return getJson<{ ok: boolean; 协议族: string; active: FamilyVersion | null; family_versions: FamilyVersion[] }>(
    `/api/protocol-lifecycle/families/${encodeURIComponent(configId)}/active`,
  )
}

export function applyUpgrade(
  configId: string,
  newVersion: string,
  operator: string,
  credential: string,
  ticket: string,
) {
  return postJson<{ ticket: UpgradeTicket; new_entry: FamilyVersion }>('/api/protocol-lifecycle/upgrades/apply', {
    config_id: configId,
    new_version: newVersion,
    operator,
    credential,
    ticket,
  })
}

export function confirmUpgrade(ticket: string, operator: string, credential: string) {
  return postJson<{ ticket: UpgradeTicket; confirmation: Record<string, unknown>; active_entry: FamilyVersion }>(
    '/api/protocol-lifecycle/upgrades/confirm',
    { ticket, operator, credential },
  )
}

export function fetchUpgradeHistory(configId = '') {
  const query = configId ? `?config_id=${encodeURIComponent(configId)}` : ''
  return getJson<{ ok: boolean; applications: UpgradeTicket[]; confirmations: Record<string, unknown>[] }>(
    `/api/protocol-lifecycle/upgrades/history${query}`,
  )
}

export function bindDevice(deviceId: string, deviceType: string, protocolId: string) {
  return postJson<{ entry: Record<string, unknown>; binding: BindingRecord; family_versions: FamilyVersion[] }>(
    '/api/protocol-lifecycle/bindings',
    { device_id: deviceId, device_type: deviceType, protocol_id: protocolId },
  )
}

export function confirmDeviceBinding(deviceId: string) {
  return postJson<{ binding: BindingRecord }>('/api/protocol-lifecycle/bindings/confirm', {
    device_id: deviceId,
  })
}

export function fetchBindingDetail(deviceId: string) {
  return getJson<BindingDetail>(`/api/protocol-lifecycle/bindings/${encodeURIComponent(deviceId)}`)
}

export function reportDeviceProtocol(deviceId: string) {
  return postJson<{ report: Record<string, unknown> }>('/api/protocol-lifecycle/bindings/report', {
    device_id: deviceId,
  })
}
