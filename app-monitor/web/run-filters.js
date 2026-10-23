/** 容量筛选只作用于当前搜索，不把搜索所选容量当成商品明确写出的容量。 */
export function normalizeCapacity(value) {
  const match = String(value || "").trim().toUpperCase().match(/^(64|128|256|512|1T|2T)(?:G|GB|B)?$/);
  return match?.[1] || "";
}

export function itemCapacity(item, run = {}) {
  return normalizeCapacity(item.storage) || normalizeCapacity(item.pass_label) || normalizeCapacity(run.storage);
}

export function capacityOptions(runs, run) {
  const values = new Set();
  for (const entry of [...runs, run]) {
    values.add(normalizeCapacity(entry.storage));
    for (const pass of entry.passes || []) values.add(normalizeCapacity(pass.storage));
  }
  for (const item of run.items || []) values.add(itemCapacity(item, run));
  values.delete("");
  const size = value => value.endsWith("T") ? Number(value.slice(0, -1)) * 1024 : Number(value);
  return [...values].sort((a, b) => size(a) - size(b));
}

export function resultBucket(item) {
  if (item.matched || item.code === "matched") return "matched";
  return item.code === "pending" ? "pending" : "rejected";
}

export function filterRunItems(run, { storage = "", result = "", code = "" } = {}) {
  const all = run.items || [];
  const capacities = {};
  for (const item of all) {
    const key = itemCapacity(item, run) || "unknown";
    capacities[key] = (capacities[key] || 0) + 1;
  }
  const scoped = all.filter(item => !storage || (itemCapacity(item, run) || "unknown") === storage);
  const counts = { all: scoped.length, matched: 0, pending: 0, rejected: 0 };
  const reasons = {};
  for (const item of scoped) {
    const bucket = resultBucket(item);
    counts[bucket] += 1;
    if (bucket === "rejected") {
      const reason = item.code || "other";
      reasons[reason] = (reasons[reason] || 0) + 1;
    }
  }
  const items = scoped.filter(item => (!result || resultBucket(item) === result)
    && (result !== "rejected" || !code || (item.code || "other") === code));
  return { items, counts, reasons, capacities };
}
