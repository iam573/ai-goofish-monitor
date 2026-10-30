<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { ChevronDown, RefreshCw, Smartphone } from 'lucide-vue-next'
import {
  IPHONE_PROXY_CERTIFICATE_URL,
  getIPhoneStatus,
  updateIPhoneChannel,
  updateIPhoneSelection,
  type IPhoneChannel,
  type IPhoneDevice,
  type IPhoneStatus,
} from '@/api/iphone'
import { Checkbox } from '@/components/ui/checkbox'
import { toast } from '@/components/ui/toast'

const POLL_INTERVAL_MS = 5000

const { t } = useI18n()
const root = ref<HTMLElement | null>(null)
const open = ref(false)
const loading = ref(false)
const saving = ref(false)
const status = ref<IPhoneStatus | null>(null)
const loadError = ref('')
const drafts = reactive<Record<string, { channel: IPhoneChannel; ip: string }>>({})
const savingChannel = ref('')
let pollTimer: ReturnType<typeof setInterval> | undefined

const devices = computed(() =>
  [...(status.value?.devices || [])].sort((a, b) => Number(b.connected) - Number(a.connected))
)
const anyConnected = computed(() => devices.value.some((device) => device.connected))
const guideSteps = ['cable', 'developerMode', 'trustWda', 'wifi', 'keepUnlocked', 'channels'] as const
const selectedUdids = computed(() => status.value?.selected_udids || [])
const selectedConnected = computed(() =>
  devices.value.filter((device) => device.connected && selectedUdids.value.includes(device.udid))
)
const fallbackDevice = computed(() => devices.value.find((device) => device.connected))
const proxyInUse = computed(() =>
  devices.value.some((device) => device.collection_channel === 'proxy' || drafts[device.udid]?.channel === 'proxy')
)
const proxyNeedsSetup = computed(() =>
  devices.value.some((device) => device.collection_channel === 'proxy' && !device.proxy_status?.client_connected)
)

const summary = computed(() => {
  if (!status.value) return loading.value ? t('iphoneDevices.loading') : t('iphoneDevices.unavailable')
  if (!status.value.platform_supported) return t('iphoneDevices.unsupported')
  if (selectedUdids.value.length) {
    return t('iphoneDevices.selectedSummary', {
      connected: selectedConnected.value.length,
      total: selectedUdids.value.length,
    })
  }
  return fallbackDevice.value
    ? t('iphoneDevices.fallbackSummary', { name: fallbackDevice.value.name })
    : t('iphoneDevices.noneConnected')
})

const dotClass = computed(() => {
  if (!status.value?.platform_supported) return 'bg-slate-300'
  const ready = selectedUdids.value.length ? selectedConnected.value.length > 0 : Boolean(fallbackDevice.value)
  return ready ? 'bg-emerald-500' : 'bg-red-500'
})

function savedDraft(device: IPhoneDevice) {
  return { channel: device.collection_channel, ip: device.proxy_client_ip || '' }
}

function draftFor(device: IPhoneDevice) {
  return drafts[device.udid] ?? (drafts[device.udid] = savedDraft(device))
}

function isDirty(device: IPhoneDevice) {
  const draft = drafts[device.udid]
  if (!draft) return false
  return draft.channel !== device.collection_channel
    || (draft.channel === 'proxy' && draft.ip.trim() !== (device.proxy_client_ip || ''))
}

function syncDrafts() {
  for (const device of status.value?.devices || []) {
    if (!isDirty(device)) drafts[device.udid] = savedDraft(device)
  }
}

async function load(quiet = false) {
  if (!quiet) loading.value = true
  loadError.value = ''
  try {
    status.value = await getIPhoneStatus()
    syncDrafts()
  } catch (error) {
    if (!quiet) loadError.value = error instanceof Error ? error.message : String(error)
  } finally {
    loading.value = false
  }
}

async function toggleDevice(udid: string, checked: boolean) {
  if (!status.value) return
  const previous = [...selectedUdids.value]
  const next = checked ? [...previous, udid] : previous.filter((item) => item !== udid)
  status.value.selected_udids = next
  saving.value = true
  try {
    const result = await updateIPhoneSelection(next)
    status.value.selected_udids = result.selected_udids
  } catch (error) {
    status.value.selected_udids = previous
    toast({
      title: t('iphoneDevices.saveFailed'),
      description: error instanceof Error ? error.message : String(error),
      variant: 'destructive',
    })
  } finally {
    saving.value = false
  }
}

async function saveChannel(device: IPhoneDevice) {
  const draft = drafts[device.udid]
  if (!draft) return
  const ip = draft.ip.trim()
  if (draft.channel === 'proxy' && !ip) {
    toast({ title: t('iphoneDevices.channel.ipRequired'), variant: 'destructive' })
    return
  }
  savingChannel.value = device.udid
  try {
    const result = await updateIPhoneChannel(device.udid, draft.channel, draft.channel === 'proxy' ? ip : device.proxy_client_ip || '')
    Object.assign(device, {
      collection_channel: result.collection_channel,
      proxy_client_ip: result.proxy_client_ip,
      proxy_status: result.proxy_status,
    })
    drafts[device.udid] = { channel: result.collection_channel, ip: result.proxy_client_ip || '' }
    toast({ title: t('iphoneDevices.channel.saved') })
    await load(true)
  } catch (error) {
    toast({
      title: t('iphoneDevices.channel.saveFailed'),
      description: error instanceof Error ? error.message : String(error),
      variant: 'destructive',
    })
  } finally {
    savingChannel.value = ''
  }
}

function proxyState(device: IPhoneDevice): { text: string; tone: string } | null {
  if (device.collection_channel !== 'proxy') return null
  const proxy = device.proxy_status
  if (!device.proxy_client_ip) return { text: t('iphoneDevices.proxyStatus.needIp'), tone: 'text-amber-600' }
  if (!proxy) return null
  if (proxy.error) return { text: t('iphoneDevices.proxyStatus.error', { error: proxy.error }), tone: 'text-red-600' }
  if (!proxy.ready) return { text: t('iphoneDevices.proxyStatus.preparing'), tone: 'text-slate-500' }
  if (proxy.busy) return { text: t('iphoneDevices.proxyStatus.busy'), tone: 'text-amber-600' }
  if (proxy.tls_failed) return { text: t('iphoneDevices.proxyStatus.tlsFailed'), tone: 'text-red-600' }
  if (proxy.client_connected) return { text: t('iphoneDevices.proxyStatus.connected'), tone: 'text-emerald-600' }
  return { text: t('iphoneDevices.proxyStatus.waiting'), tone: 'text-slate-500' }
}

function stopPolling() {
  if (pollTimer) clearInterval(pollTimer)
  pollTimer = undefined
}

function toggleOpen() {
  open.value = !open.value
  stopPolling()
  if (open.value) {
    load()
    pollTimer = setInterval(() => load(true), POLL_INTERVAL_MS)
  }
}

function close() {
  open.value = false
  stopPolling()
}

function handleClickOutside(event: MouseEvent) {
  if (open.value && root.value && !root.value.contains(event.target as Node)) close()
}

function handleKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape') close()
}

onMounted(() => {
  load()
  document.addEventListener('mousedown', handleClickOutside)
  document.addEventListener('keydown', handleKeydown)
})

onBeforeUnmount(() => {
  stopPolling()
  document.removeEventListener('mousedown', handleClickOutside)
  document.removeEventListener('keydown', handleKeydown)
})
</script>

<template>
  <div ref="root" class="relative">
    <button
      type="button"
      class="inline-flex items-center gap-2 rounded-full border border-slate-200/80 bg-white/80 py-1 pl-2.5 pr-2 text-left shadow-sm backdrop-blur transition-colors hover:bg-slate-50"
      :aria-expanded="open"
      aria-haspopup="dialog"
      :aria-label="t('iphoneDevices.title')"
      @click="toggleOpen"
    >
      <Smartphone class="h-4 w-4 text-slate-500" />
      <span class="hidden flex-col leading-tight lg:flex">
        <small class="text-[10px] text-slate-400">{{ t('iphoneDevices.title') }}</small>
        <b class="max-w-[160px] truncate text-[11px] font-bold text-slate-700">{{ summary }}</b>
      </span>
      <i class="h-2 w-2 rounded-full" :class="dotClass" />
      <ChevronDown class="h-3.5 w-3.5 text-slate-400" />
    </button>

    <div
      v-if="open"
      class="absolute right-0 top-full z-[110] mt-2 max-h-[80vh] w-96 overflow-y-auto rounded-xl border border-slate-200 bg-white p-3 text-sm shadow-xl"
      role="dialog"
      :aria-label="t('iphoneDevices.title')"
    >
      <div class="mb-2 flex items-start justify-between gap-2">
        <div>
          <p class="font-bold text-slate-800">{{ t('iphoneDevices.title') }}</p>
          <p class="text-xs text-slate-500">{{ t('iphoneDevices.hint') }}</p>
        </div>
        <button
          type="button"
          class="rounded-md p-1 text-slate-500 hover:bg-slate-100 disabled:opacity-50"
          :disabled="loading"
          :title="t('iphoneDevices.refresh')"
          :aria-label="t('iphoneDevices.refresh')"
          @click="load()"
        >
          <RefreshCw class="h-4 w-4" :class="loading ? 'animate-spin' : ''" />
        </button>
      </div>

      <p v-if="loadError" class="rounded-md bg-red-50 p-2 text-xs text-red-600">{{ loadError }}</p>
      <p v-else-if="status && !status.platform_supported" class="rounded-md bg-slate-50 p-2 text-xs text-slate-600">
        {{ status.device_error }}
      </p>
      <template v-else-if="status">
        <p v-if="status.device_error" class="mb-2 rounded-md bg-red-50 p-2 text-xs text-red-600">{{ status.device_error }}</p>
        <p v-if="!devices.length" class="py-3 text-center text-xs text-slate-500">{{ t('iphoneDevices.noDevices') }}</p>
        <ul v-else class="space-y-1">
          <li
            v-for="device in devices"
            :key="device.udid"
            class="rounded-lg px-2 py-2 hover:bg-slate-50"
            :class="device.connected ? '' : 'opacity-60'"
          >
            <label class="flex cursor-pointer items-center gap-3">
              <Checkbox
                :model-value="selectedUdids.includes(device.udid)"
                :disabled="saving"
                @update:model-value="(value) => toggleDevice(device.udid, value === true)"
              />
              <span class="min-w-0 flex-1">
                <span class="block truncate font-medium text-slate-800">{{ device.name }}</span>
                <span class="block text-xs text-slate-500">iOS {{ device.os_version || '?' }} · {{ device.udid.slice(-6) }}</span>
              </span>
              <span
                class="shrink-0 rounded-full px-2 py-0.5 text-[10px] font-bold"
                :class="device.busy
                  ? 'bg-amber-50 text-amber-700'
                  : device.connected ? 'bg-emerald-50 text-emerald-700' : 'bg-slate-100 text-slate-500'"
              >
                {{ device.busy ? t('iphoneDevices.busy') : device.connected ? t('iphoneDevices.idle') : device.status }}
              </span>
            </label>
            <div class="mt-2 flex flex-wrap items-center gap-1.5 pl-7 text-xs">
              <select
                v-model="draftFor(device).channel"
                class="h-7 rounded-md border border-slate-200 bg-white px-1.5 text-xs text-slate-700"
                :aria-label="t('iphoneDevices.channel.label')"
              >
                <option value="ui">{{ t('iphoneDevices.channel.ui') }}</option>
                <option value="proxy">{{ t('iphoneDevices.channel.proxy') }}</option>
              </select>
              <input
                v-if="draftFor(device).channel === 'proxy'"
                v-model="draftFor(device).ip"
                class="h-7 w-36 rounded-md border border-slate-200 px-2 text-xs"
                inputmode="decimal"
                :placeholder="t('iphoneDevices.channel.ipPlaceholder')"
                @keydown.enter.prevent="saveChannel(device)"
              />
              <button
                v-if="isDirty(device)"
                type="button"
                class="h-7 rounded-md bg-slate-900 px-2.5 text-xs font-medium text-white disabled:opacity-50"
                :disabled="savingChannel === device.udid"
                @click="saveChannel(device)"
              >
                {{ t('iphoneDevices.channel.save') }}
              </button>
              <template v-for="state in [proxyState(device)]" :key="device.udid">
                <span v-if="state && !isDirty(device)" :class="state.tone">{{ state.text }}</span>
              </template>
            </div>
            <p
              v-if="device.collection_channel === 'ui' && device.proxy_client_ip && !isDirty(device)"
              class="mt-1 pl-7 text-[11px] text-slate-400"
            >
              {{ t('iphoneDevices.channel.uiProxyHint') }}
            </p>
          </li>
        </ul>
        <p class="mt-2 border-t border-slate-100 pt-2 text-xs text-slate-500">
          {{ selectedUdids.length ? t('iphoneDevices.dispatchHint') : t('iphoneDevices.fallbackHint') }}
        </p>

        <details
          v-if="proxyInUse"
          class="mt-2 rounded-lg bg-sky-50 px-3 py-2 text-xs text-slate-600"
          :open="proxyNeedsSetup"
        >
          <summary class="cursor-pointer select-none font-bold text-slate-700">{{ t('iphoneDevices.proxyGuide.title') }}</summary>
          <ol class="mt-2 list-decimal space-y-1.5 pl-4">
            <li>{{ t('iphoneDevices.proxyGuide.network') }}</li>
            <li>
              <template v-if="status.proxy.addresses.length">
                {{ t('iphoneDevices.proxyGuide.server', { addresses: status.proxy.addresses.join(' / '), port: status.proxy.port }) }}
              </template>
              <template v-else>{{ t('iphoneDevices.proxyGuide.serverUnknown', { port: status.proxy.port }) }}</template>
            </li>
            <li>
              {{ t('iphoneDevices.proxyGuide.certificate') }}
              <a
                v-if="status.proxy.certificate_ready"
                :href="IPHONE_PROXY_CERTIFICATE_URL"
                class="text-sky-700 underline"
                download
              >{{ t('iphoneDevices.proxyGuide.certificateDownload') }}</a>
            </li>
            <li>{{ t('iphoneDevices.proxyGuide.clientIp') }}</li>
            <li>{{ t('iphoneDevices.proxyGuide.turnOff') }}</li>
            <li>{{ t('iphoneDevices.proxyGuide.shared') }}</li>
          </ol>
        </details>
      </template>

      <details
        v-if="!status || status.platform_supported"
        class="group mt-2 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600"
        :open="status ? !anyConnected : false"
      >
        <summary class="cursor-pointer select-none font-bold text-slate-700">{{ t('iphoneDevices.guide.title') }}</summary>
        <ol class="mt-2 list-decimal space-y-1.5 pl-4">
          <li v-for="step in guideSteps" :key="step">{{ t(`iphoneDevices.guide.${step}`) }}</li>
        </ol>
      </details>
    </div>
  </div>
</template>
