"""Extract stated evidence; pricing treats unmentioned conditions as normal by policy."""
from __future__ import annotations

import re
import unicodedata
from typing import Any

from .item_identity import has_model_evidence
from .keyword_rules import condition_patterns
from .matching import explicit_iphone_models, extract_storage, recognize_apple_product
from .smart import BATTERY_PATTERN, Facts, Finding, Policy

GRADE_KEYWORDS = {
    "靓机": ["99新", "九九新", "几乎全新", "近全新", "充新", "无明显使用痕迹", "无明显划痕", "无划痕", "无磕碰"],
    "小花": ["小花", "轻微划痕", "细微划痕", "细小划痕", "轻微使用痕迹", "轻微磕碰"],
    "大花": ["大花", "明显划痕", "划痕较多", "磕碰", "掉漆", "有划痕"],
    "外爆": ["外屏碎", "外屏破", "外爆"],
    "内爆可测": ["内屏碎", "内屏破", "内爆", "漏液"],
}
FAULTS = {key: condition_patterns(key) for key in ('screen_watermark', 'screen_broken', 'function_fault', 'locked', 'repaired', 'screen_replaced', 'battery_replaced', 'water_damage', 'back_glass_broken')}
NEGATION = re.compile(r"(?:无|没有|没|未|不曾|从未|不是|并非|不带|不含|不支持|不做|拒绝)(?:任何|明显|一点|轻微|大|小|过|的|一处|进行|发生|出现){0,3}$")
UNCERTAIN = re.compile(r"可能|疑似|不确定|好像|貌似|不清楚|不保证|不排除|无法确认|不能保证|不能排除|看不出|看不出来")
COLORS = ("薰衣草紫", "星宇橙", "宇宙橙", "沙漠色", "雾青色", "深蓝色", "白色", "黑色", "银色", "金色", "蓝色", "紫色", "粉色", "绿色", "橙色")


def clean(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))).casefold()


def _clause(text: str, start: int, end: int) -> tuple[str, str]:
    left = re.split(r"[，,。；;！!？?\n]|但是|不过|但", text[:start])[-1]
    right = re.split(r"[，,。；;！!？?\n]|但是|不过|但", text[end:])[0]
    return left[-24:], right[:24]


def mentions(text: str, patterns: list[str], *, screen_watermark=False) -> list[tuple[bool | None, str, int, int]]:
    values = []
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.I):
            left, right = _clause(text, match.start(), match.end())
            context = left + match.group() + right
            if (screen_watermark or pattern in FAULTS["screen_watermark"]) and re.search(r"摄像头|镜头|像头", context) and not re.search(r"屏幕|内屏|外屏|显示", context):
                continue
            if (screen_watermark or pattern in FAULTS["screen_watermark"]) and re.search(r"图片|照片|拍摄|图上|商品图|加水印", context) and not re.search(r"屏幕|内屏|外屏|显示", context):
                continue
            uncertain = bool(UNCERTAIN.search(context))
            denied = bool(NEGATION.search(left) or re.match(r"(?:都)?(?:没有|不存在|未出现|未发生)", right))
            # “不保证无水印” must stay unknown, never become “无水印”.
            value = None if uncertain else not denied
            evidence = (left + match.group() + right).strip()
            values.append((value, evidence, match.start(), match.end()))
    return values


def _finding(values, label: str, unknowns: list[str]) -> Finding:
    yes = [v for v in values if v[0] is True]
    no = [v for v in values if v[0] is False]
    uncertain = [v for v in values if v[0] is None]
    if yes and no:
        unknowns.append(f"{label}描述互相矛盾，按明确异常优先")
        return Finding(value="yes", evidence=yes[0][1][:500])
    if uncertain:
        unknowns.append(f"{label}描述不明确或互相矛盾")
        return Finding(value=None, evidence="；".join(v[1] for v in values)[:500])
    selected = yes or no
    return Finding(value="yes" if yes else "no" if no else None,
                   evidence=selected[0][1][:500] if selected else "")


PARTS = (r"(?:外屏玻璃|外屏|内屏|显示屏|屏幕|屏|总成|后盖玻璃|后玻璃|后盖|后壳|外壳|中框|主板|尾插|听筒|扬声器|喇叭|"
         r"前置摄像头|后置摄像头|摄像头|像头|镜头|相机|后摄|前摄|面容|排线|按键|马达|配件)")
PART_QUALIFIER = r"(?:过|了|的|个|一个|一次|新的?|全新|原装|原厂|官方|正品|拆机|苹果|售后|国产|第三方|组装|华星|天马|高刷|高清|柔性|硬屏|oled|品牌|高容量?|超高容的?)*"
GENUINE = re.compile(r"原装|原厂|官方|正品|官修|苹果售后|直营|授权|拆机|原拆")
NOT_GENUINE = re.compile(r"非原装|不是原装|非原厂|非正品|接近原装|仿原装|类原装|第三方|三方|国产|组装|副厂|高仿|华星|天马|后压")
OTHER_PARTS_ORIGINAL = re.compile(r"(?:其他|其它|其余|别的|剩下的?|全|纯)(?:都|全|全部|均)?(?:是)?(?:原装|原厂|正品|原)")
REPLACED_PART_PATTERNS = (
    # 换过国产屏 / 更换了电池和后盖 / 修过屏幕
    rf"(?:更换|换|修)(?:过|了|好)?(?P<q>{PART_QUALIFIER}(?:(?:电池|电芯)[、和及与跟+＋/]{PART_QUALIFIER})?){PARTS}",
    # 后盖换过 / 屏幕已更换 / 电池和屏幕都修过 / 屏幕没换过
    rf"{PARTS}(?P<mid>(?:是|有|已经?|被|都|也|均|没有?|未|无|从未|不曾|原装)*)(?:更换|换|修)(?:过|了|新|掉)",
    r"压(?:过|了)?(?:后盖|盖|屏|外屏)",
    rf"{PARTS}(?:是|为|用的)?(?:国产|第三方|组装|副厂|高仿|华星|天马)",
    rf"(?:不是|非|并非)(?:原装|原厂|正品)(?:的)?{PARTS}",
    rf"{PARTS}[^，,。；;！!？?\n]{{0,8}}?(?:未知部件|非原装部件|无法验证)",
)
GENERIC_REPAIRS = (r"拆修过?", r"维修过?", r"修过", r"翻新", r"扩容", r"爆改", r"搬(?:板|ic)", r"飞线",
                   r"(?:主板|基带)(?:有)?(?:问题|故障|维修|修过)")
_SENTENCE = re.compile(r"[，,。；;！!？?\n]")
_FULL_STOP = re.compile(r"[。；;！!？?\n]")
_ADVICE = re.compile(r"可以|建议|需要|想|准备|打算")


def replaced_parts(text: str, findings: dict | None = None, unknowns: list[str] | None = None) -> Finding:
    """Any non-battery replacement or repair counts unless the part is stated to be genuine."""
    values = []
    for pattern in REPLACED_PART_PATTERNS:
        for match in re.finditer(pattern, text, re.I):
            if re.match(r"幕?(?:膜|保护|贴)", text[match.end():]):
                continue  # 换过屏幕膜 is not a screen replacement.
            left, right = _clause(text, match.start(), match.end())
            context = left + match.group() + right
            if _ADVICE.search(left):
                continue  # Advice such as “可以换个国产屏” describes no actual repair.
            inner = (match.groupdict().get("q") or "") + (match.groupdict().get("mid") or "")
            denied_popup = re.search(r"(?:无|没有?|不)(?:提示|显示|弹出?|有)?(?:未知|非原装部件)", match.group())
            if re.search(r"没|未|无|不曾", inner) or NEGATION.search(left) or denied_popup:
                values.append((False, context, match.start(), match.end()))
                continue
            if UNCERTAIN.search(context):
                values.append((None, context, match.start(), match.end()))
                continue
            before = OTHER_PARTS_ORIGINAL.sub("", left)
            after = OTHER_PARTS_ORIGINAL.sub("", match.group() + right)[:len(match.group()) + 8]
            genuine_before = re.search(r"(?:原装|原厂|官方|正品|官修|苹果|直营店?|授权店?)(?:售后|维修|店|的)?$", before)
            genuine = bool(GENUINE.search(after) or genuine_before) and not NOT_GENUINE.search(before[-6:] + after)
            values.append((not genuine, context, match.start(), match.end()))
    for value, evidence, start, end in mentions(text, list(GENERIC_REPAIRS)):
        if any(start < other_end and end > other_start for _, _, other_start, other_end in values):
            continue
        sentence_start = max([m.end() for m in _SENTENCE.finditer(text, 0, start)], default=0)
        whole_start = max([m.end() for m in _FULL_STOP.finditer(text, 0, start)], default=0)
        sentence = text[whole_start:(_FULL_STOP.search(text, end) or re.search(r"$", text)).start()]
        if re.match(r"拆修|维修|修过", text[start:end]) and re.search(r"电池|电芯", sentence) \
                and not re.search(PARTS, re.sub(r"(?:电池|电芯)", "", sentence)):
            continue  # “没有因为其他问题拆修过，除了换电池” is a battery repair.
        prefix = text[sentence_start:start]
        if value is True and (re.search(r"(?:非|不是|并非)[^，,。；;！!？?\n]{0,6}$", prefix)
                              or re.search(r"(?:无|没有?|未|从未|不曾)(?:任何|拆机|拆|改装|过|的)*$", prefix)):
            value = False
        values.append((value, evidence, start, end))
    # Screen keywords (国产屏、弹屏…) also come from the editable deduction vocabulary.
    findings = findings or {}
    actual = lambda finding: not re.search(r"膜|保护", finding.evidence) and not _ADVICE.search(finding.evidence)
    screen_original = findings.get("screen_original", Finding())
    for key in ("screen_popup", "screen_replaced"):
        finding = findings.get(key)
        if (finding and finding.value == "yes" and screen_original.value != "yes" and actual(finding)
                and not any(v[0] is not None for v in values)):
            values.append((True, finding.evidence, 0, 0))
    if screen_original.value == "no" and actual(screen_original):
        values.append((True, screen_original.evidence, 0, 0))
    return _finding(values, "电池以外的配件更换", unknowns if unknowns is not None else [])


def blocked_title_condition(title: str, blocked: list[str], keywords=None) -> tuple[str, str] | None:
    """Skip only explicit, configured faults; negation/uncertainty still use detail review."""
    text = clean(title)
    for key in blocked:
        if key == "part_replaced":
            finding = replaced_parts(text)
            if finding.value == "yes":
                return key, finding.evidence
            continue
        finding = _finding(mentions(text, condition_patterns(key, keywords), screen_watermark=key == "screen_watermark"), key, [])
        if finding.value == "yes":
            return key, finding.evidence
    return None


def extract_local(item: dict, config, quotes: list[dict], rules) -> Facts:
    text = clean(item.get("description", "") + "\n" + "\n".join(item.get("detail_parameters", [])))
    title = clean(item["title"])
    all_text = title + "\n" + text
    unknowns: list[str] = []
    findings = {}
    labels = {"screen_watermark":"屏幕水印", "screen_broken":"屏幕破损", "function_fault":"功能", "locked":"锁机",
              "repaired":"拆修", "screen_replaced":"换屏", "battery_replaced":"换电池", "water_damage":"进水", "back_glass_broken":"后盖破损"}
    for key in FAULTS:
        values = mentions(all_text, condition_patterns(key, config.condition_keywords), screen_watermark=key == "screen_watermark")
        if key == "repaired":
            values += [(False, m.group(), m.start(), m.end()) for m in re.finditer(r"无拆无修|无拆无换|未拆未修|从未拆修", all_text)]
        if key == "function_fault":
            values += [(False, m.group(), m.start(), m.end()) for m in re.finditer(r"功能(?:一切|全部|均|都)?正常|功能全正常|一切正常|正常使用", all_text)]
        if key == "locked":
            values += [(False, m.group(), m.start(), m.end()) for m in re.finditer(r"无锁|已退(?:出)?(?:账号|账户|id)|账号已退|账户已退|id已退", all_text)]
        findings[key] = _finding(values, labels[key], unknowns)

    battery_values = []
    for match in BATTERY_PATTERN.finditer(all_text):
        left, right = _clause(all_text, match.start(), match.end())
        if NEGATION.search(left) or UNCERTAIN.search(left + match.group()):
            unknowns.append("电池健康度描述不明确")
            continue
        if re.match(r"\s*(?:mah|毫安|次|循环|g|gb|元)", right, re.I):
            continue
        battery_values.append((float(match.group(1)), match.group()))
    unique_battery = {value for value, _ in battery_values}
    if len(unique_battery) > 1:
        unknowns.append("多个电池健康度互相矛盾")
    findings["battery_health"] = Finding(value=f"{battery_values[0][0]:g}" if len(unique_battery) == 1 else None,
                                         evidence=battery_values[0][1] if len(unique_battery) == 1 else "")

    # Description identity takes priority over the listing summary; contradictions remain explicit.
    for key in ("model", "storage"):
        findings[key] = Finding()
    explicit = explicit_iphone_models(text)
    explicit_source = text
    if not explicit:
        explicit = explicit_iphone_models(title)
        explicit_source = title
    if len(explicit) == 1:
        findings["model"] = Finding(value=explicit[0], evidence=explicit_source[:500])
    products = [dict(q) for q in quotes]
    for content in (text, title):
        identity = recognize_apple_product(content, products)
        if identity and has_model_evidence(identity[0], content):
            if not findings["model"].value:
                findings["model"] = Finding(value=identity[0], evidence=content[:500])
            findings["storage"] = Finding(value=identity[1], evidence=content[:500])
            break
    if not findings["model"].value:
        for content in (text, title):
            candidates = list(dict.fromkeys(q["model"] for q in quotes if has_model_evidence(q["model"], content)))
            if candidates:
                findings["model"] = Finding(value=max(candidates,key=len), evidence=content[:500])
                break
    if not findings["storage"].value:
        capacity = extract_storage(text) or extract_storage(title)
        if capacity:
            findings["storage"] = Finding(value=capacity, evidence=(text if extract_storage(text) else title)[:500])

    non_whole = mentions(all_text, [r"求购", r"高价回收", r"专业回收", r"大量收购", r"机模", r"模型机", r"仅(?:卖|售|出)?(?:配件|手机壳|屏幕|主板|包装盒)", r"只(?:卖|售|出)(?:配件|手机壳|屏幕|主板|包装盒)", r"租机", r"出租", r"租赁"])
    false_price = mentions(all_text, [r"(?:首付|定金|订金|月供|补差价|补差款|换购价|以旧换新价|起售价|到手价以.*为准)"])
    custom = [v for word in rules.excluded for v in mentions(all_text,[re.escape(clean(word))]) if v[0] is not False]
    if custom:
        unknowns.append(f"命中需核对的排除词：{custom[0][1][:80]}")
    findings["whole_phone"] = Finding(value="no" if any(v[0] is True for v in non_whole) else "yes" if findings['model'].value else None,
                                     evidence=next((v[1] for v in non_whole if v[0] is True),title)[:500])
    findings["full_price"] = Finding(value="no" if any(v[0] is True for v in false_price) else "yes",
                                    evidence=next((v[1] for v in false_price if v[0] is True),f"页面挂牌价：¥{float(item['price']):g}")[:500])
    from .quote_rules import extra_findings
    extra_findings(all_text, findings, unknowns, config.condition_keywords)
    findings["part_replaced"] = replaced_parts(
        all_text, findings, unknowns if "part_replaced" in config.blocked_conditions else None)
    findings["warranty"] = Finding()
    matches = []
    for grade, words in config.grade_keywords.items():
        for word in words:
            for value, evidence, start, end in mentions(all_text, [re.escape(clean(word))]):
                if value is True:
                    matches.append((grade,evidence,start,end))
                elif value is None:
                    unknowns.append("成色描述存在不确定表述")
    # A specific phrase (“轻微磕碰”) overrides a nested generic word (“磕碰”).
    matches = [m for m in matches if not any(m[2]>=other[2] and m[3]<=other[3] and m[3]-m[2]<other[3]-other[2] for other in matches)]
    rank = {grade:index for index,grade in enumerate(GRADE_KEYWORDS)}
    chosen = max(matches,key=lambda m:rank[m[0]]) if matches else None
    return Facts(grade=chosen[0] if chosen else None, grade_evidence=chosen[1][:500] if chosen else "",
                 findings=findings, uncertainties=list(dict.fromkeys(unknowns)))


def automatic_policy(row: dict) -> Policy:
    from .quote_rules import compile_policy
    return compile_policy(row)


def merge_review(local: Facts, review: Facts) -> Facts:
    """AI can reduce a local opportunity, but cannot silently erase known local risks."""
    merged = local.model_copy(deep=True)
    # An optional model must not override the user's missing-information policy.
    aliases = {"battery_health": "电池|健康度", "repaired": "拆修|维修", "screen_watermark": "水印|烧屏",
               "screen_broken": "碎屏|屏幕破损", "function_fault": "功能", "locked": "锁机|激活锁",
               "screen_replaced": "换屏", "battery_replaced": "换电池", "water_damage": "进水"}
    for message in review.uncertainties:
        missing_only = bool(re.search(r"未(?:写|提|说明|描述|提供|标明|明确)|没有(?:写|说明|提供)|缺少|未知", message))
        fields = [key for key, pattern in aliases.items() if re.search(pattern, message)]
        absent = lambda key: all(not f or f.value is None and not f.evidence
                                 for f in (local.findings.get(key), review.findings.get(key)))
        if missing_only and fields and all(absent(key) for key in fields) and not re.search(r"矛盾|冲突|疑似|可能|异常|故障", message):
            continue
        merged.uncertainties.append(message)
    if review.grade is None:
        merged.uncertainties.append("AI未能确认本地成色判断")
    elif merged.grade is None or list(GRADE_KEYWORDS).index(review.grade) > list(GRADE_KEYWORDS).index(merged.grade):
        merged.grade, merged.grade_evidence = review.grade, review.grade_evidence
    for key, finding in review.findings.items():
        if finding.value is None:
            continue
        previous = merged.findings.get(key)
        if previous and previous.value is not None and previous.value != finding.value:
            if key == "battery_health":
                if float(previous.value) > float(finding.value):
                    merged.findings[key] = finding
            elif key in FAULTS or key == "part_replaced":
                merged.findings[key] = finding if finding.value == "yes" else previous
                merged.uncertainties.append(f"本地与AI对{key}的判断存在分歧")
            else:
                merged.uncertainties.append(f"本地与AI对{key}的判断存在分歧")
        else:
            merged.findings[key] = finding
    merged.uncertainties=list(dict.fromkeys(merged.uncertainties))
    return merged


def note_might_apply(row: dict, item: dict, facts: Facts) -> bool:
    """Do not penalize absent conditions; surface explicitly mentioned unsupported quote notes."""
    text = clean("\n".join([item.get("title", ""), item.get("description", ""),
                            *item.get("detail_parameters", [])]))
    name = clean(row["name"])
    below = re.search(r"电池(?:健康度|健康|效率|容量)?(?:低于|小于|不足|<)\s*(\d{2,3})", name)
    at_most = re.search(r"电池(?:健康度|健康|效率|容量)?\s*(\d{2,3})\s*%?以下", name)
    if below or at_most:
        battery = facts.findings.get("battery_health")
        if not battery or battery.value is None:
            return bool(battery and battery.evidence)
        threshold = float((below or at_most).group(1))
        health = float(battery.value)
        return health < threshold if below else health <= threshold
    if re.search(r"电池(?:鼓|膨胀)|鼓包", name):
        # Scope “不” to this fault; globally treating it as negation would hide “不开机”.
        swelling_text = re.sub(r"不(?=鼓包|膨胀)", "无", text)
        return any(value is not False for value, *_ in mentions(
            swelling_text, [r"鼓包", r"膨胀", r"电池(?:有|出现|已经)?鼓(?:起)?"]))
    for key, patterns in FAULTS.items():
        finding = facts.findings.get(key)
        if finding and (finding.value == "yes" or finding.value is None and finding.evidence):
            if any(re.search(pattern, name, re.I) for pattern in patterns):
                return True
    for key in ("color", "variant"):
        finding = facts.findings.get(key)
        if finding and finding.value and clean(finding.value) in name:
            return True
    tokens = [token for token in re.split(r"[\d\W_]+",name) if len(token)>=2]
    return any(token in text for token in tokens)
