import { initSmart } from "./smart-ui.js";
import { capacityOptions, filterRunItems, normalizeCapacity } from "./run-filters.js";
import { codeLabels, codeOrder, runOutcome } from "./run-outcome.js";
const state = { products: [], rules: null, overview: null, targets: [], devices: [] };
const gradeOrder = ["靓机", "小花", "大花", "外爆", "内爆可测"];

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return response.status === 204 ? null : response.json();
}

function toast(message, error = false) {
  const element = document.querySelector("#toast");
  element.textContent = message;
  element.className = error ? "show error" : "show";
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => { element.className = ""; }, 3500);
}

function escapeHtml(value) {
  const div = document.createElement("div");
  div.textContent = value == null ? "" : String(value);
  return div.innerHTML;
}

function displayStorage(value) {
  const storage = String(value || "");
  return storage.endsWith("T") ? `${storage}B` : `${storage}G`;
}

function quoteDifferenceLabel(quotePrice, itemPrice) {
  const difference = Number(itemPrice) - Number(quotePrice);
  if (difference > 0) return `高于回收报价 ¥${Math.round(difference)}`;
  if (difference < 0) return `低于回收报价 ¥${Math.round(-difference)}`;
  return "与回收报价持平";
}

function formatTime(value) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
}

/** 今天的显示「14:32」，更早的显示「10-18 14:32」。 */
function shortTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  const now = new Date();
  const hm = date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit", hour12: false });
  if (date.toDateString() === now.toDateString()) return hm;
  return `${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")} ${hm}`;
}

function ago(value) {
  if (!value) return "从未";
  const minutes = Math.round((Date.now() - new Date(value).getTime()) / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  if (minutes < 60 * 24) return `${Math.floor(minutes / 60)} 小时前`;
  return shortTime(value);
}

function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/* ------------------------------------------------------------------ 流程状态 */

function quoteSyncDate(batch) {
  return (batch?.fetched_at || batch?.quote_date || "").slice(0, 10);
}

async function loadOverview() {
  const value = await api("/api/overview");
  state.overview = value;
  const rules = value.rules;
  const health = document.querySelector("#health");
  health.textContent = "服务正常 · iPhone 采集";
  state.smart = value.smart;
  health.classList.add("online");
  health.classList.remove("warn");

  const noWarranty = value.quotes.find(q => q.category_id === rules.quote_category) || value.quotes[0];
  const quoteFresh = Boolean(noWarranty?.fresh);
  const quoteStep = {
    label: "① 每日报价",
    main: quoteSyncDate(noWarranty) ? `${quoteSyncDate(noWarranty).slice(5)} · ${noWarranty.entry_count} 条` : "尚未同步",
    sub: noWarranty?.parse_status === "failed" ? "图片已下载，报价识别失败，请查看每日报价"
      : noWarranty?.parse_status === "parsing" ? "图片已下载，正在识别报价"
      : noWarranty?.parse_status === "needs_review" ? "图片已下载，尚未识别到有效报价"
      : quoteFresh ? `今天已同步 · 每天 ${value.quote_schedule} 同步一次` : `沿用最近报价 · 每天 ${value.quote_schedule} 同步一次`,
    tone: quoteFresh ? "ok" : "warn",
  };
  const ruleStep = {
    label: "② 监控规则",
    main: `${rules.region_city || "全国"} · 全部 iPhone 机型`,
    sub: `${rules.quote_category === "100027" ? "有保" : "无保"}${value.smart?.enabled ? " · 自动成色与扣费" : rules.quote_grade}回收价 · 最多高于 ¥${rules.max_over_quote}`,
    tone: "ok",
  };
  const schedule = value.schedule;
  const searchStep = {
    label: "③ 闲鱼搜索",
    main: value.busy ? (value.live ? `正在搜索 iPhone ${value.live.model}` : "正在搜索…") : schedule.active ? (schedule.next_label ? `下一个：iPhone ${schedule.next_label}` : "本轮已搜完") : "未在搜索",
    sub: value.live ? `${value.live.stage} · 已看 ${value.live.observed} 件 · 命中 ${value.live.matched}` : schedule.active ? `${rules.search_start}–${rules.search_end} · ${schedule.units} 个搜索单元 · 今日 ${value.runs_today} 次` : schedule.reason,
    tone: value.busy || schedule.active ? "ok" : (rules.enabled ? "warn" : "off"),
  };
  const dealStep = {
    label: "④ 命中推送",
    main: `今日 ${value.deals_today} 件`,
    sub: value.notification_channels.length ? `推送到 ${value.notification_channels.join(" / ")}` : "未配置推送渠道",
    tone: value.notification_channels.length ? "ok" : "warn",
  };
  const badge = document.querySelector("#nav-deal-count");
  badge.hidden = !value.deals_today;
  badge.textContent = value.deals_today;
  badge.title = `今日命中 ${value.deals_today} 件`;
  document.querySelector("#pipeline").innerHTML = [quoteStep, ruleStep, searchStep, dealStep].map(step =>
    `<div class="step ${step.tone}"><span class="step-label">${step.label}</span><strong>${escapeHtml(step.main)}</strong><span class="step-sub">${escapeHtml(step.sub)}</span></div>`
  ).join("");
}

/* ------------------------------------------------------------------ 命中商品 */

const dealView = { status: "", items: [], loadVersion: 0 };

async function loadDeals() {
  const version = ++dealView.loadVersion;
  const [deals, assessments] = await Promise.all([
    api("/api/deals"), api("/api/smart/assessments?status=pending&limit=500"),
  ]);
  if (version !== dealView.loadVersion) return;
  const confirmedIds = new Set(deals.map(deal => String(deal.item_id)));
  const pending = assessments.filter(record => record.snapshot?.item_url && !confirmedIds.has(String(record.item_id))).map(record => {
    const value = record.result, item = record.snapshot;
    const reasons = (value.uncertainties || []).filter(reason => value.grade || reason !== "未取得确定成色的报价，使用候选上限");
    return {
      ...item, item_price: item.price, model: value.model, storage: value.storage, grade: value.grade,
      status: "pending", quote_price: null, quote_upper: value.value_high,
      reference_grade: value.reference_grade,
      assessment_id: record.id, matched_at: record.created_at,
      notification_status: record.notification_status, reason: reasons.join("；") || value.reason,
    };
  });
  dealView.items = [...deals.map(deal => ({ ...deal, status: "matched" })), ...pending]
    .sort((a, b) => (Date.parse(b.matched_at) || 0) - (Date.parse(a.matched_at) || 0));
  renderDeals();
}

function renderDeals() {
  const counts = { "": dealView.items.length, matched: 0, pending: 0 };
  for (const item of dealView.items) counts[item.status] += 1;
  document.querySelectorAll("#deal-filter button").forEach(button => {
    const active = button.dataset.dealStatus === dealView.status;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", String(active));
    button.querySelector("[data-deal-count]").textContent = counts[button.dataset.dealStatus];
  });
  const deals = dealView.items.filter(item => !dealView.status || item.status === dealView.status);
  document.querySelector("#deal-list").innerHTML = deals.length ? deals.map(deal => {
    const pending = deal.status === "pending";
    const push = { sent: ["已推送", ""], failed: ["推送失败", "bad"], disabled: ["未配置推送", "muted"],
      none: ["未推送", "muted"], shadow: ["仅记录", "muted"], duplicate: ["已处理", "muted"],
      sending: ["推送中", "muted"], pending: ["待推送", "muted"] }[deal.notification_status];
    const quote = pending
      ? `${deal.grade || "成色待确认"} · ${deal.quote_upper != null ? `${deal.reference_grade || ""}参考上限 ¥${money(deal.quote_upper)}` : "回收估值待确认"}`
      : `${deal.grade || "参考"}回收估值 ¥${money(deal.quote_price)}`;
    return `<article class="deal ${pending ? "is-pending" : ""}">
      <div class="deal-main">
        <a class="deal-title" href="${escapeHtml(deal.item_url)}" target="_blank" rel="noreferrer">${escapeHtml(deal.title)}</a>
        <div class="deal-meta">
          <span class="tag deal-status ${pending ? "pending" : "confirmed"}">${pending ? "待确认" : "已确认"}</span>
          <span class="tag strong">iPhone ${escapeHtml(deal.model)} · ${escapeHtml(displayStorage(deal.storage))}</span>
          ${deal.region || deal.display_region ? `<span class="tag">${escapeHtml(deal.region || deal.display_region)}</span>` : ""}
          <span>${escapeHtml(quote)}</span>
          <span>${shortTime(deal.matched_at)}</span>
          ${push ? `<span class="push ${push[1]}">${push[0]}</span>` : ""}
        </div>
        ${pending && deal.reason ? `<div class="deal-note">待确认：${escapeHtml(deal.reason)}</div>` : ""}
      </div>
      <div class="deal-price">
        <strong>¥${deal.item_price}</strong>
        <span class="margin ${pending ? "pending" : ""}">${pending ? "估值待确认" : quoteDifferenceLabel(deal.quote_price, deal.item_price)}</span>
      </div>
      ${deal.assessment_id ? `<div class="deal-footer"><button type="button" class="run-item-evidence" data-assessment-id="${deal.assessment_id}">成色与扣费依据 <span aria-hidden="true">›</span></button></div>` : ""}
    </article>`;
  }).join("") : `<div class="empty">${dealView.status === "pending" ? "暂无待确认商品" : dealView.status === "matched" ? "暂无已确认商品" : "暂无已确认或待确认商品"}<br /><span class="hint">搜索发现符合价格条件的商品后会显示在这里。</span></div>`;
}

/* ------------------------------------------------------------------ 规则 */

async function loadDevices() {
  const data = await api("/api/devices");
  state.devices = data.devices;
  renderDevices();
  if (state.rules) updateRuleHints();
}

const deviceIcons = {
  ios: `<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="6.5" y="2.5" width="11" height="19" rx="2.4"/><path d="M10.5 18.5h3"/></svg>`,
};

function selectedDevices() { return state.devices.filter(device => device.selected); }

function deviceSummary() {
  const selected = selectedDevices();
  if (!selected.length) return "未选择";
  if (selected.length === 1) return selected[0].name;
  return `${selected.length} 台 iPhone`;
}

function renderDevices() {
  const selected = selectedDevices();
  document.querySelector("#device-options").innerHTML = state.devices.map(device => {
    const order = selected.indexOf(device);
    const meta = escapeHtml(device.os_version ? `iOS ${device.os_version}` : "iPhone");
    const proxy = device.collection_channel === "proxy";
    const status = device.proxy_status || {};
    const address = status.addresses?.[0] || "电脑局域网 IP";
    const proxyText = status.error || (!device.proxy_client_ip ? "待填写手机 IP" : status.tls_failed ? "证书未通过，请检查完全信任" : status.client_connected ? "已收到手机数据" : status.ready ? "等待手机连接" : "代理准备中…");
    return `<div class="device-card" data-device="${escapeHtml(device.id)}"><div class="device-row ${device.connected ? "" : "offline"}">
      <label class="device-select" aria-label="选择 ${escapeHtml(device.name)}"><input type="checkbox" data-device-selection value="${escapeHtml(device.id)}" ${device.selected ? "checked" : ""} /></label>
      <span class="device-glyph">${deviceIcons.ios}</span>
      <span class="device-info"><b>${escapeHtml(device.name)}</b>
        <small>${meta} · <span class="${device.connected ? "online-text" : "warn-text"}">${escapeHtml(device.connection_status || (device.connected ? "已连接" : "未连接"))}</span></small></span>
      ${selected.length > 1 && order >= 0 ? `<em class="device-order" title="采集顺序">${order + 1}</em>` : ""}
      <select class="device-channel" data-device-channel aria-label="${escapeHtml(device.name)} 采集方式">
        <option value="ui" ${!proxy ? "selected" : ""}>原有采集</option>
        <option value="proxy" ${proxy ? "selected" : ""}>代理采集</option>
      </select>
    </div>${proxy ? `<div class="device-proxy">
      <div class="device-proxy-state"><span class="dot ${status.client_connected ? "ok" : "warn"}"></span><span>${escapeHtml(proxyText)}</span></div>
      <label class="device-proxy-ip">手机 Wi-Fi IP <input data-proxy-ip value="${escapeHtml(device.proxy_client_ip || "")}" placeholder="例如 192.168.1.20" inputmode="text" autocomplete="off" /><button type="button" class="secondary compact" data-save-proxy>保存</button></label>
      <details><summary>手机代理设置</summary><div class="device-proxy-help">
        <p>手机与电脑连接同一局域网，在手机 Wi-Fi 详情中将 HTTP 代理设为手动。</p>
        <div class="device-proxy-address"><span>服务器 <b>${escapeHtml(address)}</b></span><span>端口 <b>${escapeHtml(String(status.port || 8899))}</b></span></div>
        <p>手机 Safari 打开 <b>http://mitm.it</b> 安装证书，再到「通用 → 关于本机 → 证书信任设置」开启完全信任。</p>
        <p>上方 IP 填手机 Wi-Fi 详情中的 IP 地址。切换通道从下一次搜索生效。</p>
      </div></details>
    </div>` : device.proxy_client_ip ? `<p class="device-channel-note">使用原有采集时，请在手机关闭 Wi-Fi 代理。</p>` : ""}</div>`;
  }).join("") || `<p class="hint">没有检测到 iPhone；Mac 采集已禁用。</p>`;
  document.querySelector("#device-summary").textContent = deviceSummary();
  const dot = document.querySelector("#device-dot");
  const ready = selected.every(device => device.connected && (device.collection_channel !== "proxy" || device.proxy_status?.client_connected));
  dot.className = `dot ${!selected.length ? "bad" : ready ? "ok" : "warn"}`;
  dot.title = !selected.length ? "未选择设备" : ready ? "所选设备已就绪" : selected.some(device => !device.connected) ? "有设备未连接" : "代理等待手机连接或配置";
}

function setDeviceMenu(open) {
  document.querySelector("#device-menu").hidden = !open;
  document.querySelector("#device-picker-btn").setAttribute("aria-expanded", String(open));
  document.querySelector("#device-picker").classList.toggle("open", open);
}

/* ------------------------------------------------------------------ 规则表单 */

const ruleFields = ["region_city", "region_path", "quote_category", "quote_grade", "max_over_quote",
  "min_price_ratio", "exclude_keywords", "scroll_pages",
  "search_start", "search_end", "model_interval_minutes"];
const numberFields = new Set(["max_over_quote", "min_price_ratio", "model_interval_minutes", "scroll_pages"]);
const rulesForm = document.querySelector("#rules-form");
const field = name => rulesForm.elements[name];

function formValues() {
  const values = {};
  ruleFields.forEach(name => {
    const value = field(name).value;
    values[name] = numberFields.has(name) ? Number(value) : String(value).trim();
  });
  values.exclude_keywords = splitKeywords(values.exclude_keywords).join(",");
  return values;
}

function isDirty() {
  if (!state.rules) return false;
  const values = formValues();
  return ruleFields.some(name => {
    const saved = name === "exclude_keywords" ? splitKeywords(state.rules[name]).join(",") : state.rules[name];
    return numberFields.has(name) ? Number(saved) !== values[name] : String(saved ?? "").trim() !== values[name];
  }) || smartUI.isDirty();
}

function refreshDirty() {
  const dirty = isDirty();
  rulesForm.classList.toggle("dirty", dirty);
  document.querySelector("#rules-save").disabled = !dirty;
  document.querySelector("#rules-reset").disabled = !dirty;
  document.querySelector("#save-state").textContent = dirty ? "有未保存的修改" : "已保存";
}

function fillRules(rules) {
  ruleFields.forEach(name => { field(name).value = rules[name] ?? ""; });
  renderTags();
  setPower(rules.enabled);
  refreshDirty();
  updateRuleHints();
}

async function loadRules() {
  state.rules = await api("/api/rules");
  fillRules(state.rules);
}

function rulesPayload(overrides = {}) {
  const payload = { ...state.rules, ...overrides };
  ["region_steps", "storage_steps_example", "pass_list"].forEach(key => delete payload[key]);
  return payload;
}

function setPower(enabled) {
  document.querySelector("#rules-enabled").checked = enabled;
  document.querySelector("#power-label").textContent = enabled ? "监控中" : "已暂停";
  document.querySelector("#power-switch").classList.toggle("on", enabled);
}

async function saveRules(event) {
  event.preventDefault();
  const invalid = [...rulesForm.elements].find(element => element.willValidate && !element.checkValidity());
  if (invalid) {
    setSettingsTab(invalid.closest(".settings-pane")?.dataset.pane);
    invalid.reportValidity();
    return;
  }
  const button = document.querySelector("#rules-save");
  button.disabled = true;
  try {
    await smartUI.saveConfig();
    state.rules = await api("/api/rules", { method: "PUT", body: JSON.stringify(rulesPayload(formValues())) });
    fillRules(state.rules);
    toast("规则已保存，下一次搜索生效");
    await Promise.all([loadTargets(), loadOverview()]);
  } catch (error) { toast(error.message, true); refreshDirty(); }
}

/* 设置分页：地区 / 判价 / 搜索 */
function setSettingsTab(name) {
  if (!name) return;
  document.querySelectorAll("#settings-tabs button").forEach(button => {
    const active = button.dataset.tab === name;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(".settings-pane").forEach(pane => pane.classList.toggle("active", pane.dataset.pane === name));
  try { localStorage.setItem("settingsTab", name); } catch (_) {}
}

/** 一句话规则里点某个高亮词：切到对应分页并聚焦该字段。 */
function focusRule(name) {
  if (name === "devices") { setDeviceMenu(true); document.querySelector("#device-options input")?.focus(); return; }
  if (name === "targets") {
    const card = document.querySelector(".target-card");
    card.scrollIntoView({ behavior: "smooth", block: "start" });
    document.querySelector("#target-filter").focus({ preventScroll: true });
    return;
  }
  const control = field(name);
  const element = control instanceof RadioNodeList ? [...control].find(input => input.checked) || control[0] : control;
  if (name === "region_path") document.querySelector("#region-advanced").open = true;
  setSettingsTab(element.closest(".settings-pane")?.dataset.pane);
  const card = document.querySelector(".settings-card");
  if (card.getBoundingClientRect().top < 0 || card.getBoundingClientRect().top > innerHeight - 200) card.scrollIntoView({ behavior: "smooth", block: "start" });
  element.focus({ preventScroll: true });
  const target = element.closest(".field, label") || element;
  target.classList.remove("flash"); void target.offsetWidth; target.classList.add("flash");
}

/* 排除词标签 */
function splitKeywords(value) {
  return [...new Set(String(value || "").split(/[,，、\n]+/).map(word => word.trim()).filter(Boolean))];
}
function renderTags() {
  const words = splitKeywords(field("exclude_keywords").value);
  document.querySelector("#exclude-tags").innerHTML = words.map(word =>
    `<span class="kw">${escapeHtml(word)}<button type="button" data-word="${escapeHtml(word)}" aria-label="删除 ${escapeHtml(word)}">×</button></span>`).join("");
}
function setKeywords(words) {
  field("exclude_keywords").value = [...new Set(words)].join(",");
  renderTags();
  onRulesEdited();
}
function addKeywordsFromInput() {
  const input = document.querySelector("#exclude-input");
  const added = splitKeywords(input.value);
  input.value = "";
  if (added.length) setKeywords([...splitKeywords(field("exclude_keywords").value), ...added]);
}

function onRulesEdited() {
  refreshDirty();
  clearTimeout(onRulesEdited.t);
  onRulesEdited.t = setTimeout(updateRuleHints, 200);
}

/* 规则说明、一句话规则、耗时估算 */
function targetCounts() {
  const active = state.targets.filter(target => target.enabled && target.units.some(unit => unit.enabled));
  return {
    total: state.targets.length,
    models: active.length,
    capacities: active.reduce((sum, target) => sum + target.units.filter(unit => unit.enabled).length, 0),
    allCapacities: state.targets.reduce((sum, target) => sum + target.units.length, 0),
  };
}

async function updateRuleHints() {
  if (!state.rules) return;
  const values = formValues();
  const city = values.region_city;
  let path = values.region_path;
  if (city && !path) {
    try { path = (await api(`/api/rules/region-path?city=${encodeURIComponent(city)}`)).path; } catch (_) { path = ""; }
  }
  field("region_path").placeholder = path ? `留空自动生成：${path}` : "留空自动生成，例如 区域 > 福建 > 厦门 > 查看";
  const devices = selectedDevices();
  const usesPhone = devices.some(device => device.kind !== "mac");
  document.querySelector("#region-hint").innerHTML = city
    ? [usesPhone ? `iPhone 端优先使用首页「${escapeHtml(city)}」Tab 搜索。` : "",
       `卡片地区明确不是${escapeHtml(city)}的商品不推送。`].join("")
    : "未设置城市：搜索全国，不核对地区。";

  const smartEnabled = Boolean(state.smart?.enabled);
  field("quote_grade").length && [...field("quote_grade")].forEach(input => { input.disabled = smartEnabled; });
  const gradeGroup = rulesForm.querySelector('[name="quote_grade"]')?.closest(".field");
  if (gradeGroup) gradeGroup.hidden = smartEnabled;
  // 判价公式示例
  const over = Math.max(0, values.max_over_quote || 0);
  const ratio = values.min_price_ratio || 0;
  const sample = 4900;
  const sourceName = values.quote_category === "100027" ? "有保" : "无保";
  document.querySelector("#price-formula").innerHTML =
    `<div class="formula-main">推送：<b>${escapeHtml(ratio ? `¥${money(Math.round(sample * ratio))}` : "¥0")}</b> ≤ 挂牌价 ≤ <b>${sourceName}${escapeHtml(values.quote_grade)}价 + ¥${money(over)}</b></div>
     <div class="hint">例：回收报价 ¥${money(sample)} → 挂牌价 ¥${money(sample + over)} 以内推送${ratio ? `，低于 ¥${money(Math.round(sample * ratio))} 视为引流或非整机` : ""}。</div>`;

  if (smartEnabled) document.querySelector("#price-formula").innerHTML = `<div class="formula-main">关键词判成色 → 扣费后回收价 + ¥${money(over)}</div><div class="hint">候选用允许成色的最高价初筛，详情成色不明按小花报价参考；未说明项按正常处理，矛盾信息列为待确认。低价比例仅标记风险。${state.smart.shadow ? '验证模式：只记录，不发通知。' : ''}</div>`;

  // 搜索方式与耗时估算：每次搜索 = 打开搜索 + 区域筛选 + 逐个容量筛选并读「新发」
  const scrolls = Math.max(0, values.scroll_pages || 0);
  document.querySelector("#storage-hint").innerHTML =
    `每款机型打开一次搜索页，依次勾选各容量并点「确定」，只看「新发」` +
    (scrolls ? `，每个容量向下翻 ${scrolls} 屏读完第一页。` : `，只读当前一屏。`);
  const { models, capacities, total, allCapacities } = targetCounts();
  const interval = values.model_interval_minutes || 90;
  const round = models ? Math.ceil((models * (4 + (city ? 3 : 0)) + capacities * (6 + 4 * scrolls + 8)) / 60) : 0;
  document.querySelector("#cadence-hint").innerHTML = models
    ? `<div><span>每轮约</span><strong>${round}<small> 分钟</small></strong></div>
       <div><span>同一机型</span><strong>${Math.max(round, interval)}<small> 分钟一次</small></strong></div>
       ${round > interval ? `<p class="warn-text">间隔小于一轮耗时，实际按一轮的耗时循环。</p>` : ""}`
    : `<p class="hint">还没有启用的机型。</p>`;

  document.querySelector("#target-count").textContent = total ? `${models}/${total} 款 · ${capacities}/${allCapacities} 个容量` : "";
  renderRuleSentence(values, { models, capacities, interval, round, path });
}

function renderRuleSentence(values, { models, capacities, interval, round }) {
  const enabled = state.rules.enabled;
  const token = (go, text, extra = "") => `<button type="button" class="tok ${extra}" data-go="${go}">${escapeHtml(text)}</button>`;
  const sourceName = values.quote_category === "100027" ? "有保" : "无保";
  const words = splitKeywords(values.exclude_keywords).length;
  const dirty = isDirty();
  document.querySelector("#rule-sentence").innerHTML = `
    <div class="rs-badge ${enabled ? "on" : "off"}"><i></i>${enabled ? "监控中" : "已暂停"}</div>
    <p class="rs-text">
      用 ${token("devices", deviceSummary(), selectedDevices().length ? "" : "bad")}
      在 ${token("region_city", values.region_city || "全国")}
      搜索 ${token("targets", `${models} 款机型 · ${capacities} 个容量`, models ? "" : "bad")}，
      挂牌价不超过 ${token("quote_grade", `${sourceName}${state.smart?.enabled ? "允许成色最高" : values.quote_grade}回收价`)} ${token("max_over_quote", `+ ¥${money(values.max_over_quote || 0)}`)}
      ${state.smart?.enabled ? "进入详情按关键词判成色与扣费；" : `就推送（排除 ${token("exclude_keywords", `${words} 个关键词`)}）；`}
      ${token("search_start", `${values.search_start || "--:--"}–${values.search_end || "--:--"}`)}
      内每款机型约 ${token("model_interval_minutes", `${Math.max(interval, round || 0)} 分钟`)}搜一次。
    </p>
    ${dirty ? `<span class="rs-dirty">预览中 · 未保存</span>` : ""}`;
}

/* ------------------------------------------------------------------ 机型 */

function runSummary(run) {
  if (!run) return `<span class="muted">尚未搜索</span>`;
  if (run.ok === 0) return `<span class="bad" title="${escapeHtml(run.error)}">失败 · ${ago(run.started_at)}</span>`;
  if (run.ok == null) return `<span>搜索中…</span>`;
  const where = run.storage ? `${displayStorage(run.storage)} · ` : "";
  return `${ago(run.started_at)} <span class="muted">· ${where}共 ${run.observed} 件 ·</span> ` +
    `<span class="${run.matched ? "hit-text" : "muted"}">命中 ${run.matched} 件</span>` +
    (run.error ? ` <span class="warn-text" title="${escapeHtml(run.error)}">· 有警告</span>` : "");
}

function unitChip(target, unit) {
  const run = unit.last_run;
  const status = !unit.enabled ? "off" : !run ? "never" : run.ok === 0 ? "fail" : run.matched ? "hit" : run.error ? "warn" : "ok";
  const detail = !run ? "尚未搜索" : run.ok === 0 ? `失败：${run.error}` :
    `${ago(run.started_at)}：共 ${run.observed} 件，命中 ${run.matched} 件` +
    (run.passes?.length ? `（${run.passes.map(p => `${p.label} ${p.error ? "失败" : p.observed}`).join("，")}）` : "") +
    (run.error ? `\n${run.error}` : "");
  return `<button type="button" class="unit-chip ${status}" data-model="${escapeHtml(target.model)}" data-storage="${escapeHtml(unit.storage)}"
    data-enabled="${unit.enabled ? 1 : 0}"
    title="${escapeHtml(detail)}\n点击${unit.enabled ? "停用" : "启用"}这个容量">${displayStorage(unit.storage)}${run && run.ok ? `<small>${run.observed}${run.matched ? `·${run.matched}` : ""}</small>` : ""}</button>`;
}

function selectionState(units) {
  const selected = units.filter(unit => unit.enabled).length;
  return { all: units.length > 0 && selected === units.length, partial: selected > 0 && selected < units.length, any: selected > 0 };
}

/** 机型表里展开的系列；记在浏览器本地，刷新后保持。 */
const openSeries = (() => {
  try { return new Set(JSON.parse(localStorage.getItem("openSeries") || "[]").map(String)); } catch (_) { return new Set(); }
})();
function saveOpenSeries() {
  try { localStorage.setItem("openSeries", JSON.stringify([...openSeries])); } catch (_) {}
}

function seriesSummary(members) {
  const units = members.flatMap(item => item.units);
  const models = members.filter(item => item.units.some(unit => unit.enabled)).length;
  const enabledUnits = units.filter(unit => unit.enabled).length;
  const deals = members.reduce((sum, item) => sum + (item.deals || 0), 0);
  const failed = members.filter(item => item.last_run?.ok === 0).length;
  const pct = units.length ? Math.round(enabledUnits / units.length * 100) : 0;
  return `<span class="meter" title="${enabledUnits}/${units.length} 个容量在监控"><i style="width:${pct}%"></i></span>
    <span>${models}/${members.length} 款 · ${enabledUnits}/${units.length} 容量</span>` +
    (deals ? `<span class="pill-tag hit">命中 ${deals}</span>` : "") +
    (failed ? `<span class="pill-tag fail">失败 ${failed}</span>` : "");
}

function setSeriesOpen(series, open) {
  open ? openSeries.add(series) : openSeries.delete(series);
  saveOpenSeries();
  const head = document.querySelector(`#target-rows .series-row[data-series="${series}"]`);
  head?.classList.toggle("open", open);
  head?.querySelector(".series-expand")?.setAttribute("aria-expanded", String(open));
  if (!targetQuery()) document.querySelectorAll(`#target-rows tr.model-row[data-series="${series}"]`).forEach(row => { row.hidden = !open; });
  updateExpandAllLabel();
}

function updateExpandAllLabel() {
  const all = [...new Set(state.targets.map(item => String(item.series)))];
  const button = document.querySelector("#expand-all-series");
  if (button) button.textContent = all.length && all.every(series => openSeries.has(series)) ? "全部收起" : "全部展开";
}

function targetQuery() {
  return document.querySelector("#target-filter").value.trim().toLowerCase().replace(/^iphone\s*/, "");
}

/** 机型筛选：有关键字时直接展开匹配的机型，隐藏没有匹配项的系列。 */
function applyTargetFilter() {
  const query = targetQuery();
  const matchedSeries = new Set();
  document.querySelectorAll("#target-rows tr.model-row").forEach(row => {
    const match = !query || row.dataset.model.toLowerCase().includes(query);
    if (query && match) matchedSeries.add(row.dataset.series);
    row.hidden = query ? !match : !openSeries.has(row.dataset.series);
  });
  document.querySelectorAll("#target-rows tr.series-row").forEach(row => {
    row.hidden = Boolean(query) && !matchedSeries.has(row.dataset.series);
    row.classList.toggle("open", query ? matchedSeries.has(row.dataset.series) : openSeries.has(row.dataset.series));
  });
  const empty = document.querySelector("#target-rows .filter-empty");
  if (empty) empty.hidden = !query || matchedSeries.size > 0;
}

async function loadTargets() {
  const targets = await api("/api/targets");
  state.targets = targets;
  let lastSeries = null;
  const rows = [];
  for (const target of targets) {
    if (target.series !== lastSeries) {
      const members = targets.filter(item => item.series === target.series);
      const seriesSelection = selectionState(members.flatMap(item => item.units));
      const open = openSeries.has(String(target.series));
      rows.push(`<tr class="series-row ${open ? "open" : ""}" data-series="${target.series}"><td colspan="3"><div class="series-line">
        <button type="button" class="series-expand" data-series="${target.series}" aria-expanded="${open}" aria-label="展开或收起">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6l6 6-6 6"/></svg></button>
        <label class="switch small series-switch">
          <input type="checkbox" class="series-toggle" data-series="${target.series}" data-partial="${seriesSelection.partial ? 1 : 0}" ${seriesSelection.all ? "checked" : ""} />
          <b>${target.series ? `iPhone ${target.series}` : "更早机型"}</b></label>
        <span class="series-summary">${seriesSummary(members)}</span>
      </div></td></tr>`);
      lastSeries = target.series;
    }
    const modelSelection = selectionState(target.units);
    rows.push(`<tr data-enabled="${modelSelection.any ? 1 : 0}" data-series="${target.series}" data-model="${escapeHtml(target.model)}" class="model-row ${modelSelection.any ? "" : "off"}" ${openSeries.has(String(target.series)) ? "" : "hidden"}>
      <td class="m-name">
        <label class="switch small"><input type="checkbox" class="target-toggle" data-model="${escapeHtml(target.model)}" data-partial="${modelSelection.partial ? 1 : 0}" ${modelSelection.all ? "checked" : ""} /> <b>${escapeHtml(target.model)}</b></label>
        <div class="m-last">${runSummary(target.last_run)}</div>
      </td>
      <td class="m-units"><div class="unit-chips">${target.units.map(unit => unitChip(target, unit)).join("")}</div></td>
      <td class="m-actions">
        ${target.deals ? `<span class="deal-count" title="累计命中 ${target.deals} 件">${target.deals}</span>` : ""}
        <button type="button" class="compact secondary run-target" data-model="${escapeHtml(target.model)}" ${modelSelection.any ? "" : "disabled"} title="立即搜索这款机型的所有已启用容量">
          <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="11" cy="11" r="6"/><path d="M16 16l4 4"/></svg>搜索</button>
      </td>
    </tr>`);
  }
  if (rows.length) rows.push(`<tr class="filter-empty" hidden><td colspan="3" class="empty">没有匹配的机型。</td></tr>`);
  document.querySelector("#target-rows").innerHTML = rows.join("") ||
    `<tr><td colspan="3" class="empty">报价表里还没有机型，请先在「每日报价」同步。</td></tr>`;
  document.querySelectorAll("#target-rows .series-toggle, #target-rows .target-toggle").forEach(input => {
    input.indeterminate = input.dataset.partial === "1";
  });
  applyTargetFilter();
  updateExpandAllLabel();
  updateRuleHints();
}

/* ------------------------------------------------------------------ 运行记录 */

/** 操作日志：每轮（容量）的点击步骤、读到的卡片 / 曝光数和商品，用于排查。 */
function passDetail(run) {
  const lines = [];
  for (const p of run.passes || []) {
    lines.push(p.error
      ? `${passName(p)}：${p.error}`
      : `${passName(p)}：关联 ${p.observed} 件` + (p.cards != null
        ? `（页面卡片 ${p.cards}${p.candidates != null ? `，候选 ${p.candidates}` : `，曝光记录 ${p.exposures}`}）` : ""));
    for (const action of p.actions || []) {
      const target = action.target || action.steps?.join(" → ") || "";
      lines.push(`操作${action.success ? "" : "失败"}：${action.action}${target ? `「${target}」` : ""}` +
        (action.method ? `（${action.method}）` : "") +
        (action.text_method ? `；输入：${action.text_method}` : "") + (action.error ? `：${action.error}` : ""));
      if (action.clicked?.length) lines.push(`实际点击：${action.clicked.join(" → ")}`);
      for (const method of action.methods || []) lines.push(`  ${method.target}：${method.method}`);
      if (action.skipped?.length) lines.push(`跳过可选项：${action.skipped.join("、")}`);
    }
    for (const item of p.items || []) {
      lines.push(`商品：¥${item.price} ${item.title}${item.display_region ? ` · ${item.display_region}` : ""} · ID ${item.item_id}`);
    }
    for (const item of p.card_samples || []) {
      lines.push(`列表商品：¥${item.price} ${item.title}${item.display_region ? ` · ${item.display_region}` : ""}`);
    }
    if (p.cards_truncated) lines.push(`其余列表商品未展开（本轮共 ${p.cards} 件）`);
    if (p.items_truncated) lines.push(`其余商品未展开（本轮共 ${p.items_total} 件）`);
  }
  if (run.error) lines.push(run.error);
  return lines.join("\n");
}

const runView = {
  status: "", search: "", model: null, groups: [], runs: [], selected: null, storage: "", result: "", code: "",
  loadVersion: 0, detailVersion: 0,
  follow: true,          // 选中的是最新一条时，新搜索开始后自动切过去
  running: false,        // 列表里有正在进行的搜索
  topId: null, listSignature: "", detailSignature: "",
  seen: new Map(),       // 每条记录已显示过的商品，用来高亮新出现的
};
const triggerLabels = { schedule: "定时", manual: "手动", inject: "调试" };
/** 每一轮的名字：按容量筛选时显示容量（「256G」），否则显示按钮名（「新发」）。 */
function passName(p) {
  const name = p.storage ? displayStorage(p.storage) : p.label;
  return p.device_name ? `${p.device_name} · ${name}` : name;
}


function unitName(run) {
  return `${run.model}${run.storage ? ` ${displayStorage(run.storage)}` : ""}`;
}

function runTone(run) {
  if (run.ok == null) return "running";
  if (run.ok === 0) return "fail";
  if (run.hit_count) return "hit";
  if (run.error) return "warn";
  return "ok";
}

function duration(run) {
  if (!run.finished_at && run.ok != null) return "";
  const end = run.finished_at ? new Date(run.finished_at) : new Date();
  const seconds = Math.max(0, Math.round((end - new Date(run.started_at)) / 1000));
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

function renderRunList() {
  const search = runView.search.trim().toLowerCase();
  const groups = runView.groups.filter(group => `iphone ${group.model}`.toLowerCase().includes(search));
  document.querySelector("#run-model-count").textContent = `${groups.length} 个机型`;
  document.querySelector("#run-list").innerHTML = groups.length ? groups.map(group => {
    const recent = {
      running: { tone: "running", pill: "live", label: "正在搜索" },
      completed: { tone: "ok", pill: "", label: "最近完成" },
      failed: { tone: "fail", pill: "bad", label: "最近失败" },
      warn: { tone: "warn", pill: "warn", label: "最近有警告" },
    }[group.latest_status];
    const tone = recent?.tone || "ok";
    return `<button type="button" class="run-row ${tone} ${group.model === runView.model ? "active" : ""}" data-model="${escapeHtml(group.model)}" aria-pressed="${group.model === runView.model}">
      <span class="run-row-top"><b>iPhone ${escapeHtml(group.model)}</b><span class="run-row-arrow" aria-hidden="true">›</span></span>
      <span class="run-row-meta"><span>累计 ${group.run_count} 次搜索</span><span class="run-time" title="最近一轮开始时间">${shortTime(group.started_at)}</span></span>
      <span class="run-row-meta">
        <span class="run-total-hits ${group.hit_count ? "has-hits" : ""}">累计命中 ${group.hit_count || 0}</span>
        ${recent ? `<span class="pill ${recent.pill}">${tone === "running" ? "<i></i>" : ""}${recent.label}</span>` : ""}
      </span>
    </button>`;
  }).join("") : `<div class="empty">${search ? "没有找到该机型，试试其他名称。" : runView.status ? "没有符合条件的机型。" : "还没有搜索记录。"}</div>`;
}

/** quiet=true 用于自动刷新：内容没变就不重绘，避免闪烁。 */
async function loadRuns({ quiet = false } = {}) {
  const version = ++runView.loadVersion;
  const query = runView.status ? `?status=${runView.status}` : "";
  const groups = await api(`/api/run-groups${query}`);
  if (version !== runView.loadVersion) return;
  runView.groups = groups;
  if (!groups.some(group => group.model === runView.model)) {
    runView.model = groups[0]?.model ?? null;
    runView.selected = null;
    runView.storage = runView.result = runView.code = "";
    runView.follow = true;
  }
  runView.running = groups.some(group => group.running_count);
  const model = runView.model;
  const runs = model == null ? [] : await api(`/api/runs?limit=1000&model=${encodeURIComponent(model)}`);
  if (version !== runView.loadVersion || model !== runView.model) return;
  runView.runs = runs;
  const top = runs[0]?.id ?? null;
  if (runView.follow || !runs.some(run => run.id === runView.selected)) runView.selected = top;
  runView.topId = top;
  const signature = JSON.stringify([runView.model, groups]);
  if (!quiet || signature !== runView.listSignature) {
    runView.listSignature = signature;
    renderRunList();
  }
  if (runView.selected) await loadRunDetail(runView.selected, { quiet });
  else document.querySelector("#run-detail").innerHTML = `<div class="empty">选择左侧的机型查看运行记录。</div>`;
}

function stepChip(ok, label, detail) {
  const icon = ok === true ? "✓" : ok === false ? "✕" : "–";
  return `<span class="step-chip ${ok === true ? "ok" : ok === false ? "bad" : ""}" title="${escapeHtml(detail || "")}">${icon} ${escapeHtml(label)}</span>`;
}

function runItemQuote(item, outcome = runOutcome(item)) {
  if (outcome.basis !== "detail" && outcome.low == null && outcome.high == null && outcome.limit == null) return "";
  const amount = number => `¥${money(number)}`;
  const notes = [];
  let reference;
  if (outcome.basis === "candidate") {
    reference = outcome.high == null ? null : `列表筛选参考 <strong>${amount(outcome.high)}</strong>`;
    if (outcome.grade) notes.push(`按最高允许档位（${escapeHtml(outcome.grade)}）筛选`);
    notes.push(outcome.bucket === "rejected" ? "未进入详情判价" : "尚未完成详情判价");
  } else {
    const range = outcome.low != null ? (outcome.high != null && outcome.high !== outcome.low
      ? `${amount(outcome.low)}–${amount(outcome.high)}` : amount(outcome.low))
      : outcome.high != null ? `≤ ${amount(outcome.high)}` : "暂无可用估值";
    reference = `回收参考 <strong>${range}</strong>`;
    notes.push(outcome.basis === "fixed" ? (outcome.grade ? `按${escapeHtml(outcome.grade)}档参考` : "固定档位参考")
      : outcome.grade ? `成色：${escapeHtml(outcome.grade)}` : "成色待确认");
    if (outcome.referenceGrade) notes.push(`按${escapeHtml(outcome.referenceGrade)}参考`);
    if (outcome.code === "unpriced_condition") notes.push("当时因缺少扣费未计价（旧策略）");
    else if (outcome.deductions) {
      const low = outcome.deductions.reduce((sum, row) => sum + Number(row.min || 0), 0);
      const high = outcome.deductions.reduce((sum, row) => sum + Number(row.max ?? row.min ?? 0), 0);
      notes.push(!outcome.deductions.length ? (outcome.unpriced ? "无对应扣费，按0计算" : "未扣费") : outcome.deductions.some(row => row.open_ended)
        ? `至少扣 ${amount(low)}` : low === high ? `已扣 ${amount(low)}`
          : outcome.low == null ? `按最低扣费 ${amount(low)}参考` : `扣费 ${amount(low)}–${amount(high)}`);
      if (outcome.unpriced && outcome.deductions.length) notes.push("其他无对应扣费项按0计算");
    }
  }
  const limit = outcome.limit != null ? `${outcome.basis === "candidate" ? "初筛" : "判价"}上限 <strong>${amount(outcome.limit)}</strong>` : null;
  if (!reference && !limit) return "";
  return `<div class="run-item-quote-line">${[reference, limit].filter(Boolean).map(text => `<span>${text}</span>`).join("")}</div>
    ${notes.length ? `<div class="run-item-quote-note">${notes.join(" · ")}</div>` : ""}`;
}

function runItem(item, fresh = false) {
  const outcome = runOutcome(item);
  const { code } = outcome;
  const reason = outcome.excess != null ? `挂牌价超出${outcome.basis === "candidate" ? "初筛" : "判价"}上限 ¥${money(outcome.excess)}` : outcome.reason;
  const isHit = outcome.bucket === "matched";
  const badge = isHit
    ? `<span class="item-badge hit">命中 · ${quoteDifferenceLabel(item.quote_price, item.price)}</span>${item.new_deal ? `<span class="item-badge">新命中</span>` : ""}`
    : `<span class="item-badge ${code} ${outcome.bucket}">${codeLabels[code] || code}</span>`;
  const notificationLabels = { sent: "已推送", failed: "推送失败", shadow: "仅记录", duplicate: "已处理", sending: "推送中" };
  const notification = notificationLabels[item.assessment_notification];
  const quote = runItemQuote(item, outcome);
  const priceDetails = outcome.base != null && outcome.deductions ? `<details class="run-item-evidence-preview">
    <summary>报价与扣费明细</summary><div class="run-item-price-details">
      <div>基础报价${outcome.referenceGrade ? `（${escapeHtml(outcome.referenceGrade)}参考）` : ""}：¥${money(outcome.base)}</div>
      ${outcome.deductions.length ? outcome.deductions.map(row => `<div>${escapeHtml(row.name)}：扣 ¥${money(row.min)}${row.max !== row.min ? `–${money(row.max)}` : ""}${row.open_ended ? "起" : ""}</div>`).join("") : outcome.code === "unpriced_condition" ? "<div>旧策略因缺少扣费未计价，保留当时结论</div>" : "<div>无适用扣费</div>"}
      ${outcome.unpriced && outcome.code !== "unpriced_condition" ? "<div>没有对应扣费金额的项目按0计算，已匹配项目照常扣除</div>" : ""}
    </div></details>` : "";
  return `<article class="run-item ${isHit ? "hit" : ""} ${fresh ? "fresh" : ""}">
    <div class="run-item-main">
      ${item.item_url
        ? `<a class="run-item-title" href="${escapeHtml(item.item_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a>`
        : `<span class="run-item-title" title="本次记录未取得商品链接">${escapeHtml(item.title)}</span>`}
      <div class="run-item-meta">
        ${badge}
        ${outcome.stage ? `<span class="run-item-stage">${escapeHtml(outcome.stage)}</span>` : ""}
        ${notification ? `<span class="item-badge ${item.assessment_notification === "failed" ? "notification-failed" : "muted"}">${notification}</span>` : ""}
        ${item.model ? `<span>${escapeHtml(item.model)}${item.storage ? ` ${escapeHtml(displayStorage(item.storage))}` : ""}</span>` : ""}
        ${item.display_region ? `<span>${escapeHtml(item.display_region)}</span>` : ""}
        ${item.pass_label && normalizeCapacity(item.pass_label) !== normalizeCapacity(item.storage) ? `<span class="pass-tag">采集 ${escapeHtml(item.pass_label)}</span>` : ""}
      </div>
      ${quote ? `<div class="run-item-reason">${quote}</div>` : ""}
      ${!isHit && reason ? `<div class="run-item-reason ${outcome.excess != null ? "price-excess" : ""}">${escapeHtml(reason)}</div>` : ""}
    </div>
    <div class="run-item-price">¥${item.price}</div>
    ${item.assessment_id ? `<div class="run-item-footer">
      <button type="button" class="run-item-evidence" data-assessment-id="${item.assessment_id}">
        <span>成色与扣费依据</span>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="m9 6 6 6-6 6" /></svg>
      </button>
    </div>` : priceDetails ? `<div class="run-item-footer">${priceDetails}</div>` : ""}
  </article>`;
}

async function loadRunDetail(id, { quiet = false } = {}) {
  const version = ++runView.detailVersion;
  const run = await api(`/api/runs/${id}`);
  if (version !== runView.detailVersion || id !== runView.selected || run.model !== runView.model) return;
  // 自动刷新不打断正在浏览历史或选择原因的操作。
  if (quiet && (document.activeElement?.matches("#run-detail select") || document.querySelector(".run-history-menu[open], .run-item-evidence-preview[open]"))) return;
  const group = runView.groups.find(item => item.model === run.model);
  const signature = JSON.stringify([run, group, runView.storage, runView.result, runView.code,
    runView.follow, runView.runs.map(item => [item.id, item.ok, item.hit_count])]);
  const tone = runTone(run);
  if (quiet && signature === runView.detailSignature) {
    const elapsed = document.querySelector("#run-elapsed");
    if (elapsed && tone === "running") elapsed.textContent = duration(run).replace(/\s/g, "");
    return;
  }
  runView.detailSignature = signature;
  const seen = runView.seen.get(run.id);
  const fresh = new Set(seen ? run.items.filter(item => !seen.has(item.item_id)).map(item => item.item_id) : []);
  runView.seen.set(run.id, new Set(run.items.map(item => item.item_id)));
  const newHit = run.items.find(item => item.matched && fresh.has(item.item_id));
  if (newHit) toast(`命中 iPhone ${newHit.model} ${displayStorage(newHit.storage)} ¥${newHit.price}${newHit.new_deal ? " · 新命中" : ""}`);
  const logOpen = document.querySelector("#run-detail .run-log")?.open || false;
  const issueOpen = document.querySelector("#run-detail .run-issue")?.open || false;
  const { items, counts, reasons, capacities } = filterRunItems(run, runView);
  const storages = capacityOptions(runView.runs, run);
  if (runView.storage && runView.storage !== "unknown" && !storages.includes(runView.storage)) storages.push(runView.storage);
  const scope = runView.storage === "unknown" ? "容量未识别" : runView.storage ? displayStorage(runView.storage) : "全部容量";
  const hasFilter = runView.storage || runView.result || runView.code;
  const storageChip = (value, label, count) => `<button type="button" class="chip ${runView.storage === value ? "active" : ""}" data-run-storage="${value}" aria-pressed="${runView.storage === value}">${label}<small>${count}</small></button>`;
  const storageFilters = [storageChip("", "全部容量", run.items.length),
    ...storages.map(storage => storageChip(storage, displayStorage(storage), capacities[storage] || 0)),
    ...(capacities.unknown || runView.storage === "unknown" ? [storageChip("unknown", "未识别容量", capacities.unknown || 0)] : [])].join("");
  const resultFilters = [["", "全部", counts.all], ["matched", "命中", counts.matched],
    ["pending", "待确认", counts.pending], ["rejected", "不符合", counts.rejected], ["incomplete", "未完成", counts.incomplete]].map(([value, label, count]) =>
    `<button type="button" class="run-result-stat ${value || "all"} ${runView.result === value ? "active" : ""}" data-run-result="${value}" aria-pressed="${runView.result === value}"><span>${label}</span><strong>${count}</strong></button>`).join("");
  const reasonCodes = [...codeOrder.filter(code => reasons[code]), ...Object.keys(reasons).filter(code => !codeOrder.includes(code))];
  if (runView.code && !reasonCodes.includes(runView.code)) reasonCodes.push(runView.code);
  const reasonFilter = ["rejected", "incomplete"].includes(runView.result) ? `<label class="run-reason-filter" for="run-reason-select">${runView.result === "incomplete" ? "未完成步骤" : "不符合原因"}<select id="run-reason-select"><option value="">全部原因（${counts[runView.result]}）</option>${reasonCodes.map(code => `<option value="${escapeHtml(code)}" ${code === runView.code ? "selected" : ""}>${escapeHtml(codeLabels[code] || code)}（${reasons[code] || 0}）</option>`).join("")}</select></label>` : "";
  const steps = [];
  if (tone === "running" && run.region) steps.push(stepChip(null, `区域 ${run.region}`, "搜索结束后显示是否已在 App 内切换"));
  else if (run.ok !== 0 && run.region) steps.push(stepChip(Boolean(run.region_applied), `区域 ${run.region}`, run.region_applied ? "已在 App 内切换" : "未切换，只核对卡片地区"));
  if (run.ok !== 0 && run.storage) steps.push(stepChip(Boolean(run.filter_applied), `容量 ${displayStorage(run.storage)}`, run.filter_applied ? "已在 App 内筛选" : "未筛选，按标题识别容量"));
  for (const pass of run.passes || []) {
    const detail = pass.error || (pass.candidates != null ? `列表卡片 ${pass.cards}，进入详情 ${pass.candidates}，取到链接 ${pass.observed}` : "");
    steps.push(stepChip(!pass.error, `${passName(pass)} ${pass.error ? "失败" : `${pass.cards ?? pass.observed ?? 0} 件`}`, detail));
  }
  const logText = passDetail(run);
  const statusLabel = tone === "running" ? "搜索中" : tone === "fail" ? "搜索失败" : tone === "warn" ? "完成 · 有警告" : "已完成";
  let body;
  if (items.length) body = items.map(item => runItem(item, fresh.has(item.item_id))).join("");
  else if (run.items.length) body = `<div class="empty run-filter-empty"><b>没有符合筛选条件的商品</b><span>当前查看${scope}${runView.result ? ` · ${{matched: "命中", pending: "待确认", rejected: "不符合", incomplete: "未完成"}[runView.result] || ""}` : ""}，可以切换容量或清除筛选。</span><button type="button" class="secondary compact" data-run-clear>清除筛选</button></div>`;
  else if (tone === "running") body = `<div class="empty live-empty"><span class="live-dot"></span>正在搜索，读到的商品会实时出现在这里…</div>`;
  else if (run.ok === 0) body = `<div class="empty">本次搜索未取得商品，展开失败原因查看详情。</div>`;
  else if (run.observed) body = `<div class="empty">此历史记录未保存逐件明细，当时共看到 ${run.observed} 件。</div>`;
  else body = `<div class="empty">本次搜索暂无商品记录，可展开操作日志检查采集情况。</div>`;
  const dateLabel = value => new Date(value).toLocaleString("zh-CN", {
    ...(new Date(value).getFullYear() !== new Date().getFullYear() ? { year: "numeric" } : {}),
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
  });
  const statusName = item => item.ok == null ? "搜索中" : item.ok === 0 ? "失败" : item.error ? "有警告" : "已完成";
  document.querySelector("#run-detail").innerHTML = `
    <header class="run-model-head">
      <div class="run-model-name"><h2>iPhone ${escapeHtml(run.model)}</h2><span class="run-state ${tone}"><i aria-hidden="true"></i>${statusLabel}</span></div>
      <div class="run-round-panel">
        <div class="run-history">
          ${!runView.follow ? `<button type="button" class="run-back-latest" data-run-latest>回到最新</button>` : ""}
          <details class="run-history-menu">
            <summary aria-label="切换搜索轮次，当前${run.id === runView.topId ? "最新一轮，" : ""}${escapeHtml(formatTime(run.started_at))}">
              <svg width="15" height="15" viewBox="0 0 20 20" fill="none" aria-hidden="true"><circle cx="10" cy="10" r="7" stroke="currentColor" stroke-width="1.5"/><path d="M10 6v4l2.5 1.5" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
              <time datetime="${escapeHtml(run.started_at)}" title="${escapeHtml(formatTime(run.started_at))}">${escapeHtml(dateLabel(run.started_at))}</time>
              <svg class="run-history-chevron" width="12" height="12" viewBox="0 0 12 12" fill="none" aria-hidden="true"><path d="m3 4.5 3 3 3-3" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"/></svg>
            </summary>
            <div class="run-history-popover">
              <div class="run-history-heading"><strong>搜索历史</strong><span>共 ${group?.run_count ?? runView.runs.length} 轮</span></div>
              <div class="run-history-options">${runView.runs.map(item => `<button type="button" class="run-history-option ${item.id === run.id ? "selected" : ""}" data-run-select="${item.id}" aria-current="${item.id === run.id}">
                <span class="run-history-option-top"><time title="${escapeHtml(formatTime(item.started_at))}">${escapeHtml(dateLabel(item.started_at))}</time><span class="run-history-state ${runTone(item)}">${statusName(item)}</span></span>
                <span class="run-history-option-bottom"><span>${item.storage ? escapeHtml(displayStorage(item.storage)) : "全部容量"} · ${escapeHtml(triggerLabels[item.trigger] ?? item.trigger)}搜索</span><span>${item.id === runView.topId ? "最新一轮" : item.id === run.id ? "正在查看" : ""}</span></span>
              </button>`).join("")}</div>
              <div class="run-history-foot">累计记录 ${group?.item_count ?? 0} 件次 · 命中 ${group?.hit_count ?? 0} 件次${group?.run_count > runView.runs.length ? `<br />显示最近 ${runView.runs.length} 轮` : ""}</div>
            </div>
          </details>
        </div>
        <p class="run-session-facts">${run.region ? `<span>${escapeHtml(run.region)}</span>` : ""}<span>${escapeHtml(triggerLabels[run.trigger] ?? run.trigger)}搜索</span>${duration(run) ? `<span>${tone === "running" ? "已用时" : "用时"}<span id="run-elapsed">${duration(run).replace(/\s/g, "")}</span></span>` : ""}</p>
      </div>
    </header>
    ${tone === "running" ? `<p class="live-line"><span class="live-dot"></span>${escapeHtml(run.live?.stage || "正在搜索…")}</p>` : ""}
    ${run.error ? `<details class="notice run-issue ${run.ok === 0 ? "bad" : "warn"}" ${issueOpen ? "open" : ""}><summary>${run.ok === 0 ? "本次搜索失败" : "部分搜索步骤未完成"} · 展开原因</summary><p>${run.error.split("；").map(escapeHtml).join("<br />")}</p></details>` : ""}
    <section class="run-results" aria-label="本次搜索商品结果">
      <div class="run-capacity"><span class="run-filter-label">容量</span><div class="chips" role="group" aria-label="筛选本次商品容量">${storageFilters}</div></div>
      <div class="run-result-stats" role="group" aria-label="筛选商品判定结果">${resultFilters}</div>
      <div class="run-results-tools"><p class="hint">${runView.storage ? "按商品容量筛选；未识别时参考采集容量。" : "容量和判定筛选仅作用于本次搜索。"}</p>${hasFilter ? `<button type="button" class="run-clear" data-run-clear>清除筛选</button>` : ""}</div>
      ${reasonFilter}
      <div class="run-items">${body}</div>
    </section>
    ${logText || steps.length ? `<details class="run-log" ${logOpen ? "open" : ""}><summary>搜索步骤与操作日志<span>按需展开，查看容量筛选和采集过程</span></summary><p class="hint">关键词「${escapeHtml(run.keyword)}」 · 日志包含本轮全部容量。</p>${steps.length ? `<div class="step-chips">${steps.join("")}</div>` : ""}<pre>${escapeHtml(logText)}</pre></details>` : ""}`;
}

/* ------------------------------------------------------------------ 每日报价 */

async function loadQuoteBatches() {
  const [statuses, batches] = await Promise.all([
    api("/api/quotes/status"), api("/api/quotes/batches"),
  ]);
  document.querySelector("#quote-batches").innerHTML = statuses.map(item => {
    if (!quoteSyncDate(item)) return `<span class="batch-chip warn">${escapeHtml(item.category_name)} · 未同步</span>`;
    const ok = item.parse_status === "parsed";
    const fresh = item.fresh;
    const statusLabel = {parsing:"识别中", failed:"识别失败", needs_review:"未识别到有效报价，需复核"}[item.parse_status] || "需复核";
    const label = `${item.category_name} · ${quoteSyncDate(item).slice(5)} · ${item.entry_count}条 · 扣费项${item.deduction_count ?? 0}条${ok ? "" : ` · ${statusLabel}`}${fresh ? "" : " · 尚未取得今日有效报价"}`;
    const error = item.parse_error ? `<span class="bad">${escapeHtml(item.parse_error)}</span>` : "";
    if (!item.image_available) return `<span class="batch-chip warn">${escapeHtml(label)} · 原图缺失</span>`;
    const image = `/api/quotes/batches/${encodeURIComponent(item.batch_id)}/image`;
    return `<a class="quote-image-card ${ok && fresh ? "" : "warn"}" href="${image}" target="_blank" rel="noopener noreferrer" title="打开${escapeHtml(item.category_name)}报价原图（抓取于 ${escapeHtml(formatTime(item.fetched_at))}）">
      <img src="${image}" alt="${escapeHtml(item.category_name)}报价原图缩略图" loading="lazy" />
      <span>${escapeHtml(label)} · 打开原图 ↗</span>${error}
    </a>`;
  }).join("");
  const currentIds = new Set(statuses.map(item => item.batch_id));
  const history = batches.filter(item => item.image_available && !currentIds.has(item.id));
  const historyPanel = document.querySelector("#quote-history");
  historyPanel.hidden = history.length === 0;
  historyPanel.querySelector("summary").textContent = `历史报价原图（${history.length}）`;
  document.querySelector("#quote-history-list").innerHTML = history.map(item => {
    const image = `/api/quotes/batches/${encodeURIComponent(item.id)}/image`;
    const label = `${item.category_name} · ${item.quote_date || item.fetched_at.slice(0, 10)}`;
    return `<a href="${image}" target="_blank" rel="noopener noreferrer" title="打开${escapeHtml(label)}原图"><img src="${image}" alt="${escapeHtml(label)}缩略图" loading="lazy" /><span>${escapeHtml(label)} ↗</span></a>`;
  }).join("");
}

const variantOrder = ["", "mini", "plus", "air", "pro", "pro max", "e"];
const quoteView = { mode: "nw", series: null };
function seriesOf(model) { const m = String(model).match(/^(\d+)/); return m ? Number(m[1]) : 0; }
function variantRank(model) {
  const i = variantOrder.indexOf(String(model).replace(/^\d+\s*/, "").toLowerCase());
  return i === -1 ? 99 : i;
}
function storageNum(storage) { const v = String(storage); return v.endsWith("T") ? parseInt(v) * 1024 : parseInt(v) || 0; }
function money(value) { return Number(value).toLocaleString("zh-CN"); }

function productAnomalies(product) {
  const bad = new Set();
  let prev = null;
  for (const grade of gradeOrder) {
    const values = product.grades[grade];
    if (!values) continue;
    if (values.warranty_premium != null && values.warranty_premium < 0) bad.add(grade);
    const price = values.no_warranty?.price;
    if (price != null) { if (prev != null && price > prev) bad.add(grade); prev = price; }
  }
  return bad;
}

function quoteCell(product, grade, bad) {
  const values = product.grades[grade];
  if (!values) return `<td class="qcell empty">—</td>`;
  const nw = values.no_warranty, w = values.warranty, premium = values.warranty_premium;
  let text = "—", cls = "";
  if (quoteView.mode === "nw" && nw) text = money(nw.price);
  if (quoteView.mode === "w" && w) text = money(w.price);
  if (quoteView.mode === "premium" && premium != null) { text = `${premium >= 0 ? "+" : "−"}${money(Math.abs(premium))}`; cls = premium < 0 ? "neg" : "pos"; }
  const tip = `无保 ${nw ? "¥" + money(nw.price) : "—"} · 有保 ${w ? "¥" + money(w.price) : "—"}${premium != null ? " · 加价 " + premium : ""}（点击估价）`;
  return `<td class="qcell ${bad.has(grade) ? "anomaly" : ""}"><button class="cell-btn ${cls} ${text === "—" ? "dim" : ""}" data-model="${escapeHtml(product.model)}" data-storage="${escapeHtml(product.storage)}" data-grade="${grade}" title="${escapeHtml(tip)}">${text}</button></td>`;
}

function renderSeriesChips(seriesList, counts) {
  const chips = [["all", "全部"], ...seriesList.map(x => [String(x), x ? String(x) : "更早"])];
  document.querySelector("#series-chips").innerHTML = chips.map(([key, label]) =>
    `<button type="button" class="chip ${String(quoteView.series) === key ? "active" : ""}" data-series="${key}">${label}<small>${counts[key]}</small></button>`).join("");
}

async function loadQuotes() {
  const filter = document.querySelector("#quote-model-filter").value.trim().toLowerCase().replace(/\s+/g, "");
  const onlyBad = document.querySelector("#quote-only-anomaly").checked;
  state.products = await api("/api/apple-products");
  const all = state.products.map(product => ({ product, bad: productAnomalies(product), series: seriesOf(product.model) }));
  const searching = Boolean(filter) || onlyBad;
  const scoped = all.filter(x =>
    (!filter || `${x.product.model}${x.product.storage}g`.toLowerCase().replace(/\s+/g, "").includes(filter)) && (!onlyBad || x.bad.size));
  const seriesList = [...new Set(all.map(x => x.series))].sort((x, y) => (x === 0) - (y === 0) || y - x);
  if (quoteView.series == null || (quoteView.series !== "all" && !seriesList.includes(Number(quoteView.series)))) {
    const hasNoWarranty = x => all.some(item => item.series === x && Object.values(item.product.grades).some(g => g.no_warranty));
    quoteView.series = seriesList.find(x => x && hasNoWarranty(x)) ?? seriesList.find(x => x) ?? "all";
  }
  const counts = { all: scoped.length };
  seriesList.forEach(x => counts[String(x)] = scoped.filter(item => item.series === x).length);
  renderSeriesChips(seriesList, counts);
  const visible = scoped.filter(x => searching || quoteView.series === "all" || String(x.series) === String(quoteView.series))
    .sort((x, y) => (y.series || -1) - (x.series || -1) || variantRank(x.product.model) - variantRank(y.product.model)
      || x.product.model.localeCompare(y.product.model) || storageNum(x.product.storage) - storageNum(y.product.storage));
  let lastModel = null, lastSeries = null;
  const rows = visible.map(({ product, bad, series }) => {
    let head = "";
    if (series !== lastSeries && (quoteView.series === "all" || searching)) head = `<tr class="series-row"><td colspan="7">${series ? `iPhone ${series}` : "更早机型"}</td></tr>`;
    const first = product.model !== lastModel || series !== lastSeries;
    lastModel = product.model; lastSeries = series;
    return `${head}<tr class="qrow ${first ? "first" : ""}"><td class="mcell">${first ? escapeHtml(product.model) : ""}</td><td class="scell">${escapeHtml(displayStorage(product.storage))}</td>${gradeOrder.map(g => quoteCell(product, g, bad)).join("")}</tr>`;
  });
  document.querySelector("#quote-body").innerHTML = rows.join("") || `<tr><td colspan="7" class="empty">没有匹配的报价</td></tr>`;
  const badCount = all.filter(x => x.bad.size).length;
  document.querySelector("#anomaly-count").textContent = badCount ? `${badCount} 个需复核` : "";
}

/* ------------------------------------------------------------------ 估价 */

const estimator = { model: null, storage: null, grade: null, source: "no_warranty", checked: new Map() };
const deductionCache = new Map();
const sourceNames = { no_warranty: "无保", warranty: "有保" };

/** 扣费项名称里写到的容量（「256G蓝色」「512/1TG蓝色」）；没写则返回空数组。 */
function storagesIn(name) {
  return [...String(name).toUpperCase().matchAll(/(64|128|256|512|1T|2T)(?:GB|G)?/g)].map(match => match[1]);
}

async function deductionsFor(model, refresh = false) {
  if (refresh || !deductionCache.has(model)) {
    deductionCache.set(model, await api(`/api/quotes/deductions?model=${encodeURIComponent(model)}`));
  }
  return deductionCache.get(model);
}

async function openEstimator(model, storage, grade) {
  const product = findProduct(model, storage);
  if (!product) return;
  Object.assign(estimator, { model, storage, grade, checked: new Map() });
  estimator.source = quoteView.mode === "w" ? "warranty" : "no_warranty";
  const values = product.grades[grade];
  if (!values?.[estimator.source]) estimator.source = values?.no_warranty ? "no_warranty" : "warranty";
  document.querySelector("#est-title").textContent = `iPhone ${model} · ${displayStorage(storage)}`;
  document.querySelector("#est-deductions").innerHTML = `<p class="hint">正在读取扣费项…</p>`;
  renderEstimator();
  document.querySelector("#quote-estimator").showModal();
  try {
    await deductionsFor(model);
  } catch (error) {
    toast(error.message, true);
  }
  renderEstimator();
}

function renderEstimator() {
  const product = findProduct(estimator.model, estimator.storage);
  if (!product) return;
  const source = estimator.source;
  document.querySelectorAll("#est-source button").forEach(button => {
    button.classList.toggle("active", button.dataset.source === source);
    button.disabled = !gradeOrder.some(grade => product.grades[grade]?.[button.dataset.source]);
  });
  document.querySelector("#est-grades").innerHTML = gradeOrder.map(grade => {
    const entry = product.grades[grade]?.[source];
    return `<button type="button" class="est-grade ${grade === estimator.grade ? "active" : ""}" data-grade="${grade}" ${entry ? "" : "disabled"}>
      <span>${grade}</span><strong>${entry ? `¥${money(entry.price)}` : "—"}</strong></button>`;
  }).join("");

  const all = deductionCache.get(estimator.model)?.[source];
  const target = document.querySelector("#est-deductions");
  if (!all) {
    target.innerHTML = target.innerHTML || "";
  } else if (!all.length) {
    target.innerHTML = `<div class="est-empty">这份${sourceNames[source]}报价还没有识别出该机型的扣费项。
      <button type="button" class="secondary compact" data-action="reparse">重新识别原图</button></div>`;
  } else {
    const items = all.filter(item => item.kind === "deduct");
    const notes = all.filter(item => item.kind !== "deduct");
    const mine = items.filter(item => { const specs = storagesIn(item.name); return !specs.length || specs.includes(estimator.storage); });
    const others = items.filter(item => !mine.includes(item));
    const row = item => {
      const checked = estimator.checked.has(item.id);
      const amount = estimator.checked.get(item.id) ?? item.amount_min;
      const ranged = item.amount_max > item.amount_min || item.open_ended;
      const control = ranged
        ? `<span class="ded-amount">−¥<input type="number" class="ded-input" data-id="${item.id}" value="${amount}" min="${item.amount_min}" ${item.open_ended ? "" : `max="${item.amount_max}"`} step="10" ${checked ? "" : "disabled"} /></span>
           <small>${item.open_ended ? `${item.amount_min} 起` : `${item.amount_min}–${item.amount_max}`}</small>`
        : `<span class="ded-amount">−¥${money(item.amount_min)}</span>`;
      return `<label class="ded-row ${checked ? "checked" : ""}" title="${escapeHtml(item.text)}">
        <input type="checkbox" class="ded-check" data-id="${item.id}" ${checked ? "checked" : ""} />
        <span class="ded-name">${escapeHtml(item.name)}</span>${control}</label>`;
    };
    target.innerHTML = `
      <div class="est-section-title">扣费项 <span class="hint">勾选适用的项目；区间项可在范围内调整金额</span></div>
      <div class="ded-list">${mine.map(row).join("") || `<p class="hint">没有适用于 ${displayStorage(estimator.storage)} 的扣费项。</p>`}</div>
      ${others.length ? `<details class="ded-others"><summary>其他容量的扣费项（${others.length}）</summary><div class="ded-list">${others.map(row).join("")}</div></details>` : ""}
      ${notes.length ? `<div class="est-section-title">报价单说明</div><ul class="ded-notes">${notes.map(item => `<li>${escapeHtml(item.name)}</li>`).join("")}</ul>` : ""}`;
  }
  updateEstimateTotal();
}

function updateEstimateTotal() {
  const product = findProduct(estimator.model, estimator.storage);
  const entry = product?.grades[estimator.grade]?.[estimator.source];
  const deducted = [...estimator.checked.values()].reduce((sum, value) => sum + (Number(value) || 0), 0);
  document.querySelector("#est-sub").textContent =
    `${sourceNames[estimator.source]} · ${estimator.grade}${entry?.source === "manual" ? " · 价格已手动校正" : ""}`;
  document.querySelector("#est-total").innerHTML = entry
    ? `<div class="est-calc"><span>报价 ¥${money(entry.price)}</span><span>扣费 ${deducted ? `−¥${money(deducted)}` : "¥0"}</span>
         ${estimator.checked.size ? `<span class="hint">已勾选 ${estimator.checked.size} 项</span>` : ""}</div>
       <div class="est-final"><span>估价</span><strong>¥${money(Math.max(0, entry.price - deducted))}</strong></div>`
    : `<div class="est-calc"><span>该成色没有${sourceNames[estimator.source]}报价</span></div>`;
}

function findProduct(model, storage) {
  return state.products.find(p => p.model === model && p.storage === storage);
}

function openQuoteEditor(model, storage, grade) {
  const product = findProduct(model, storage);
  const values = product?.grades[grade];
  if (!values) return;
  const dialog = document.querySelector("#quote-editor");
  dialog.dataset.model = model; dialog.dataset.storage = storage; dialog.dataset.grade = grade;
  document.querySelector("#editor-title").textContent = `iPhone ${model} · ${displayStorage(storage)} · ${grade}`;
  const fill = (key, entry) => {
    const input = document.querySelector(`#editor-${key}`);
    input.value = entry ? entry.price : "";
    input.disabled = !entry; input.dataset.id = entry ? entry.id : "";
    document.querySelector(`#editor-${key}-src`).textContent = entry ? `来源：${entry.source}` : "该成色无此报价";
  };
  fill("nw", values.no_warranty); fill("w", values.warranty);
  dialog.showModal();
  document.querySelector(values.no_warranty ? "#editor-nw" : "#editor-w").select();
}

async function saveQuoteEditor(event) {
  event.preventDefault();
  const dialog = document.querySelector("#quote-editor");
  const { model, storage, grade } = dialog.dataset;
  try {
    for (const key of ["nw", "w"]) {
      const input = document.querySelector(`#editor-${key}`);
      if (input.disabled) continue;
      const price = Number(input.value);
      if (!(price > 0)) throw new Error("价格必须大于 0");
      await api(`/api/quotes/entries/${input.dataset.id}`, { method: "PUT", body: JSON.stringify({ model, storage, grade, price, enabled: true }) });
    }
    dialog.close();
    toast("报价已校正");
    await loadQuotes();
  } catch (error) { toast(error.message, true); }
}

/* ------------------------------------------------------------------ 事件 */

const loaders = { deals: loadDeals, targets: loadTargets, runs: loadRuns };

document.querySelector("#deal-filter").addEventListener("click", event => {
  const button = event.target.closest("[data-deal-status]");
  if (!button) return;
  dealView.status = button.dataset.dealStatus;
  renderDeals();
});

document.querySelector("#run-filter").addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  runView.status = button.dataset.status;
  document.querySelectorAll("#run-filter button").forEach(item => item.classList.toggle("active", item === button));
  loadRuns().catch(error => toast(error.message, true));
});

document.querySelector("#run-list").addEventListener("click", event => {
  const row = event.target.closest(".run-row");
  if (!row) return;
  runView.model = row.dataset.model;
  runView.selected = null;
  runView.follow = true;
  runView.storage = runView.result = runView.code = "";
  document.querySelectorAll(".run-row").forEach(item => item.classList.toggle("active", item === row));
  loadRuns().catch(error => toast(error.message, true));
  if (window.matchMedia("(max-width: 900px)").matches) document.querySelector("#run-detail").scrollIntoView({ behavior: "smooth" });
});

document.querySelector("#run-model-search").addEventListener("input", event => {
  runView.search = event.target.value;
  renderRunList();
});

document.querySelector("#run-detail").addEventListener("change", event => {
  if (event.target.id !== "run-reason-select") return;
  runView.code = event.target.value;
  loadRunDetail(runView.selected).catch(error => toast(error.message, true));
});

document.querySelector("#run-detail").addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  if (button.hasAttribute("data-run-select")) {
    runView.selected = Number(button.dataset.runSelect);
    runView.follow = runView.selected === runView.topId;
    runView.result = runView.code = "";
    document.querySelector(".run-history-menu")?.removeAttribute("open");
  } else if (button.hasAttribute("data-run-storage")) {
    runView.storage = button.dataset.runStorage;
    runView.code = "";
  } else if (button.hasAttribute("data-run-result")) {
    runView.result = button.dataset.runResult;
    runView.code = "";
  } else if (button.hasAttribute("data-run-clear")) {
    runView.storage = runView.result = runView.code = "";
  } else if (button.hasAttribute("data-run-latest")) {
    runView.follow = true;
    runView.result = runView.code = "";
    loadRuns().catch(error => toast(error.message, true));
    return;
  } else return;
  loadRunDetail(runView.selected).catch(error => toast(error.message, true));
});

// 历史面板支持点击外部和 Escape 收起。
document.addEventListener("click", event => {
  const menu = document.querySelector(".run-history-menu[open]");
  if (menu && !menu.contains(event.target)) menu.open = false;
});
document.querySelector("#run-detail").addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  const menu = document.querySelector(".run-history-menu[open]");
  if (!menu) return;
  event.preventDefault();
  menu.open = false;
  menu.querySelector("summary").focus();
});

const panelLoaders = { "deals-panel": loadDeals, "rules-panel": loadTargets, "runs-panel": loadRuns };

/** 切换页面；地址栏记住当前页（#rules 等），刷新后停在原页面。 */
function showPanel(panelId, { load = true } = {}) {
  if (panelId === "targets-panel") panelId = "rules-panel";  // 机型已并入监控规则
  const button = document.querySelector(`.tab[data-panel="${panelId}"]`);
  if (!button) return;
  document.querySelectorAll(".tab,.panel").forEach(element => element.classList.remove("active"));
  button.classList.add("active");
  document.querySelector(`#${panelId}`).classList.add("active");
  const hash = `#${panelId.replace(/-panel$/, "")}`;
  if (location.hash !== hash) history.replaceState(null, "", hash);
  window.scrollTo({ top: 0 });
  const loader = panelLoaders[panelId];
  if (load && loader) loader().catch(error => toast(error.message, true));
}

document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => showPanel(button.dataset.panel)));
window.addEventListener("hashchange", () => showPanel(`${location.hash.slice(1)}-panel`));

document.querySelectorAll("[data-refresh]").forEach(button => button.addEventListener("click", () =>
  loaders[button.dataset.refresh]().catch(error => toast(error.message, true))));

rulesForm.addEventListener("submit", saveRules);
rulesForm.addEventListener("input", event => { if (event.target.id !== "exclude-input") onRulesEdited(); });
rulesForm.addEventListener("change", event => { if (event.target.id !== "exclude-input") onRulesEdited(); });
document.querySelector("#rules-reset").addEventListener("click", () => { fillRules(state.rules); toast("已撤销未保存的修改"); });
document.querySelector("#settings-tabs").addEventListener("click", event => {
  const button = event.target.closest("[data-tab]");
  if (button) setSettingsTab(button.dataset.tab);
});
try { setSettingsTab(localStorage.getItem("settingsTab") || "region"); } catch (_) {}
document.querySelector("#rule-sentence").addEventListener("click", event => {
  const tokenButton = event.target.closest("[data-go]");
  if (tokenButton) { event.stopPropagation(); focusRule(tokenButton.dataset.go); }
});

// 排除词标签编辑
const excludeInput = document.querySelector("#exclude-input");
excludeInput.addEventListener("keydown", event => {
  if (event.isComposing) return;
  if (event.key === "Enter" || event.key === "," || event.key === "，") { event.preventDefault(); addKeywordsFromInput(); }
  else if (event.key === "Backspace" && !excludeInput.value) setKeywords(splitKeywords(field("exclude_keywords").value).slice(0, -1));
});
excludeInput.addEventListener("input", () => { if (/[,，、\n]/.test(excludeInput.value)) addKeywordsFromInput(); });
excludeInput.addEventListener("blur", addKeywordsFromInput);
document.querySelector("#exclude-editor").addEventListener("click", event => {
  const remove = event.target.closest("[data-word]");
  if (remove) { setKeywords(splitKeywords(field("exclude_keywords").value).filter(word => word !== remove.dataset.word)); return; }
  excludeInput.focus();
});

// 总开关：立即生效，不影响其他未保存的修改
document.querySelector("#rules-enabled").addEventListener("change", async event => {
  const enabled = event.target.checked;
  setPower(enabled);
  try {
    state.rules = await api("/api/rules", { method: "PUT", body: JSON.stringify(rulesPayload({ enabled })) });
    toast(enabled ? "监控已开启" : "监控已暂停，不再自动搜索");
    updateRuleHints();
    loadOverview().catch(() => {});
  } catch (error) { toast(error.message, true); setPower(!enabled); }
});

// 设备下拉
document.querySelector("#device-picker-btn").addEventListener("click", () =>
  setDeviceMenu(document.querySelector("#device-menu").hidden));
document.addEventListener("click", event => {
  if (!event.target.closest("#device-picker") && !event.target.closest("[data-go=devices]")) setDeviceMenu(false);
});
document.addEventListener("keydown", event => {
  if (event.key === "Escape" && !document.querySelector("#device-menu").hidden) { setDeviceMenu(false); document.querySelector("#device-picker-btn").focus(); }
});
document.querySelector("#refresh-devices").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.classList.add("spinning");
  try { await loadDevices(); toast("设备列表已刷新"); } catch (error) { toast(error.message, true); }
  finally { button.classList.remove("spinning"); }
});
document.querySelector("#device-options").addEventListener("change", async event => {
  if (event.target.matches("[data-device-channel]")) {
    const card = event.target.closest("[data-device]");
    await saveDeviceChannel(card.dataset.device, event.target.value);
    return;
  }
  if (!event.target.matches("[data-device-selection]")) return;
  const ids = [...document.querySelectorAll("#device-options [data-device-selection]:checked")].map(input => input.value);
  try {
    if (!ids.length) throw new Error("至少选择一台采集设备");
    await api("/api/devices/selection", { method: "PUT", body: JSON.stringify({ ids }) });
    state.devices.forEach(device => { device.selected = ids.includes(device.id); });
    renderDevices();
    updateRuleHints();
    toast("采集设备已保存，下一次搜索生效");
  } catch (error) {
    toast(error.message, true);
    await loadDevices().catch(() => {});
  }
});

async function saveDeviceChannel(deviceId, channel, clientIp) {
  try {
    const saved = await api(`/api/devices/${encodeURIComponent(deviceId)}/channel`, {
      method: "PUT", body: JSON.stringify({ channel, ...(clientIp === undefined ? {} : {client_ip: clientIp}) }),
    });
    await loadDevices();
    toast(saved.next_run ? "已保存，下次搜索生效" : channel === "proxy" ? "代理采集已选择，请完成手机连接设置" : "已切换原有采集，请关闭手机 Wi-Fi 代理");
  } catch (error) { toast(error.message, true); await loadDevices().catch(() => {}); }
}

document.querySelector("#device-options").addEventListener("click", event => {
  const button = event.target.closest("[data-save-proxy]");
  if (!button) return;
  const card = button.closest("[data-device]");
  saveDeviceChannel(card.dataset.device, "proxy", card.querySelector("[data-proxy-ip]").value.trim());
});

// 机型表
document.querySelector("#target-filter").addEventListener("input", applyTargetFilter);
document.querySelector("#target-rows").addEventListener("click", event => {
  const head = event.target.closest(".series-row");
  if (!head || event.target.closest(".series-switch") || targetQuery()) return;
  setSeriesOpen(head.dataset.series, !head.classList.contains("open"));
});
document.querySelector("#expand-all-series").addEventListener("click", () => {
  document.querySelector("#target-filter").value = "";
  const all = [...new Set(state.targets.map(item => String(item.series)))];
  const open = !all.every(series => openSeries.has(series));
  all.forEach(series => setSeriesOpen(series, open));
  applyTargetFilter();
});

async function refreshAfterTargetChange() {
  state.rules = await api("/api/rules");
  await Promise.all([loadTargets(), loadOverview()]);
}

document.querySelector("#target-rows").addEventListener("change", async event => {
  const seriesToggle = event.target.closest(".series-toggle");
  if (seriesToggle) {
    const members = state.targets.filter(item => String(item.series) === seriesToggle.dataset.series);
    const enabled = !selectionState(members.flatMap(item => item.units)).all;
    try {
      await api(`/api/target-series/${encodeURIComponent(seriesToggle.dataset.series)}`, {
        method: "PUT", body: JSON.stringify({ enabled }),
      });
      await refreshAfterTargetChange();
    } catch (error) { toast(error.message, true); await loadTargets().catch(() => {}); }
    return;
  }
  const toggle = event.target.closest(".target-toggle");
  if (!toggle) return;
  const target = state.targets.find(item => item.model === toggle.dataset.model);
  const enabled = !selectionState(target.units).all;
  try {
    await api(`/api/targets/${encodeURIComponent(toggle.dataset.model)}`, { method: "PUT", body: JSON.stringify({ enabled }) });
    await refreshAfterTargetChange();
  } catch (error) { toast(error.message, true); await loadTargets().catch(() => {}); }
});

document.querySelector("#target-rows").addEventListener("click", async event => {
  const chip = event.target.closest(".unit-chip");
  if (chip) {
    try {
      await api(`/api/targets/${encodeURIComponent(chip.dataset.model)}`, {
        method: "PUT", body: JSON.stringify({ enabled: chip.dataset.enabled !== "1", storage: chip.dataset.storage }),
      });
      await refreshAfterTargetChange();
    } catch (error) { toast(error.message, true); }
    return;
  }
  const button = event.target.closest(".run-target");
  if (!button) return;
  const target = state.targets.find(item => item.model === button.dataset.model);
  document.querySelectorAll(".run-target").forEach(item => { item.disabled = true; });
  try {
    button.lastChild.textContent = "搜索中…";
    const result = await api(`/api/targets/${encodeURIComponent(target.model)}/run`, { method: "POST" });
    const warning = result.warnings?.[0];
    toast(`iPhone ${target.model}：共 ${result.observed} 件，命中 ${result.matched} 件${warning ? `（${warning}）` : ""}`, Boolean(warning));
  } catch (error) {
    toast(error.message, true);
  }
  await Promise.all([loadTargets(), loadOverview(), loadDeals()]);
});

document.querySelector("#sync-quote").addEventListener("click", async event => {
  const button = event.currentTarget;
  button.disabled = true; button.textContent = "下载并识别中…";
  try {
    const results = await api("/api/quotes/sync", { method: "POST" });
    const errors = results.filter(result => result.error);
    const incomplete = results.filter(result => !result.error && (result.parse_status !== "parsed" || !Number(result.entry_count)));
    const count = results.reduce((sum, result) => sum + Number(result.entry_count || 0), 0);
    if (errors.length) toast(errors.map(result => result.error).join("；"), true);
    else if (incomplete.length) toast(incomplete.map(result => `${result.category_name || result.category_id}：${result.parse_error || "图片已下载，但尚无有效报价，请重新识别"}`).join("；"), true);
    else toast(results.some(result => result.updated) ? `同步完成，共有 ${count} 条有效报价` : `报价没有变化，已有 ${count} 条有效报价`);
    await Promise.all([loadQuoteBatches(), loadQuotes(), loadOverview(), loadTargets()]);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "立即同步"; }
});

document.querySelector("#quote-model-filter").addEventListener("input", () => { clearTimeout(loadQuotes.t); loadQuotes.t = setTimeout(loadQuotes, 200); });
document.querySelector("#quote-only-anomaly").addEventListener("change", loadQuotes);
document.querySelector("#quote-body").addEventListener("click", event => {
  const cell = event.target.closest(".cell-btn");
  if (cell) openEstimator(cell.dataset.model, cell.dataset.storage, cell.dataset.grade).catch(error => toast(error.message, true));
});
document.querySelector("#series-chips").addEventListener("click", event => {
  const chip = event.target.closest(".chip");
  if (chip) { quoteView.series = chip.dataset.series; loadQuotes(); }
});
document.querySelector("#quote-mode").addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button) return;
  quoteView.mode = button.dataset.mode;
  document.querySelectorAll("#quote-mode button").forEach(x => x.classList.toggle("active", x === button));
  loadQuotes();
});
document.querySelector("#quote-editor-form").addEventListener("submit", saveQuoteEditor);

const estimatorDialog = document.querySelector("#quote-estimator");
document.querySelector("#est-close").addEventListener("click", () => estimatorDialog.close());
estimatorDialog.addEventListener("click", event => { if (event.target === estimatorDialog) estimatorDialog.close(); });
document.querySelector("#est-source").addEventListener("click", event => {
  const button = event.target.closest("button");
  if (!button || button.dataset.source === estimator.source) return;
  estimator.source = button.dataset.source;
  estimator.checked = new Map();  // 无保 / 有保的扣费项不同，切换时清空勾选
  renderEstimator();
});
document.querySelector("#est-grades").addEventListener("click", event => {
  const button = event.target.closest(".est-grade");
  if (!button) return;
  estimator.grade = button.dataset.grade;
  renderEstimator();
});
document.querySelector("#est-deductions").addEventListener("change", event => {
  const check = event.target.closest(".ded-check");
  if (check) {
    const id = Number(check.dataset.id);
    const item = deductionCache.get(estimator.model)?.[estimator.source]?.find(entry => entry.id === id);
    check.checked ? estimator.checked.set(id, item?.amount_min ?? 0) : estimator.checked.delete(id);
    const row = check.closest(".ded-row");
    row.classList.toggle("checked", check.checked);
    const input = row.querySelector(".ded-input");
    if (input) { input.disabled = !check.checked; if (check.checked) estimator.checked.set(id, Number(input.value)); }
    updateEstimateTotal();
  }
});
document.querySelector("#est-deductions").addEventListener("input", event => {
  const input = event.target.closest(".ded-input");
  if (!input || !estimator.checked.has(Number(input.dataset.id))) return;
  estimator.checked.set(Number(input.dataset.id), Math.max(Number(input.min) || 0, Number(input.value) || 0));
  updateEstimateTotal();
});
document.querySelector("#est-deductions").addEventListener("click", event => {
  if (event.target.closest('[data-action="reparse"]')) reparseQuotes(event.target.closest("button"));
});
document.querySelector("#est-reset").addEventListener("click", () => { estimator.checked = new Map(); renderEstimator(); });
document.querySelector("#est-edit").addEventListener("click", () => {
  estimatorDialog.close();
  openQuoteEditor(estimator.model, estimator.storage, estimator.grade);
});

async function reparseQuotes(button) {
  const label = button?.textContent;
  if (button) { button.disabled = true; button.textContent = "识别中…"; }
  try {
    const results = await api("/api/quotes/reparse", { method: "POST" });
    const errors = results.filter(result => result.error);
    const incomplete = results.filter(result => !result.error && result.parse_status !== "parsed");
    deductionCache.clear();
    if (errors.length) toast(errors.map(result => result.error).join("；"), true);
    else if (incomplete.length) toast(incomplete.map(result => result.parse_error || "原图未识别到有效报价，请核对原图").join("；"), true);
    else if (!results.length) toast("还没有报价原图，请先立即同步", true);
    else toast(`已重新识别：${results.map(r => `${r.entry_count} 条报价、${r.deduction_count} 条扣费项`).join("；")}`);
    await Promise.all([loadQuoteBatches(), loadQuotes(), loadOverview(), loadTargets()]);
    if (estimatorDialog.open) { await deductionsFor(estimator.model, true); renderEstimator(); }
  } catch (error) { toast(error.message, true); }
  finally { if (button) { button.disabled = false; button.textContent = label; } }
}
document.querySelector("#reparse-quote").addEventListener("click", event => reparseQuotes(event.currentTarget));
document.querySelector("#editor-cancel").addEventListener("click", () => document.querySelector("#quote-editor").close());

document.querySelector("#test-notification").addEventListener("click", async () => {
  try {
    const result = await api("/api/notifications/test", { method: "POST" });
    toast(result.configured_channels.length ? "测试推送已发送" : "尚未在 .env 配置推送渠道", !result.configured_channels.length);
  } catch (error) { toast(error.message, true); }
});

const smartUI = initSmart({api, escapeHtml, toast, state, gradeOrder, loadOverview, loadDeals, updateRuleHints, onDirty: refreshDirty});

async function initialize() {
  if (location.hash) showPanel(`${location.hash.slice(1)}-panel`, { load: false });
  try {
    await Promise.all([loadOverview(), loadRules(), loadDevices(), smartUI.loadConfig()]);
    await Promise.all([loadDeals(), loadTargets(), loadQuoteBatches(), loadQuotes()]);
  } catch (error) {
    document.querySelector("#health").textContent = "连接失败";
    toast(error.message, true);
  }
}
initialize();
// 每 3 秒一拍：正在搜索时状态条、命中列表实时刷新；运行记录页一直实时刷新（内容不变不重绘）。
// 空闲时状态条和命中列表 30 秒刷新一次。
let refreshTick = 0;
setInterval(() => {
  if (document.hidden) return;
  refreshTick += 1;
  if (!document.querySelector("#device-menu").hidden && refreshTick % 2 === 0 && !document.activeElement?.closest("#device-options")) loadDevices().catch(() => {});
  const busy = Boolean(state.overview?.busy) || runView.running;
  if (busy || refreshTick % 10 === 0) loadOverview().catch(() => {});
  if (document.querySelector("#runs-panel.active")) loadRuns({ quiet: true }).catch(() => {});
  if (document.querySelector("#deals-panel.active") && (busy || refreshTick % 10 === 0)) loadDeals().catch(() => {});
}, 3000);
