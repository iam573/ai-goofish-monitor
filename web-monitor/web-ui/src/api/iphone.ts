import { http } from '@/lib/http'

export interface IPhoneDevice {
  udid: string
  name: string
  os_version: string
  connected: boolean
  status: string
  selected: boolean
  busy: boolean
  collection_channel: IPhoneChannel
  proxy_client_ip: string | null
  proxy_status: IPhoneProxyStatus | null
}

export type IPhoneChannel = 'ui' | 'proxy'

export interface IPhoneProxyStatus {
  running: boolean
  ready: boolean
  error: string
  port: number
  addresses: string[]
  certificate_ready: boolean
  client_connected: boolean
  tls_failed: boolean
  client_ip: string
  last_seen: number | null
  busy?: boolean
}

export interface IPhoneStatus {
  platform_supported: boolean
  devices: IPhoneDevice[]
  device_error: string | null
  selected_udid: string | null
  selected_udids: string[]
  appium_server_url: string
  appium_ready: boolean
  appium_autostart: boolean
  wda_configured: boolean
  wda_auto_sign: boolean
  wda_expires_at: string | null
  wda_issue: string | null
  proxy: IPhoneProxyStatus
}

export interface IPhoneChannelResult {
  udid: string
  collection_channel: IPhoneChannel
  proxy_client_ip: string | null
  proxy_status: IPhoneProxyStatus | null
}

export const IPHONE_PROXY_CERTIFICATE_URL = '/api/iphone/proxy/certificate'

export async function updateIPhoneChannel(
  udid: string,
  channel: IPhoneChannel,
  clientIp: string,
): Promise<IPhoneChannelResult> {
  return await http(`/api/iphone/devices/${encodeURIComponent(udid)}/channel`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ channel, client_ip: clientIp }),
  })
}

export async function getIPhoneStatus(): Promise<IPhoneStatus> {
  return await http('/api/iphone/status')
}

export async function updateIPhoneSelection(udids: string[]): Promise<{ selected_udids: string[] }> {
  return await http('/api/iphone/selection', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ udids }),
  })
}
