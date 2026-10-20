const state = { products: [], rules: null, overview: null, targets: [] };
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

async function loadOverview() {
  const value = await api("/api/overview");
  state.overview = value;
  const rules = value.rules;
  const health = document.querySelector("#health");
  health.textContent = value.track_db_available ? "服务正常" : "服务正常 · 未找到闲鱼曝光 DB";
  health.classList.add("online");
  health.classList.toggle("warn", !value.track_db_available);

  const noWarranty = value.quotes.find(q => q.category_id === rules.quote_category) || value.quotes[0];
  const quoteFresh = Boolean(noWarranty?.fresh);
  const quoteStep = {
    label: "① 每日报价",
    main: noWarranty?.quote_date ? `${noWarranty.quote_date.slice(5)} · ${noWarranty.entry_count} 条` : "尚未同步",
    sub: quoteFresh ? `今天已更新 · 每天 ${value.quote_schedule} 起检查` : `等待今天的报价 · 每天 ${value.quote_schedule} 起检查`,
    tone: quoteFresh ? "ok" : "warn",
  };
  const ruleStep = {
    label: "② 监控规则",
    main: `${rules.region_city || "全国"} · 全部 iPhone 机型`,
    sub: `${rules.quote_category === "100027" ? "有保" : "无保"}${rules.quote_grade}回收价 · 最多高于 ¥${rules.max_over_quote}`,
    tone: "ok",
  };
  const schedule = value.schedule;
  const searchStep = {
    label: "③ 闲鱼搜索",
    main: value.busy ? "正在搜索…" : schedule.active ? (schedule.next_label ? `下一个：iPhone ${schedule.next_label}` : "本轮已搜完") : "未在搜索",
    sub: schedule.active ? `${rules.search_start}–${rules.search_end} · ${schedule.units} 个搜索单元 · 今日 ${value.runs_today} 次` : schedule.reason,
    tone: schedule.active ? "ok" : (rules.enabled ? "warn" : "off"),
  };
  const dealStep = {
    label: "④ 命中推送",
    main: `今日 ${value.deals_today} 件`,
    sub: value.notification_channels.length ? `推送到 ${value.notification_channels.join(" / ")}` : "未配置推送渠道",
    tone: value.notification_channels.length ? "ok" : "warn",
  };
  document.querySelector("#pipeline").innerHTML = [quoteStep, ruleStep, searchStep, dealStep].map(step =>
    `<div class="step ${step.tone}"><span class="step-label">${step.label}</span><strong>${escapeHtml(step.main)}</strong><span class="step-sub">${escapeHtml(step.sub)}</span></div>`
  ).join("");
}

/* ------------------------------------------------------------------ 命中商品 */

async function loadDeals() {
  const deals = await api("/api/deals");
  document.querySelector("#deal-list").innerHTML = deals.length ? deals.map(deal => {
    const push = { sent: ["已推送", ""], failed: ["推送失败", "bad"], disabled: ["未配置推送", "muted"], pending: ["待推送", "muted"] }[deal.notification_status] || [deal.notification_status, "muted"];
    return `<article class="deal">
      <div class="deal-main">
        <a class="deal-title" href="${escapeHtml(deal.item_url)}" target="_blank" rel="noreferrer">${escapeHtml(deal.title)}</a>
        <div class="deal-meta">
          <span class="tag strong">iPhone ${escapeHtml(deal.model)} · ${escapeHtml(displayStorage(deal.storage))}</span>
          ${deal.region || deal.display_region ? `<span class="tag">${escapeHtml(deal.region || deal.display_region)}</span>` : ""}
          <span>${escapeHtml(deal.grade)}回收报价 ¥${deal.quote_price}</span>
          <span>${shortTime(deal.matched_at)}</span>
          <span class="push ${push[1]}">${push[0]}</span>
        </div>
      </div>
      <div class="deal-price">
        <strong>¥${deal.item_price}</strong>
        <span class="margin">${quoteDifferenceLabel(deal.quote_price, deal.item_price)}</span>
      </div>
    </article>`;
  }).join("") : `<div class="empty">还没有命中的商品。<br /><span class="hint">报价和规则设好后，系统会在搜索时段内自动轮流搜索各机型。</span></div>`;
}

/* ------------------------------------------------------------------ 规则 */

const ruleFields = ["region_city", "region_path", "quote_category", "quote_grade", "max_over_quote",
  "min_price_ratio", "exclude_keywords", "scroll_pages",
  "search_start", "search_end", "model_interval_minutes"];
const numberFields = new Set(["max_over_quote", "min_price_ratio", "model_interval_minutes", "scroll_pages"]);

async function loadRules() {
  const rules = await api("/api/rules");
  state.rules = rules;
  const form = document.querySelector("#rules-form");
  form.elements.enabled.checked = rules.enabled;
  ruleFields.forEach(name => { form.elements[name].value = rules[name]; });
  updateRuleHints();
}

async function updateRuleHints() {
  const form = document.querySelector("#rules-form");
  const city = form.elements.region_city.value.trim();
  const custom = form.elements.region_path.value.trim();
  let path = custom;
  if (city && !custom) path = (await api(`/api/rules/region-path?city=${encodeURIComponent(city)}`)).path;
  form.elements.region_path.placeholder = path || "留空自动生成，例如 区域 > 福建 > 厦门 > 查看";
  document.querySelector("#region-hint").innerHTML = city
    ? `每次搜索会在 App 内依次点击 <b>${escapeHtml(path)}</b>（? 开头的步骤可选）；同时核对商品卡片上的地区，不是${escapeHtml(city)}的不推送。`
    : "未设置城市：搜索全国，不核对地区。";
  // 搜索方式与节奏的估算：每次搜索 = 打开搜索 + 区域筛选 + 容量筛选 + 新发读一轮
  const scrolls = Math.max(0, Number(form.elements.scroll_pages.value) || 0);
  document.querySelector("#storage-hint").innerHTML =
    `每款机型只搜索一次，程序会自动切换各个容量，并仅查看「新发」商品` +
    (scrolls ? `并向下翻 ${scrolls} 屏读完第一页。` : `并读取当前一屏。`);
  const units = state.targets.filter(target => target.enabled && target.units.some(unit => unit.enabled)).length;
  const capacityCount = state.targets.reduce((sum, target) =>
    sum + (target.enabled ? target.units.filter(unit => unit.enabled).length : 0), 0);
  const interval = Number(form.elements.model_interval_minutes.value) || 90;
  const seconds = units ? Math.ceil((units * (4 + (city ? 3 : 0)) + capacityCount * (6 + 4 * scrolls)) / units) : 0;
  const round = Math.ceil((units * (4 + (city ? 3 : 0)) + capacityCount * (6 + 4 * scrolls + 8)) / 60);
  document.querySelector("#cadence-hint").innerHTML = units
    ? `当前 ${units} 款机型、${capacityCount} 个容量筛选项，每款机型只打开一次搜索页，平均约 ${seconds} 秒，搜完一轮约 ${round} 分钟。` +
      (round > interval ? ` <span class="warn-text">间隔小于一轮耗时，同一机型实际约每 ${round} 分钟搜一次。</span>` : ` 同一机型每 ${interval} 分钟搜一次。`)
    : "";
}

async function saveRules(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const payload = { ...state.rules, enabled: form.elements.enabled.checked };
  ruleFields.forEach(name => {
    const value = form.elements[name].value;
    payload[name] = numberFields.has(name) ? Number(value) : value;
  });
  ["region_steps", "storage_steps_example", "pass_list"].forEach(key => delete payload[key]);
  try {
    state.rules = await api("/api/rules", { method: "PUT", body: JSON.stringify(payload) });
    toast("规则已保存，下一次搜索生效");
    await Promise.all([loadTargets(), loadOverview()]);
  } catch (error) { toast(error.message, true); }
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
    data-enabled="${unit.enabled ? 1 : 0}" ${target.enabled ? "" : "disabled"}
    title="${escapeHtml(detail)}\n点击${unit.enabled ? "停用" : "启用"}这个容量">${displayStorage(unit.storage)}${run && run.ok ? `<small>${run.observed}${run.matched ? `·${run.matched}` : ""}</small>` : ""}</button>`;
}

function selectionState(units) {
  const selected = units.filter(unit => unit.enabled).length;
  return { all: units.length > 0 && selected === units.length, partial: selected > 0 && selected < units.length, any: selected > 0 };
}

async function loadTargets() {
  const targets = await api("/api/targets");
  state.targets = targets;
  let lastSeries = null;
  const rows = [];
  for (const target of targets) {
    if (target.series !== lastSeries) {
      const seriesUnits = targets.filter(item => item.series === target.series).flatMap(item => item.units);
      const seriesSelection = selectionState(seriesUnits);
      rows.push(`<tr class="series-row"><td colspan="5"><label class="switch small series-switch">
        <input type="checkbox" class="series-toggle" data-series="${target.series}" data-partial="${seriesSelection.partial ? 1 : 0}" ${seriesSelection.all ? "checked" : ""} />
        <b>${target.series ? `iPhone ${target.series}` : "更早机型"} 全部机型</b></label></td></tr>`);
      lastSeries = target.series;
    }
    const modelSelection = selectionState(target.units);
    const storages = `<div class="unit-chips">${target.units.map(unit => unitChip(target, unit)).join("")}</div>`;
    const runnable = modelSelection.any;
    rows.push(`<tr data-enabled="${modelSelection.any ? 1 : 0}" class="${modelSelection.any ? "" : "off"}">
      <td><label class="switch small"><input type="checkbox" class="target-toggle" data-model="${escapeHtml(target.model)}" data-partial="${modelSelection.partial ? 1 : 0}" ${modelSelection.all ? "checked" : ""} ${target.series_enabled ? "" : "disabled"} /> <b>${escapeHtml(target.model)}</b></label></td>
      <td>${storages}</td>
      <td>${runSummary(target.last_run)}</td>
      <td class="num">${target.deals || ""}</td>
      <td class="num"><button class="compact secondary run-target" data-model="${escapeHtml(target.model)}" ${runnable ? "" : "disabled"}>搜索</button></td>
    </tr>`);
  }
  document.querySelector("#target-rows").innerHTML = rows.join("") ||
    `<tr><td colspan="5" class="empty">报价表里还没有机型，请先在「每日报价」同步。</td></tr>`;
  document.querySelectorAll("#target-rows .series-toggle, #target-rows .target-toggle").forEach(input => {
    input.indeterminate = input.dataset.partial === "1";
  });
  updateRuleHints();
}

/* ------------------------------------------------------------------ 运行记录 */

/** 操作日志：每轮（容量）的点击步骤、读到的卡片 / 曝光数和商品，用于排查。 */
function passDetail(run) {
  const lines = [];
  for (const p of run.passes || []) {
    lines.push(p.error
      ? `${passName(p)}：${p.error}`
      : `${passName(p)}：关联 ${p.observed} 件` + (p.cards != null ? `（页面卡片 ${p.cards}，曝光记录 ${p.exposures}）` : ""));
    for (const action of p.actions || []) {
      const target = action.target || action.steps?.join(" → ") || "";
      lines.push(`操作${action.success ? "" : "失败"}：${action.action}${target ? `「${target}」` : ""}` +
        (action.method ? `（${action.method}）` : "") +
        (action.text_method ? `；输入：${action.text_method}` : "") + (action.error ? `：${action.error}` : ""));
      if (action.clicked?.length && action.steps?.length) lines.push(`实际点击：${action.clicked.join(" → ")}`);
      for (const method of action.methods || []) lines.push(`  ${method.target}：${method.method}`);
      if (action.skipped?.length) lines.push(`跳过可选项：${action.skipped.join("、")}`);
    }
    for (const item of p.items || []) {
      lines.push(`商品：¥${item.price} ${item.title}${item.display_region ? ` · ${item.display_region}` : ""} · ID ${item.item_id}`);
    }
    if (p.items_truncated) lines.push(`其余商品未展开（本轮共 ${p.items_total} 件）`);
  }
  if (run.error) lines.push(run.error);
  return lines.join("\n");
}

const runView = { status: "", selected: null, code: "" };
const triggerLabels = { schedule: "定时", manual: "手动", inject: "调试" };
const codeLabels = {
  matched: "命中", price_high: "价格偏高", region: "地区不符", unrecognized: "无法识别",
  excluded: "排除词", too_cheap: "疑似引流", no_quote: "无报价", other_model: "其他机型", other: "其他",
};
/** 每一轮的名字：按容量筛选时显示容量（「256G」），否则显示按钮名（「新发」）。 */
function passName(p) {
  return p.storage ? displayStorage(p.storage) : p.label;
}

const codeOrder = ["matched", "price_high", "region", "other_model", "unrecognized", "excluded", "too_cheap", "no_quote", "other"];

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
  if (!run.finished_at) return "";
  const seconds = Math.round((new Date(run.finished_at) - new Date(run.started_at)) / 1000);
  return seconds >= 60 ? `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒` : `${seconds} 秒`;
}

async function loadRuns() {
  const query = runView.status ? `&status=${runView.status}` : "";
  const runs = await api(`/api/runs?limit=120${query}`);
  if (!runs.some(run => run.id === runView.selected)) runView.selected = runs[0]?.id ?? null;
  document.querySelector("#run-list").innerHTML = runs.length ? runs.map(run => {
    const tone = runTone(run);
    const count = run.item_count || run.observed;
    const passes = (run.passes || []).map(p => `${passName(p)} ${p.error ? "✕" : p.observed}`).join(" · ");
    const status = tone === "fail" ? `<span class="pill bad">失败</span>`
      : tone === "running" ? `<span class="pill">进行中</span>`
      : `<span class="pill">${count} 件</span>${run.hit_count ? `<span class="pill hit">命中 ${run.hit_count}</span>` : ""}${tone === "warn" ? `<span class="pill warn">警告</span>` : ""}`;
    return `<button type="button" class="run-row ${tone} ${run.id === runView.selected ? "active" : ""}" data-id="${run.id}">
      <span class="run-row-top"><b>${escapeHtml(unitName(run))}</b><span class="run-time">${shortTime(run.started_at)}</span></span>
      <span class="run-row-meta">${status}<span class="run-passes">${escapeHtml(passes)}</span>${run.trigger !== "schedule" ? `<span class="run-trigger">${triggerLabels[run.trigger] ?? run.trigger}</span>` : ""}</span>
    </button>`;
  }).join("") : `<div class="empty">${runView.status ? "没有符合条件的记录。" : "还没有搜索记录。"}</div>`;
  if (runView.selected) await loadRunDetail(runView.selected);
  else document.querySelector("#run-detail").innerHTML = `<div class="empty">选择左侧的一条记录查看详情。</div>`;
}

function stepChip(ok, label, detail) {
  const icon = ok === true ? "✓" : ok === false ? "✕" : "–";
  return `<span class="step-chip ${ok === true ? "ok" : ok === false ? "bad" : ""}" title="${escapeHtml(detail || "")}">${icon} ${escapeHtml(label)}</span>`;
}

function runItem(item) {
  const code = item.code || "other";
  const above = item.code === "price_high" && item.threshold_price != null ? item.price - item.threshold_price : null;
  const badge = item.matched
    ? `<span class="item-badge hit">命中 · ${quoteDifferenceLabel(item.quote_price, item.price)}</span>${item.new_deal ? `<span class="item-badge">本次推送</span>` : `<span class="item-badge muted">之前已推送</span>`}`
    : `<span class="item-badge ${code}">${codeLabels[code] || code}</span>`;
  const quote = item.quote_price != null
    ? `回收报价 ¥${item.quote_price} · 推送上限 ¥${Math.round(item.threshold_price)}${above != null ? ` · <b class="bad">高出上限 ¥${Math.round(above)}</b>` : ""}`
    : "";
  return `<article class="run-item ${item.matched ? "hit" : ""}">
    <div class="run-item-main">
      <a class="run-item-title" href="${escapeHtml(item.item_url)}" target="_blank" rel="noreferrer">${escapeHtml(item.title)}</a>
      <div class="run-item-meta">
        ${badge}
        ${item.model ? `<span>${escapeHtml(item.model)} ${escapeHtml(displayStorage(item.storage))}</span>` : ""}
        ${item.display_region ? `<span>${escapeHtml(item.display_region)}</span>` : ""}
        ${item.pass_label ? `<span class="pass-tag">${escapeHtml(item.pass_label)}</span>` : ""}
      </div>
      <div class="run-item-reason">${item.matched || item.code === "price_high" ? quote : `${escapeHtml(item.decision || "")}${quote ? ` · ${quote}` : ""}`}</div>
    </div>
    <div class="run-item-price">¥${item.price}</div>
  </article>`;
}

async function loadRunDetail(id) {
  const run = await api(`/api/runs/${id}`);
  const tone = runTone(run);
  const steps = [];
  if (run.ok !== 0 && run.region) steps.push(stepChip(Boolean(run.region_applied), `区域 ${run.region}`, run.region_applied ? "已在 App 内切换" : "未切换，只核对卡片地区"));
  if (run.ok !== 0 && run.storage) steps.push(stepChip(Boolean(run.filter_applied), `容量 ${displayStorage(run.storage)}`, run.filter_applied ? "已在 App 内筛选" : "未筛选，按标题识别容量"));
  for (const p of run.passes || []) {
    const detail = p.error || (p.cards != null ? `页面卡片 ${p.cards}，曝光记录 ${p.exposures}，关联 ${p.observed}` : "");
    steps.push(stepChip(p.error ? false : true, `${passName(p)} ${p.error ? "失败" : `${p.observed} 件`}`, detail));
  }
  const codes = run.codes || {};
  const total = run.items.length;
  if (runView.code && !codes[runView.code]) runView.code = "";
  const filters = total ? [`<button type="button" class="chip ${runView.code ? "" : "active"}" data-code="">全部<small>${total}</small></button>`,
    ...codeOrder.filter(code => codes[code]).map(code =>
      `<button type="button" class="chip ${code === "matched" ? "chip-hit" : ""} ${runView.code === code ? "active" : ""}" data-code="${code}">${codeLabels[code]}<small>${codes[code]}</small></button>`)].join("") : "";
  const items = run.items.filter(item => !runView.code || (item.code || "other") === runView.code);
  const logText = passDetail(run);
  let body;
  if (total) body = items.map(runItem).join("");
  else if (run.ok === 0) body = `<div class="empty bad">这次搜索失败了，没有读到商品。</div>`;
  else if (run.observed) body = `<div class="empty">这条记录早于逐件明细功能，只知道看到了 ${run.observed} 件。</div>`;
  else body = `<div class="empty">这次没有读到商品。<br /><span class="hint">把鼠标停在上面的「新发 / 降价」上，可以看到页面卡片数和曝光记录数。</span></div>`;

  document.querySelector("#run-detail").innerHTML = `
    <div class="detail-head ${tone}">
      <div>
        <h2>iPhone ${escapeHtml(unitName(run))}</h2>
        <p class="hint">${formatTime(run.started_at)}${duration(run) ? ` · 用时 ${duration(run)}` : ""} · ${triggerLabels[run.trigger] ?? run.trigger}搜索 · 关键词「${escapeHtml(run.keyword)}」</p>
      </div>
      <div class="detail-stats">
        <div><strong>${total || run.observed}</strong><span>看到</span></div>
        <div class="${run.hit_count ? "hit" : ""}"><strong>${run.hit_count || 0}</strong><span>命中</span></div>
        <div><strong>${run.new_count || 0}</strong><span>新推送</span></div>
      </div>
    </div>
    ${steps.length ? `<div class="step-chips">${steps.join("")}</div>` : ""}
    ${run.error ? `<div class="notice ${run.ok === 0 ? "bad" : "warn"}">${run.error.split("；").map(escapeHtml).join("<br />")}</div>` : ""}
    ${filters ? `<div class="chips code-filter">${filters}</div>` : ""}
    <div class="run-items">${body}</div>
    ${logText ? `<details class="run-log"><summary>操作日志</summary><pre>${escapeHtml(logText)}</pre></details>` : ""}`;
}

/* ------------------------------------------------------------------ 每日报价 */

async function loadQuoteBatches() {
  const [statuses, batches] = await Promise.all([
    api("/api/quotes/status"), api("/api/quotes/batches"),
  ]);
  document.querySelector("#quote-batches").innerHTML = statuses.map(item => {
    if (!item.quote_date) return `<span class="batch-chip warn">${escapeHtml(item.category_name)} · 未同步</span>`;
    const ok = item.parse_status === "parsed";
    const fresh = item.fresh;
    const label = `${item.category_name} · ${item.quote_date.slice(5)} · ${item.entry_count}条${ok ? "" : " · 需复核"}${fresh ? "" : " · 非今日"}`;
    if (!item.image_available) return `<span class="batch-chip warn">${escapeHtml(label)} · 原图缺失</span>`;
    const image = `/api/quotes/batches/${encodeURIComponent(item.batch_id)}/image`;
    return `<a class="quote-image-card ${ok && fresh ? "" : "warn"}" href="${image}" target="_blank" rel="noopener noreferrer" title="打开${escapeHtml(item.category_name)}报价原图（抓取于 ${escapeHtml(formatTime(item.fetched_at))}）">
      <img src="${image}" alt="${escapeHtml(item.category_name)}报价原图缩略图" loading="lazy" />
      <span>${escapeHtml(label)} · 打开原图 ↗</span>
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
  const tip = `无保 ${nw ? "¥" + money(nw.price) : "—"} · 有保 ${w ? "¥" + money(w.price) : "—"}${premium != null ? " · 加价 " + premium : ""}（点击校正）`;
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
  runView.selected = Number(row.dataset.id);
  runView.code = "";
  document.querySelectorAll(".run-row").forEach(item => item.classList.toggle("active", item === row));
  loadRunDetail(runView.selected).catch(error => toast(error.message, true));
  if (window.matchMedia("(max-width: 900px)").matches) document.querySelector("#run-detail").scrollIntoView({ behavior: "smooth" });
});

document.querySelector("#run-detail").addEventListener("click", event => {
  const chip = event.target.closest(".code-filter .chip");
  if (!chip) return;
  runView.code = chip.dataset.code;
  loadRunDetail(runView.selected).catch(error => toast(error.message, true));
});

document.querySelectorAll(".tab").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll(".tab,.panel").forEach(element => element.classList.remove("active"));
  button.classList.add("active");
  document.querySelector(`#${button.dataset.panel}`).classList.add("active");
  const loader = { "deals-panel": loadDeals, "rules-panel": loadTargets, "runs-panel": loadRuns }[button.dataset.panel];
  if (loader) loader().catch(error => toast(error.message, true));
}));

document.querySelectorAll("[data-refresh]").forEach(button => button.addEventListener("click", () =>
  loaders[button.dataset.refresh]().catch(error => toast(error.message, true))));

const rulesForm = document.querySelector("#rules-form");
rulesForm.addEventListener("submit", saveRules);
["region_city", "region_path", "model_interval_minutes", "scroll_pages"].forEach(name =>
  rulesForm.elements[name].addEventListener("input", () => { clearTimeout(updateRuleHints.t); updateRuleHints.t = setTimeout(updateRuleHints, 250); }));

document.querySelector("#target-rows").addEventListener("change", async event => {
  const seriesToggle = event.target.closest(".series-toggle");
  if (seriesToggle) {
    const members = state.targets.filter(item => String(item.series) === seriesToggle.dataset.series);
    const enabled = !selectionState(members.flatMap(item => item.units)).all;
    try {
      await api(`/api/target-series/${encodeURIComponent(seriesToggle.dataset.series)}`, {
        method: "PUT", body: JSON.stringify({ enabled }),
      });
      state.rules = await api("/api/rules");
      await Promise.all([loadTargets(), loadOverview()]);
    } catch (error) { toast(error.message, true); await loadTargets().catch(() => {}); }
    return;
  }
  const toggle = event.target.closest(".target-toggle");
  if (!toggle) return;
  const target = state.targets.find(item => item.model === toggle.dataset.model);
  const enabled = !selectionState(target.units).all;
  try {
    await api(`/api/targets/${encodeURIComponent(toggle.dataset.model)}`, { method: "PUT", body: JSON.stringify({ enabled }) });
    state.rules = await api("/api/rules");
    await Promise.all([loadTargets(), loadOverview()]);
  } catch (error) { toast(error.message, true); await loadTargets().catch(() => {}); }
});

document.querySelector("#target-rows").addEventListener("click", async event => {
  const chip = event.target.closest(".unit-chip");
  if (chip) {
    try {
      await api(`/api/targets/${encodeURIComponent(chip.dataset.model)}`, {
        method: "PUT", body: JSON.stringify({ enabled: chip.dataset.enabled !== "1", storage: chip.dataset.storage }),
      });
      state.rules = await api("/api/rules");
      await Promise.all([loadTargets(), loadOverview()]);
    } catch (error) { toast(error.message, true); }
    return;
  }
  const button = event.target.closest(".run-target");
  if (!button) return;
  const target = state.targets.find(item => item.model === button.dataset.model);
  document.querySelectorAll(".run-target").forEach(item => { item.disabled = true; });
  try {
    button.textContent = "搜索中…";
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
    const count = results.reduce((sum, result) => sum + Number(result.entry_count || 0), 0);
    if (errors.length) toast(errors.map(result => result.error).join("；"), true);
    else toast(results.some(result => result.updated) ? `同步完成，共识别 ${count} 条报价` : "报价没有变化");
    await Promise.all([loadQuoteBatches(), loadQuotes(), loadOverview(), loadTargets()]);
  } catch (error) { toast(error.message, true); }
  finally { button.disabled = false; button.textContent = "立即同步"; }
});

document.querySelector("#quote-model-filter").addEventListener("input", () => { clearTimeout(loadQuotes.t); loadQuotes.t = setTimeout(loadQuotes, 200); });
document.querySelector("#quote-only-anomaly").addEventListener("change", loadQuotes);
document.querySelector("#quote-body").addEventListener("click", event => {
  const cell = event.target.closest(".cell-btn");
  if (cell) openQuoteEditor(cell.dataset.model, cell.dataset.storage, cell.dataset.grade);
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
document.querySelector("#editor-cancel").addEventListener("click", () => document.querySelector("#quote-editor").close());

document.querySelector("#test-notification").addEventListener("click", async () => {
  try {
    const result = await api("/api/notifications/test", { method: "POST" });
    toast(result.configured_channels.length ? "测试推送已发送" : "尚未在 .env 配置推送渠道", !result.configured_channels.length);
  } catch (error) { toast(error.message, true); }
});

async function initialize() {
  try {
    await Promise.all([loadOverview(), loadRules()]);
    await Promise.all([loadDeals(), loadTargets(), loadQuoteBatches(), loadQuotes()]);
  } catch (error) {
    document.querySelector("#health").textContent = "连接失败";
    toast(error.message, true);
  }
}
initialize();
// 状态条每 30 秒刷新一次；命中列表在页面可见时一起刷新。
setInterval(() => {
  if (document.hidden) return;
  loadOverview().catch(() => {});
  if (document.querySelector("#deals-panel.active")) loadDeals().catch(() => {});
  if (document.querySelector("#runs-panel.active")) loadRuns().catch(() => {});
}, 30000);
