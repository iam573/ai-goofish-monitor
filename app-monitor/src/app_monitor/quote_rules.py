"""Compile quoted amounts and explicit applicability; never invent a deduction price."""
from __future__ import annotations

import re
from .quotes import parse_deduction_text, normalize_deduction_text
from .smart import Condition, Facts, Finding, Policy

COLORS = {
    '红色': ['红色'], '黄色': ['黄色'], '蓝色': ['蓝色', '远峰蓝', '海蓝色'],
    '黑色': ['黑色', '深空黑', '石墨色', '曜石黑'], '白色': ['白色', '星光色'],
    '金色': ['金色', '香槟金', '沙漠色', '沙漠金'], '银色': ['银色', '银白色'],
    '紫色': ['紫色', '暗紫色', '深紫色'], '绿色': ['绿色', '苍岭绿', '午夜绿'],
    '粉色': ['粉色', '粉红色'], '橙色': ['橙色', '星宇橙', '宇宙橙'],
}
VARIANTS = {
    '国行': [r'国行', r'大陆版', r'中国大陆版'],
    '港澳版': [r'港澳版?', r'港版', r'香港版', r'澳门版', r'澳版'],
    '美版': [r'美版', r'美国版'], '日版': [r'日版', r'日本版'], '韩版': [r'韩版', r'韩国版'],
    '欧版': [r'欧版', r'欧洲版'], '台版': [r'台版', r'台湾版'], '加版': [r'加版', r'加拿大版'],
    '海外版': [r'海外版', r'外版', r'水货', r'国际版', r'新加坡版', r'新加坡版本'],
}
FLAG_PATTERNS = {
    'screen_original': [r'(?:换|更换)(?:过|了|的是|的)?(?:官方|原装|原厂|拆机)(?:原装|原厂)?屏', r'(?:原装|原厂|拆机)屏(?:幕)?(?:已|有)?更?换过'],
    'screen_popup': [r'(?:屏幕?|显示屏)(?:有|会|出现)?弹窗', r'弹屏', r'屏幕?(?:未知|不明|无法验证)(?:部件)?'],
    'battery_popup': [r'电池(?:有|会|出现)?弹窗', r'弹电', r'电池(?:未知|不明|无法验证)(?:部件)?'],
    'battery_cell_replaced': [r'(?:更换|换)(?:过|了)?(?:全新|新)?(?:原装|原厂|官方|第三方)?(?:的)?电芯',
                              r'(?:全新|新)(?:原装|原厂)?电芯', r'电芯(?:已更换|更换过|换过|换新|新换)'],
    'battery_third_party': [r'(?:第三方|非原装|组装|国产|德赛|品胜|飞毛腿|诺希|倍思)(?:的)?(?:电池|电芯)'],
    'battery_swollen': [r'(?:电池)?鼓包', r'电池(?:有|出现)?(?:鼓起|膨胀)'],
    'face_id_fault': [r'面容(?:损坏|失灵|不可用|坏|故障)', r'无面容', r'face\s*id(?:失灵|损坏)'],
    'rear_camera_replaced': [r'(?:更换|换)(?:过|了)?(?:后置?|后摄)(?:摄像头|镜头|像头)?', r'后(?:置摄像头|摄像头|镜头|摄|像头)(?:更换|换过|换了)'],
    'camera_watermark': [r'(?:摄像头|镜头|像头)(?:有|出现|轻微|小)?水印'],
    'screen_aging': [r'(?:屏幕|内屏|显示屏)(?:有|轻微|出现)?老化'],
    'front_camera_spots': [r'(?:前置?|前摄|自拍)(?:摄像头|镜头|像头)?(?:有|出现)?(?:彩点|黑点|斑点)'],
    'rear_camera_spots': [r'(?:后置?|后摄)(?:摄像头|镜头|像头)?(?:有|出现)?(?:彩点|黑点|斑点)'],
    'front_camera_flare': [r'(?:前置?|前摄|自拍)(?:摄像头|镜头|像头)?(?:有|出现)?蓝光'],
    'rear_camera_flare': [r'(?:后置?|后摄)(?:摄像头|镜头|像头)?(?:有|出现)?蓝光'],
    'true_tone_missing': [r'无原彩', r'原彩(?:缺失|不可用|没有|丢失)'],
    'full_set_missing': [r'单机(?:出|出售)?', r'无配件', r'无(?:包装|全套)', r'不带(?:盒|配件)', r'只有手机'],
}


def extra_findings(text: str, findings: dict, unknowns: list[str]) -> None:
    from .local_rules import mentions, _finding
    from .smart import FACT_LABELS
    for key, patterns in FLAG_PATTERNS.items():
        content = re.sub(r'不(?=鼓包|膨胀|弹窗)', '无', text)
        values = mentions(content, patterns)
        if key == 'battery_cell_replaced':
            # “没换过新电芯” must not become positive through the nested “新电芯”.
            values = [v for v in values if not any(v[2]>=other[2] and v[3]<=other[3]
                      and v[3]-v[2]<other[3]-other[2] for other in values)]
        findings[key] = _finding(values, FACT_LABELS[key], unknowns)
    for key, mapping in [('color', COLORS), ('variant', VARIANTS)]:
        values = []
        for canonical, patterns in mapping.items():
            matches = [m for m in mentions(text, patterns) if m[0] is True]
            if matches:
                values.append((canonical, matches[0][1]))
        # A named national variant takes precedence over generic “海外版”.
        if key == 'variant' and len(values) > 1:
            values = [pair for pair in values if pair[0] != '海外版']
        if key == 'color' and len(values) > 1:
            # Remove short color matches nested inside a longer known color phrase.
            values = [pair for pair in values if not (pair[0] == '红色' and any(v[0] == '粉色' for v in values))]
        if len(values) == 1:
            findings[key] = Finding(value=values[0][0], evidence=values[0][1][:500])
        elif len(values) > 1:
            findings[key] = Finding(evidence='；'.join(pair[1] for pair in values)[:500])
            unknowns.append(f'{FACT_LABELS[key]}出现多个选项，需确认对应售价')
    variant = findings.get('variant', Finding())
    findings['overseas'] = Finding(value=('no' if variant.value == '国行' else 'yes') if variant.value else None,
                                   evidence=variant.evidence)
    for specific, parent in [('screen_original','screen_replaced'), ('screen_popup','screen_replaced'),
                              ('battery_third_party','battery_replaced'), ('battery_popup','battery_replaced'),
                              ('battery_cell_replaced','battery_replaced')]:
        if findings[specific].value == 'yes':
            findings[parent] = findings[specific].model_copy()
    # A known third-party screen is explicitly not an original replacement screen.
    third_screen = _finding(mentions(text, [r'第三方屏', r'组装屏', r'国产屏', r'非原装屏']), '第三方屏幕', [])
    if third_screen.value == 'yes':
        findings['screen_replaced'] = third_screen
        findings['screen_original'] = Finding(value='no', evidence=third_screen.evidence)
    original_battery = _finding(mentions(text, [r'(?:更换|换)(?:过|了)?(?:原装|原厂|官方)电池']), '原装更换电池', [])
    if original_battery.value == 'yes':
        findings['battery_replaced'] = original_battery
        findings['battery_third_party'] = Finding(value='no', evidence=original_battery.evidence)


def _clean(text):
    from .local_rules import clean
    text=clean(normalize_deduction_text(text)).replace('港澳販', '港澳版').replace('面客', '面容')
    for wrong,correct in [('电池飯','电池鼓'),('后除头彩点','后像头彩点'),('后俊头彩点','后像头彩点'),
                          ('示像头彩点','后像头彩点'),('弹电/弹用','弹电/弹屏'),('弹电,/弹屏','弹电/弹屏')]:
        text=text.replace(wrong,correct)
    return text


def _scope(name):
    grades = []
    for prefix, values in [('靓机小花', ['靓机','小花']), ('靓机/小花', ['靓机','小花']),
                           ('大花后等级', ['大花','外爆','内爆可测']), ('后三等级', ['大花','外爆','内爆可测']),
                           ('后二等级', ['外爆','内爆可测'])]:
        if name.startswith(prefix):
            grades, name = values, name[len(prefix):]
            break
    specs = re.match(r'((?:(?:64|128|256|512)(?:gb|g)?|[12]t)(?:/(?:(?:64|128|256|512)(?:gb|g)?|[12]t))*(?:g)?)', name)
    requires = []
    if specs:
        from .quotes import deduction_storages
        requires.append(Condition(field='storage', operator='in', value='|'.join(deduction_storages(specs[0]))))
        name = name[specs.end():]
    return name, grades, requires


def compile_policy(row: dict) -> Policy:
    name, grades, requires = _scope(re.sub(r'\s+', '', _clean(row['name'])))
    for variant in VARIANTS:
        if name.startswith(variant) and name != variant:
            requires.append(Condition(field='variant',operator='eq',value=variant))
            name=name[len(variant):]
            break
    def policy(field, *, operator='eq', value='yes', group=None, covers=(), priority=10, alternatives=(), **kw):
        return Policy(reviewed=True, automatic=True, field=field, operator=operator, value=value,
                      group=group or field, grades=grades, requires=requires, covers=list(covers),
                      priority=priority, alternatives=list(alternatives), **kw)
    if name in {'不分颜色','不区分颜色','无颜色差价','颜色不加减'} or re.fullmatch(r'[()（）、/靓机小花在此基础上多扣\s]+', name) or re.search(r'机/小花在',name):
        return policy('model', operator='contains', value='', action='ignore')
    if re.search(r'后盖(?:破|碎).*当大花收', name):
        return policy('back_glass_broken', action='grade', grade='大花', group='back_glass')
    if row.get('amount_min') is None:
        return Policy()
    threshold = re.fullmatch(r'电池(?:健康度|健康|效率|容量)?(?:低于|小于|不足|<)(\d{2,3})%?(?:的)?', name)
    inclusive = re.fullmatch(r'电池(?:健康度|健康|效率|容量)?(\d{2,3})%?以下', name)
    if threshold or inclusive:
        number = float((threshold or inclusive)[1])
        if 0 <= number <= 100:
            return policy('battery_health', operator='lt' if threshold else 'le', value=f'{number:g}',
                          group='battery_health', tiered=True)
    if name in {'港澳版','港版','香港版','澳门版'}:
        return policy('variant', operator='in', value='港澳版|港版|港澳', group='variant', priority=30)
    variant = next((key for key in VARIANTS if name == key), None)
    if variant:
        return policy('variant', value=variant, group='variant', priority=30)
    if name.startswith('水货'):
        tail = name.removeprefix('水货')
        from .quotes import deduction_storages
        specs = deduction_storages(tail)
        if tail == '三网' or specs and re.fullmatch(r'(?:64|128|256|512|[12]t)(?:g|gb)?', tail):
            p = policy('overseas', group='variant', priority=40 if specs else 10)
            if specs:p.requires.append(Condition(field='storage',operator='in',value='|'.join(specs)))
            return p
    color_values = [color for color in COLORS if color in name]
    if name == '黄/红色':
        color_values = ['黄色','红色']
        name = '黄色/红色'
    if color_values and not re.sub(r'[/、,]|'+'|'.join(map(re.escape,color_values)), '', name):
        return policy('color', operator='in', value='|'.join(color_values), group='color')
    known = {
        '弹屏': ('screen_replaced','screen_part',['screen_replaced'],10),
        '换屏': ('screen_replaced','screen_part',['screen_replaced'],10),
        '换过屏': ('screen_replaced','screen_part',['screen_replaced'],10),
        '更换屏幕': ('screen_replaced','screen_part',['screen_replaced'],10),
        '弹正屏': ('screen_original','screen_part',['screen_replaced'],30),
        '弹电': ('battery_replaced','battery_part',['battery_replaced'],10),
        '换电池': ('battery_replaced','battery_part',['battery_replaced'],10),
        '换过电池': ('battery_replaced','battery_part',['battery_replaced'],10),
        '更换电池': ('battery_replaced','battery_part',['battery_replaced'],10),
        '换电芯': ('battery_cell_replaced','battery_part',['battery_replaced'],30),
        '更换电芯': ('battery_cell_replaced','battery_part',['battery_replaced'],30),
        '第三方': ('battery_third_party','battery_part',['battery_replaced'],30),
        '第三方电池': ('battery_third_party','battery_part',['battery_replaced'],30),
        '电池鼓': ('battery_swollen','battery_swollen',[],10),
        '电池鼓包': ('battery_swollen','battery_swollen',[],10),
        '面容': ('face_id_fault','face_id_fault',[],10), '面容坏': ('face_id_fault','face_id_fault',[],10),
        '原彩': ('true_tone_missing','true_tone_missing',[],10),
        '轻微老化': ('screen_aging','screen_aging',[],10), '老化': ('screen_aging','screen_aging',[],10),
        '像头水印': ('camera_watermark','camera_watermark',[],10), '头水印': ('camera_watermark','camera_watermark',[],10),
        '像头小水印': ('camera_watermark','camera_watermark',[],10), '头小水印': ('camera_watermark','camera_watermark',[],10),
        '前像头小水印': ('camera_watermark','camera_watermark',[],10),
        '前像头彩点': ('front_camera_spots','front_camera_spots',[],10), '前彩': ('front_camera_spots','front_camera_spots',[],10),
        '后像头彩点': ('rear_camera_spots','rear_camera_spots',[],10), '后彩': ('rear_camera_spots','rear_camera_spots',[],10),
        '前蓝光': ('front_camera_flare','front_camera_flare',[],10), '后蓝光': ('rear_camera_flare','rear_camera_flare',[],10),
        '后换像头': ('rear_camera_replaced','rear_camera_replaced',[],10), '后换头': ('rear_camera_replaced','rear_camera_replaced',[],10),
        '屏大碎': ('screen_broken','screen',[],10), '水印': ('screen_watermark','screen',[],10),
        '屏幕水印': ('screen_watermark','screen',[],10), '烧屏': ('screen_watermark','screen',[],10),
        '进水': ('water_damage','repair',['water_damage'],10), '拆修': ('repaired','repair',['repaired'],10),
        '后盖破': ('back_glass_broken','back_glass',['back_glass_broken'],10),
        '无全套': ('full_set_missing','full_set',[],10),
    }
    if name in known:
        field, group, covers, priority = known[name]
        return policy(field, group=group, covers=covers, priority=priority)
    if name == '弹电/弹屏':
        return policy('screen_replaced', alternatives=[Condition(field='battery_replaced',operator='eq',value='yes')],
                      group='replacement_bundle', covers=['screen_replaced','battery_replaced'])
    return Policy()


def compile_rows(rows: list[dict]) -> list[dict]:
    """Reinterpret existing OCR rows in memory; keep original manual-rule identities."""
    result = []
    for row in rows:
        identity = [row['name'],row['kind'],bool(row['open_ended']),row['column_index'],row['position']]
        text = _clean(row['text'])
        # Narrow corrections to chart forms: these trailing numbers are quoted amounts.
        text = re.sub(r'(弹电[/、,一]*[/]?弹屏)[,，•一.]?(\d+)$',r'\1-\2',text)
        text = re.sub(r'(?<=低于)(\d{2})9(?=[-扣按])',r'\1%',text)
        name, lo, hi, more, kind = parse_deduction_text(text)
        entry = {**row,'text':text,'name':name,'_rule_identity':identity}
        if lo is not None:entry.update(amount_min=lo,amount_max=hi,open_ended=more,kind=kind)
        entry['policy'] = compile_policy(entry).model_dump()
        result.append(entry)
    for row in result:
        if row['name'].endswith('弹电/弹屏•') and any(other is not row and other['name'].startswith('大花后等级弹电/弹屏') and other.get('amount_min') is not None for other in result):
            row['policy']=Policy(reviewed=True,action='ignore').model_dump()
    # “高保充新无全套” belongs to an extra chart grade not imported as a base quote.
    for i,row in enumerate(result[:-1]):
        following=result[i+1]
        if row['text'] in {'高保充新','高保充新无'} and following['name'] in {'无全套','全套'} and row['column_index']==following['column_index']:
            row['policy']=Policy(reviewed=True,action='ignore').model_dump()
            following['policy']=Policy(reviewed=True,action='ignore').model_dump()
    # Repair split battery tier notes, retaining each tier's amount and full source.
    for column in {r['column_index'] for r in result}:
        column_rows = [r for r in result if r['column_index'] == column]
        for i, row in enumerate(column_rows):
            if not re.search(r'电池低于\d+%.*[（(]', row['text']):continue
            parts = [row]; j=i+1
            while j<len(column_rows) and len(parts)<4 and re.match(r'(?:电池低于)?\d{2,3}%', column_rows[j]['text']):
                parts.append(column_rows[j]);j+=1
                if re.search(r'[）)]',parts[-1]['text']):break
            joined=''.join(p['text'] for p in parts)
            prefix='靓机小花' if joined.startswith('靓机小花') else ''
            tiers=re.findall(r'(?:电池低于)?(\d{2,3})%[-扣](\d+)',joined)
            if not tiers:continue
            for part in parts:part['policy']=Policy(reviewed=True,action='ignore').model_dump()
            for limit, amount in tiers:
                text=f'{prefix}电池低于{limit}%-{amount}'
                name, lo, hi, more, kind = parse_deduction_text(text)
                entry={**row,'name':name,'text':joined,'amount_min':lo,'amount_max':hi,'open_ended':more,'kind':kind,
                       '_rule_identity':[*row['_rule_identity'],limit],
                       '_parent_rule_identities':[p['_rule_identity'] for p in parts]}
                entry['policy']=compile_policy(entry).model_dump();result.append(entry)
    # References like “低于70%按弹电” borrow only this model/category's actual quoted fee.
    for row in result:
        if re.search(r'电池低于\d{2}%按(?:弹电|3单电)',row['text']):
            target=next((r for r in result if r['name']=='弹电' and r.get('amount_min') is not None),None)
            if target:
                limit=re.search(r'低于(\d{2})%',row['text'])[1]
                row.update(name=f'电池低于{limit}%',amount_min=target['amount_min'],amount_max=target['amount_max'],open_ended=target['open_ended'])
                row['policy']=Policy(reviewed=True,automatic=True,field='battery_health',operator='lt',value=limit,
                                     group='battery_part',priority=40,covers=['battery_replaced'],tiered=True).model_dump()
    return result


def resolve_automatic_group(matches, facts, policies, grade, unknown):
    """Resolve alternatives for one issue; independent groups remain cumulative."""
    if not matches or not all(p.automatic for _,p in matches):return matches
    highest=max(p.priority for _,p in matches)
    matches=[pair for pair in matches if pair[1].priority==highest]
    # A grade/variant-qualified rule is more specific than a generic alias.
    specificity=max(len(p.requires)+bool(p.grades) for _,p in matches)
    matches=[pair for pair in matches if len(pair[1].requires)+bool(pair[1].grades)==specificity]
    first, policy=matches[0]
    candidates=[row for row,_ in matches]
    uncertain_type=None
    if policy.group == 'replacement_bundle':
        replaced=[key for key in ('screen_replaced','battery_replaced') if facts.findings.get(key,Finding()).value=='yes']
        if len(replaced)>1:
            unknown.append('屏幕与电池同时更换，组合备注叠扣方式待确认，保留扣费区间')
            candidates.append({**first,'amount_max':float(first.get('amount_max') or first['amount_min'])*len(replaced)})
        popup_known=any(facts.findings.get(key,Finding()).value=='yes' for key in ('screen_popup','battery_popup'))
        if not popup_known:
            unknown.append('更换部件的弹窗情况未明确，按报价扣费区间处理')
            ranges=[];sources=[]
            for key in replaced:
                group='screen_part' if key=='screen_replaced' else 'battery_part'
                possible=[]
                for row in policies:
                    p=Policy.model_validate(row['policy'])
                    if not p.automatic or p.group!=group or row.get('amount_min') is None:continue
                    value=p.applies(facts,grade)
                    if value is True or value is None and p.field in {'screen_original','battery_third_party'}:
                        possible.append(row)
                if possible:
                    ranges.append((min(float(r['amount_min']) for r in possible),max(float(r.get('amount_max') or r['amount_min']) for r in possible)))
                    sources.extend(r['text'] for r in possible)
            if ranges and len(ranges)==len(replaced):
                candidates.append({**first,'amount_min':sum(r[0] for r in ranges),'amount_max':sum(r[1] for r in ranges),
                                   'text':'；'.join(sources)})
    subtype={'screen_part':'screen_original','battery_part':'battery_third_party'}.get(policy.group)
    if subtype and policy.field in {'screen_replaced','battery_replaced'}:
        finding=facts.findings.get(subtype)
        if not finding or finding.value is None:
            for row in policies:
                alternative=Policy.model_validate(row['policy'])
                if not alternative.automatic or alternative.group!=policy.group or alternative.field!=subtype:continue
                hypothetical=facts.model_copy(deep=True)
                hypothetical.findings[subtype]=Finding(value='yes')
                if alternative.applies(hypothetical,grade) and row.get('amount_min') is not None:
                    candidates.append(row);uncertain_type=policy.field
    amounts=[(float(row['amount_min']),float(row.get('amount_max') or row['amount_min']))
             for row in candidates if row.get('amount_min') is not None]
    if not amounts:return matches
    low=min(pair[0] for pair in amounts);high=max(pair[1] for pair in amounts)
    selected={**first,'amount_min':low,'amount_max':high,'open_ended':any(row.get('open_ended') for row in candidates)}
    if policy.group == 'replacement_bundle':
        selected['text']='；'.join(dict.fromkeys(row['text'] for row in candidates))
    if uncertain_type:
        unknown.append(f'{"换屏" if uncertain_type=="screen_replaced" else "换电池"}部件类型未明确，按报价扣费区间处理')
        selected['text']='；'.join(dict.fromkeys(row['text'] for row in candidates))
    elif len({(r['name'],r.get('amount_min'),r.get('amount_max')) for r in candidates})>1:
        unknown.append(f'同类扣费存在多个适用金额：{policy.group}，按区间处理')
    return [(selected,policy)]
