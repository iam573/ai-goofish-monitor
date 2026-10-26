import pytest
from app_monitor.local_rules import extract_local
from app_monitor.quote_rules import compile_rows
from app_monitor.quotes import parse_deduction_text, deduction_storages, latest_entries, _save_deductions, QuoteDeduction
from app_monitor.rules import Rules
from app_monitor.smart import SmartConfig, price_facts, policies_for
from conftest import add_quotes


def notes(*texts):
    rows=[]
    for i,value in enumerate(texts):
        column,text=value if isinstance(value,tuple) else (0,value)
        name,lo,hi,more,kind=parse_deduction_text(text)
        rows.append(dict(id=i+1,column_index=column,position=i,text=text,name=name,
                         amount_min=lo,amount_max=hi,open_ended=more,kind=kind))
    return compile_rows(rows)


def assess(description, rows, price=2000, storage='256', rules=None):
    item=dict(title=f'iPhone 17 {storage}G',description=description,price=price)
    quotes=[dict(id=i+1,model='17',storage=storage,grade=grade,price=amount)
            for i,(grade,amount) in enumerate([('靓机',3000),('小花',2800),('大花',2400)])]
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    facts.verify(item['title']+'\n'+description+f'\n页面挂牌价：¥{price:g}')
    return price_facts(item,facts,quotes,rows,SmartConfig(),rules or Rules(),'17',storage), facts


@pytest.mark.parametrize('version,fee', [('国行',0),('港版',200),('港澳版',200),('香港版',200),
                                        ('美版无锁',500),('日版',500),('外版',500)])
def test_actual_variant_deduction_and_aliases_do_not_stack(version,fee):
    result,facts=assess('99新，'+version,notes('港澳版-200','水货三网-500'))
    assert result['status']=='matched' and result['value_high']==3000-fee
    assert len(result['deductions'])==int(bool(fee))


@pytest.mark.parametrize('description,fee', [('99新，红色',150),('99新，黄色',150),('99新，黑色',0),('99新，远峰蓝',0)])
def test_combined_colors_are_one_condition(description,fee):
    result,_=assess(description,notes('黄色/红色-150'))
    assert result['status']=='matched' and result['value_high']==3000-fee


def test_version_color_health_and_part_costs_are_cumulative():
    result,_=assess('99新，港版，红色，电池健康78%，换过第三方电池',
                   notes('港澳版-200','水货三网-500','红色-50','电池低于80%-100','弹电-300','第三方-80'))
    assert result['status']=='matched' and result['value_low']==2570
    assert result['threshold_low']==2770 and len(result['deductions'])==4


@pytest.mark.parametrize('description,low,high,status', [
    ('99新，换过屏',200,300,'pending'), ('99新，换过原装屏',200,200,'matched'),
    ('99新，换过第三方屏',300,300,'matched'),('99新，没换过屏',0,0,'matched'),
    ('99新，原装屏',0,0,'matched'),
])
def test_screen_type_selects_quote_or_keeps_possible_range(description,low,high,status):
    result,_=assess(description,notes('弹屏-300','弹正屏-200'))
    assert result['status']==status and result['value_high']==3000-low
    assert sum(r['max'] for r in result['deductions'])==high
    assert len(result['deductions'])==int(bool(high))


@pytest.mark.parametrize('description,low,high,status', [
    ('99新，换了电池',100,200,'pending'), ('99新，更换过原装电池',200,200,'matched'),
    ('99新，换过第三方电池',100,100,'matched'), ('99新，没有换电池',0,0,'matched'),
])
def test_battery_type_selects_one_fee(description,low,high,status):
    result,_=assess(description,notes('弹电-200','第三方-100'))
    assert result['status']==status and result['value_high']==3000-low
    assert sum(r['max'] for r in result['deductions'])==high


@pytest.mark.parametrize('price,status',[(3000,'pending'),(3001,'rejected')])
def test_unspecified_part_uses_lower_deduction_endpoint_plus_negotiation(price,status):
    result,_=assess('99新，换过屏',notes('弹屏-300','弹正屏-200'),price=price)
    assert result['status']==status and result['threshold_high']==3000
    assert result['value_high']==2800 and result['value_low'] is None


def test_both_replaced_parts_are_deducted_and_can_reject_under_base_price():
    result,_=assess('99新，换过屏幕，换过电池',notes('弹屏-300','弹正屏-200','弹电-200','第三方-100'),price=2901)
    assert result['status']=='rejected' and result['value_high']==2700
    assert result['threshold_high']==2900 and len(result['deductions'])==2


@pytest.mark.parametrize('storage,color,fee',[('256','蓝色',300),('512','蓝色',500),('256','黑色',0),('512','黑色',0)])
def test_capacity_only_selects_base_or_limits_explicit_color_condition(storage,color,fee):
    result,_=assess('99新，'+color,notes('256G蓝色-300','512/1T蓝色-500'),storage=storage)
    assert result['value_high']==3000-fee and result['status']=='matched'
    assert deduction_storages('换屏-512')==[]
    assert deduction_storages('512/1T蓝色')==['512','1T']


def test_capacity_specific_import_rule_overrides_general_import_fee():
    result,_=assess('99新，美版无锁',notes('水货三网-500','水货1T-800'),storage='1T')
    assert result['value_high']==2200 and len(result['deductions'])==1


@pytest.mark.parametrize('grade_text,fee', [('99新',100),('明显划痕',0)])
def test_grade_scope_does_not_apply_liangji_xiaohua_fee_to_dahua(grade_text,fee):
    result,_=assess(grade_text+'，电池健康80%',notes('靓机小花电池低于85%-100'))
    base=3000 if grade_text=='99新' else 2400
    assert result['value_high']==base-fee


@pytest.mark.parametrize('health,fee',[(85,0),(82,50),(79,100),(69,200)])
def test_wrapped_battery_tiers_are_reconstructed_and_not_cumulative(health,fee):
    rows=notes('靓机小花电池低于83%-50（电池低于','80%-100','电池低于70%-200）')
    result,_=assess(f'99新，电池健康{health}%',rows)
    assert result['value_high']==3000-fee and len(result['deductions'])==int(bool(fee))


@pytest.mark.parametrize('health,fee',[(75,0),(70,0),(69,300)])
def test_battery_reference_uses_quoted_amount_from_same_model(health,fee):
    result,_=assess(f'99新，电池健康{health}%',notes('电池低于70％按3单电','弹电-300'))
    assert result['value_high']==3000-fee


@pytest.mark.parametrize('description,fee', [('99新，屏幕轻微老化',100),('99新，屏幕无老化',0),
    ('99新，后置摄像头有彩点',120),('99新，前置摄像头有蓝光',30),
    ('99新，镜头有水印',50),('99新，照片有水印',0),('99新，面容正常',0)])
def test_other_explicit_quote_conditions_are_recognized_without_normal_feature_false_positives(description,fee):
    rows=notes('轻微老化-100','后像头彩点-120','前蓝光-30','像头水印-50','面容-400-600')
    result,_=assess(description,rows)
    assert result['status']=='matched' and result['value_high']==3000-fee


def test_grade_qualified_combined_replacement_is_not_added_twice():
    result,_=assess('99新，电池弹窗',notes('靓机小花弹电/弹屏-700','大花后等级弹电/弹屏-500','弹电-350','弹屏-500'))
    assert result['value_high']==2300 and len(result['deductions'])==1


def test_unknown_popup_keeps_quoted_alternatives_instead_of_assuming_popup():
    result,_=assess('99新，换过电池',notes('靓机小花弹电/弹屏-700','弹电-350'))
    assert result['status']=='pending' and result['value_high']==2650
    assert result['deductions'][0]['max']==700


def test_identified_but_unpriced_repair_uses_zero_deduction_and_continues():
    result,_=assess('99新，换过屏',[])
    assert result['status']=='pending' and result['value_high']==3000
    assert result['threshold_high']==3200 and result['deductions']==[]
    assert 'screen_replaced' in result['unpriced_conditions']


@pytest.mark.parametrize('price,status', [(3000,'pending'),(3001,'rejected')])
def test_missing_camera_fee_keeps_xiaohua_and_negotiation_boundary(price,status):
    result,_=assess('轻微划痕，换过后摄',[],price=price)
    assert result['grade']=='小花' and result['base_price']==2800
    assert result['value_high']==2800 and result['threshold_high']==3000
    assert result['status']==status and result['deductions']==[]
    assert 'rear_camera_replaced' in result['unpriced_conditions']
    if status=='rejected':
        assert result['rejection_code']=='price_high'


def test_missing_fee_does_not_discard_existing_battery_deduction():
    result,_=assess('轻微划痕，换过后摄，电池健康78%',notes('电池低于80%扣50'),price=2950)
    assert result['status']=='pending' and result['grade']=='小花'
    assert result['value_high']==2750 and result['threshold_high']==2950
    assert len(result['deductions'])==1 and result['deductions'][0]['min']==50
    assert 'rear_camera_replaced' in result['unpriced_conditions']


def test_camera_replacement_fee_also_covers_generic_repair():
    result,_=assess('轻微划痕，换过后摄，电池健康78%',notes('后换像头-700','电池低于80%扣50'))
    assert result['status']=='matched' and result['value_high']==2050
    assert result['threshold_high']==2250 and len(result['deductions'])==2
    assert not result.get('unpriced_conditions')


def test_unknown_grade_with_missing_fee_still_uses_xiaohua():
    result,_=assess('换过后摄',[],price=3000)
    assert result['grade'] is None and result['reference_grade']=='小花'
    assert result['status']=='pending' and result['value_high']==2800 and result['threshold_high']==3000


@pytest.mark.parametrize('amount', [None,-100,float('nan')])
def test_missing_or_invalid_fee_amount_is_zero_not_a_rejection(amount):
    rows=notes('后换像头-700')
    rows[0].update(amount_min=amount,amount_max=amount)
    result,_=assess('轻微划痕，换过后摄',rows)
    assert result['status']=='pending' and result['value_high']==2800
    assert result['deductions']==[] and result['unpriced_notes']


async def test_missing_fee_pending_item_is_delivered_with_usable_reference(database):
    from app_monitor.service import MonitorService
    from app_monitor.notifications import NotificationService

    class Notifier:
        def __init__(self):self.sent=[]
        async def send(self,item,reason):
            self.sent.append(item)
            return {'mock':{'success':True}}

    add_quotes(database,[('14','256','靓机',1950),('14','256','小花',1750)])
    notifier=Notifier(); service=MonitorService(database,None,notifier)
    item={'item_id':'1090413599999','title':'iPhone 14 256G 国行','description':'轻微划痕，换过后摄',
          'price':1600,'item_url':'https://www.goofish.com/item?id=1090413599999','display_region':'厦门'}
    result=await service.process_items([item],Rules(),'iPhone 14',True,'256',expected_model='14')
    assert result['notified']==1 and len(notifier.sent)==1
    value=notifier.sent[0]['smart_assessment']
    assert value['status']=='pending' and value['base_price']==1750
    assert value['value_high']==1750 and value['threshold_high']==1950
    assert result['decisions'][0]['code']=='pending'
    _,body=NotificationService._format_message(notifier.sent[0],'','',[])
    assert '小花参考，无对应扣费，按0计算' in body
    assert '无法计价' not in body and '待确认：' not in body
    await service.process_items([item],Rules(),'iPhone 14',True,'256',expected_model='14')
    assert len(notifier.sent)==1


def test_legacy_rule_identity_and_manual_override_survive_compilation(database):
    from app_monitor.smart import digest,Policy
    batch=add_quotes(database,[('17','256','靓机',3000)])
    with database.connect() as c:
        _save_deductions(c,batch,'100025',[QuoteDeduction('17',0,0,'港澳版-200','港澳版',200,200)])
    key=digest(['100025','17','港澳版','deduct',False,0,0])
    custom=Policy(reviewed=True,action='ignore').model_dump()
    database.state_set('smart_policies',{key:custom})
    row=policies_for(database,'17','100025')[0]
    assert row['rule_key']==key and row['policy']==custom


async def test_delivery_gate_uses_deductions_and_never_sends_rejected_repair(database):
    from app_monitor.service import MonitorService
    class Notifier:
        def __init__(self):self.sent=[]
        async def send(self,item,reason):
            self.sent.append(item)
            return {'mock':{'success':True}}
    batch=add_quotes(database,[('17','256','靓机',3000),('17','256','小花',2800)])
    with database.connect() as c:
        _save_deductions(c,batch,'100025',[QuoteDeduction('17',0,0,'港澳版-200','港澳版',200,200),
            QuoteDeduction('17',1,0,'弹电-300','弹电',300,300),QuoteDeduction('17',1,1,'第三方-100','第三方',100,100)])
    notifier=Notifier();service=MonitorService(database,None,notifier)
    item={'item_id':'1090413599999','title':'iPhone 17 256G','description':'99新，港版，换过第三方电池',
          'price':2901,'item_url':'https://www.goofish.com/item?id=1090413599999','display_region':'厦门'}
    await service.process_items([item],Rules(),'iPhone 17',True,'256',expected_model='17')
    assert notifier.sent==[]  # Base+200 would pass, but 3000-200-100+200 does not.
    await service.process_items([{**item,'price':2900}],Rules(),'iPhone 17',True,'256',expected_model='17')
    assert len(notifier.sent)==1
    assert notifier.sent[0]['smart_assessment']['value_high']==2700
    assert notifier.sent[0]['smart_assessment']['threshold_high']==2900


def test_battery_reference_replaces_health_and_replacement_fee_without_double_charge():
    result,_=assess('99新，电池健康69%，换过第三方电池',
                   notes('靓机小花电池低于70%-150','电池低于70%按弹电','弹电-300','第三方-100'))
    assert result['value_high']==2700 and len(result['deductions'])==1


def test_accessory_note_for_unimported_high_warranty_grade_does_not_charge_liangji():
    result,_=assess('99新，单机出，无配件',notes('高保充新无','全套-100'))
    assert result['status']=='matched' and result['value_high']==3000


def test_combined_screen_and_battery_popup_is_not_reported_as_certain_single_fee():
    result,_=assess('99新，屏幕弹窗，电池弹窗',notes('靓机小花弹电/弹屏-700'))
    assert result['status']=='pending'
    assert result['value_high']==2300 and result['deductions'][0]['max']==1400


def test_grade_change_precedes_scoped_fees_and_keeps_its_original_trigger():
    rows=notes('后盖破当大花收','靓机小花电池低于85%-100')
    rows[0]['policy']['grades']=['靓机']
    result,_=assess('99新，后盖破，电池健康80%',rows)
    assert result['grade']=='大花' and result['base_price']==2400
    assert result['deductions']==[] and result['status']=='matched'


@pytest.mark.parametrize('phrase', ['新原装电芯100健康','换过电芯','更换了原装电芯',
                                  '换过全新原装电芯','电芯已更换','换新电芯'])
def test_new_or_replaced_cell_counts_as_battery_replacement_with_evidence(phrase):
    result,facts=assess('99新，'+phrase,notes('弹电-300'))
    assert facts.findings['battery_cell_replaced'].value=='yes'
    assert facts.findings['battery_replaced'].value=='yes'
    assert facts.findings['battery_third_party'].value is None
    assert result['value_high']==2700 and len(result['deductions'])==1
    if '100健康' in phrase:assert facts.findings['battery_health'].value=='100'


@pytest.mark.parametrize('phrase', ['原装电芯','原装电芯100健康','没有换过电芯','没换过新电芯',
                                  '未换过全新原装电芯','新机未拆封，原装电芯','电池健康100%'])
def test_original_cell_or_healthy_battery_alone_does_not_imply_replacement(phrase):
    result,facts=assess('99新，'+phrase,notes('弹电-300'))
    assert facts.findings['battery_cell_replaced'].value!='yes'
    assert facts.findings['battery_replaced'].value!='yes'
    assert result['deductions']==[] and result['value_high']==3000


def test_suspected_cell_replacement_remains_uncertain_not_a_confirmed_deduction():
    result,facts=assess('99新，可能更换过电芯',notes('弹电-300'))
    assert facts.findings['battery_cell_replaced'].value is None
    assert result['status']=='pending' and result['deductions']==[]


def test_original_cell_does_not_claim_a_genuine_complete_battery_or_cheaper_type():
    result,facts=assess('99新，新原装电芯100健康',notes('弹电-300','第三方-100'))
    assert facts.findings['battery_third_party'].value is None
    assert result['status']=='pending' and result['value_high']==2900
    assert result['deductions'][0]['min']==100 and result['deductions'][0]['max']==300
