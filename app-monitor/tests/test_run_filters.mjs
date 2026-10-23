import assert from "node:assert/strict";
import test from "node:test";
import { capacityOptions, filterRunItems, itemCapacity } from "../web/run-filters.js";

const run = {
  storage: null,
  passes: [{ storage: "128" }, { storage: "256" }],
  items: [
    { item_id: "a", storage: "128", code: "matched", matched: 1 },
    { item_id: "b", storage: "256", pass_label: "128G", code: "pending" },
    { item_id: "c", storage: null, pass_label: "256G", code: "no_link" },
    { item_id: "d", storage: "128", code: "price_high" },
    { item_id: "e", storage: null, pass_label: "新发", code: "unrecognized" },
    { item_id: "f", storage: "256", code: null },
  ],
};

test("容量优先用商品识别值，再参考采集容量，无法确认的单独保留", () => {
  assert.equal(itemCapacity(run.items[1], run), "256");
  assert.equal(itemCapacity(run.items[2], run), "256");
  assert.equal(itemCapacity(run.items[4], run), "");
  assert.equal(itemCapacity(run.items[4], { storage: "512" }), "512");
  assert.equal(itemCapacity({ storage: "1TB" }), "1T");
});

test("容量覆盖历史轮次和本轮商品，去重后按实际大小排序", () => {
  assert.deepEqual(capacityOptions([{ storage: "2T" }, { passes: [{ storage: "512GB" }, { storage: "1T" }] }], run),
    ["128", "256", "512", "1T", "2T"]);
});

test("默认展示全部，容量筛选后计数与商品同步，未知结果归入未命中", () => {
  const all = filterRunItems(run);
  assert.deepEqual(all.counts, { all: 6, matched: 1, pending: 1, rejected: 4 });
  assert.deepEqual(all.capacities, { 128: 2, 256: 3, unknown: 1 });
  const scoped = filterRunItems(run, { storage: "256" });
  assert.deepEqual(scoped.counts, { all: 3, matched: 0, pending: 1, rejected: 2 });
  assert.deepEqual(scoped.items.map(item => item.item_id), ["b", "c", "f"]);
  assert.deepEqual(scoped.reasons, { no_link: 1, other: 1 });
});

test("容量、结果、具体原因可组合，空结果保持筛选而不偷偷切回全部", () => {
  const result = filterRunItems(run, { storage: "256", result: "rejected", code: "no_link" });
  assert.deepEqual(result.items.map(item => item.item_id), ["c"]);
  assert.equal(result.counts.rejected, 2);
  assert.equal(filterRunItems(run, { storage: "256", result: "matched" }).items.length, 0);
  assert.equal(filterRunItems(run, { storage: "512" }).counts.all, 0);
  assert.deepEqual(filterRunItems(run, { storage: "unknown" }).items.map(item => item.item_id), ["e"]);
  assert.equal(filterRunItems({ items: [] }, { storage: "256" }).items.length, 0);
});
