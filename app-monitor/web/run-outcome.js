/** Interpret saved run evidence; never consult today's quotes or settings. */
export const codeLabels = {
  matched: "命中", pending: "待确认", price_high: "价格偏高", region: "地区不符",
  other_model: "机型不符", storage_mismatch: "容量不符", excluded: "命中排除规则",
  grade_not_accepted: "不接受成色", no_quote: "缺少报价", unpriced_condition: "旧策略未计价",
  not_whole: "非整机", not_full_price: "非整机售价", not_item_or_price: "商品或售价不符", unrecognized: "无法识别",
  too_cheap: "价格异常偏低", smart_rejected: "其他不符合", other: "其他不符合",
  no_link: "核验未完成", verification_incomplete: "核验未完成",
  detail_failed: "详情读取失败", link_failed: "链接获取失败", analysis_failed: "判价未完成",
};
export const codeOrder = Object.keys(codeLabels);
const incomplete = new Set(["no_link", "verification_incomplete", "detail_failed", "link_failed", "analysis_failed"]);
const numeric = value => value !== null && value !== undefined && value !== "" && Number.isFinite(Number(value)) ? Number(value) : null;

function legacyRejection(reason) {
  const rules = [
    [/挂牌价.*高于.*(?:上限|议价空间)/, "price_high"],
    [/详情机型与搜索目标不符|标题明确为.*不一致|商品机型为.*不一致/, "other_model"],
    [/详情容量与搜索目标不符|标题容量.*不一致|商品容量为.*不符/, "storage_mismatch"],
    [/不接受成色|降档后成色不在接受范围/, "grade_not_accepted"],
    [/缺少适用扣费|无可确定的适用扣费/, "unpriced_condition"],
    [/没有可用.*报价|没有.*小花报价|没有可用报价/, "no_quote"],
    [/存在不接受的问题|标题明确存在不收问题/, "excluded"],
    [/并非整机或价格不是整机售价/, "not_item_or_price"],
  ];
  return rules.find(([pattern]) => pattern.test(reason))?.[1] || "smart_rejected";
}

export function runOutcome(item) {
  const value = item.assessment_result || item.valuation || null;
  const savedReason = String(item.decision || value?.reason || "");
  let code = item.code || "other";
  if (code === "smart_rejected") code = value?.rejection_code || legacyRejection(savedReason);
  const bucket = item.matched || code === "matched" ? "matched" : code === "pending" ? "pending"
    : incomplete.has(code) ? "incomplete" : "rejected";
  const basis = value || item.quote_basis === "detail" ? "detail" : item.quote_basis || (code === "no_link" ? "candidate" : "fixed");
  const stage = code === "detail_failed" ? "详情读取" : code === "link_failed" ? "链接获取"
    : code === "no_link" && !value ? null : basis === "candidate" ? "列表筛选"
      : basis === "detail" || code === "analysis_failed" ? "详情判价" : "规则筛选";
  // Older early rejections only stored these explicitly named amounts in text.
  // Missing amounts and reference grades remain unknown, never back-calculated.
  const savedUpper = savedReason.match(/回收参考上限\s*[¥￥]\s*([\d,]+(?:\.\d+)?)/);
  const savedBase = savedReason.match(/基础报价\s*[¥￥]\s*([\d,]+(?:\.\d+)?)/);
  const low = value && Object.hasOwn(value, "value_low") ? numeric(value.value_low) : numeric(item.quote_price);
  const high = value ? numeric(value.value_high) : savedUpper ? Number(savedUpper[1].replaceAll(",", "")) : low;
  const limit = bucket === "matched" ? numeric(value?.threshold_low) ?? numeric(item.threshold_price) ?? numeric(value?.threshold_high)
    : numeric(value?.threshold_high) ?? numeric(value?.threshold_low) ?? numeric(item.threshold_price);
  const price = numeric(item.price);
  const excess = code === "price_high" && limit !== null && price !== null && price > limit ? price - limit : null;
  const reason = savedReason.replace(/^详情判价不符合：/, "")
    .replace(/^列表初筛符合，但未取得商品链接：/, "核验未完成：")
    .replace(/ · 基础报价[\s\S]*$/, "");
  return {
    code, label: codeLabels[code] || "其他不符合", bucket, basis, stage, reason,
    low, high, limit, excess, grade: value?.grade || item.quote_grade || null,
    referenceGrade: value?.reference_grade || null,
    base: numeric(value?.base_price) ?? (savedBase ? Number(savedBase[1].replaceAll(",", "")) : null),
    deductions: Array.isArray(value?.deductions) ? value.deductions : null,
    unpriced: Boolean(value?.unpriced_conditions?.length || value?.unpriced_notes?.length),
  };
}
