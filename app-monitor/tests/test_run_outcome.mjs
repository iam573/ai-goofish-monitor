import assert from "node:assert/strict";
import test from "node:test";
import { runOutcome } from "../web/run-outcome.js";
import { filterRunItems } from "../web/run-filters.js";

const list = { code: "price_high", price: 4088, quote_price: 3750, threshold_price: 3950,
  quote_grade: "靓机", quote_basis: "candidate" };
const detail = { code: "smart_rejected", price: 3844, quote_price: null, threshold_price: 3750,
  quote_grade: null, quote_basis: "detail", decision: "详情判价不符合：挂牌价高于可计算的回收价上限加议价空间 · 基础报价 ¥3550；扣费后回收参考上限 ¥3550" };

test("用户两条历史超价记录采用同一原因，保留各自阶段和超价金额", () => {
  const a = runOutcome(list), b = runOutcome(detail);
  assert.equal(a.label, "价格偏高");
  assert.equal(b.label, a.label);
  assert.equal(a.stage, "列表筛选");
  assert.equal(b.stage, "详情判价");
  assert.equal(a.excess, 138);
  assert.equal(b.excess, 94);
  assert.equal(b.high, 3550);
  assert.equal(b.low, null);
  assert.equal(b.referenceGrade, null); // The old record did not save this fact.
});

test("同一原因筛选合并新旧列表与详情记录，不改写输入", () => {
  const run = { items: [list, detail, { code: "smart_rejected", decision: "已识别影响报价的条件，但缺少适用扣费，无法确定推送门槛" }] };
  const original = JSON.stringify(run);
  const result = filterRunItems(run, { result: "rejected", code: "price_high" });
  assert.equal(result.items.length, 2);
  assert.deepEqual(result.reasons, { price_high: 2, unpriced_condition: 1 });
  assert.equal(JSON.stringify(run), original);
});

test("详情快照保留未知成色、小花参考和扣费后的上限", () => {
  const result = runOutcome({ code: "price_high", price: 3844, quote_basis: "detail", valuation: {
    grade: null, reference_grade: "小花", base_price: 3750, value_low: null, value_high: 3550,
    threshold_high: 3750, deductions: [{ name: "电池", min: 200, max: 300 }],
  } });
  assert.equal(result.grade, null);
  assert.equal(result.referenceGrade, "小花");
  assert.equal(result.base, 3750);
  assert.equal(result.high, 3550);
  assert.equal(result.low, null);
  assert.equal(result.limit, 3750);
  assert.equal(result.deductions[0].min, 200);
});

test("缺少适用金额时不把基础报价冒充扣费后金额，不倒推旧记录", () => {
  const missing = runOutcome({ code: "unpriced_condition", quote_basis: "detail", valuation: {
    base_price: 3750, value_low: null, value_high: null, threshold_high: null,
  } });
  assert.equal(missing.high, null);
  assert.equal(missing.limit, null);
  assert.equal(missing.label, "缺少扣费依据");
  const old = runOutcome({ code: "smart_rejected", quote_basis: "detail", threshold_price: 3750,
    decision: "挂牌价高于可计算的回收价上限加议价空间" });
  assert.equal(old.high, null); // No current/default negotiation amount may be subtracted.
  assert.equal(old.referenceGrade, null);
  const legacyNoLink = runOutcome({ code: "no_link", quote_price: 3750 });
  assert.equal(legacyNoLink.basis, "candidate");
  assert.equal(legacyNoLink.stage, null);
});

test("已确认使用稳妥门槛，待确认保留候选上限，取链接失败仍属未完成", () => {
  const valuation = { value_low: 3300, value_high: 3550, threshold_low: 3500, threshold_high: 3750 };
  assert.equal(runOutcome({ code: "matched", valuation }).limit, 3500);
  assert.equal(runOutcome({ code: "pending", valuation }).limit, 3750);
  const link = runOutcome({ code: "link_failed", valuation, decision: "详情判价待确认，链接获取失败" });
  assert.equal(link.bucket, "incomplete");
  assert.equal(link.stage, "链接获取");
  assert.equal(link.high, 3550);
});

test("旧泛化拒绝仅在有明确原因时细分，未知原因不猜测", () => {
  for (const [reason, code] of [
    ["详情容量与搜索目标不符", "storage_mismatch"], ["详情机型与搜索目标不符", "other_model"],
    ["不接受成色：外爆", "grade_not_accepted"], ["成色未确定，且没有可用的小花报价，无法计价", "no_quote"],
    ["存在不接受的问题：锁机", "excluded"], ["旧的不明结论", "smart_rejected"],
    ["并非整机或价格不是整机售价", "not_item_or_price"],
  ]) assert.equal(runOutcome({ code: "smart_rejected", decision: reason }).code, code);
});
