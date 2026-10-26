import pytest

from app_monitor.local_rules import automatic_policy, extract_local, merge_review
from app_monitor.rules import Rules
from app_monitor.smart import Facts, Finding, SmartConfig, SmartService, candidate_decision, evidence_text, price_facts
from app_monitor.quotes import latest_entries
from conftest import add_quotes


@pytest.fixture
def quotes(database):
    add_quotes(database,[('17','256','靓机',5200),('17','256','小花',4800),('17','256','大花',4000)])
    return latest_entries(database,'100025')


def listing(description,price=4500,title='iPhone 17 256G'):
    return dict(item_id='1090413599999',title=title,price=price,description=description,
                display_region='厦门',item_url='https://www.goofish.com/item?id=1090413599999')


@pytest.mark.parametrize('text,grade',[
    ('99新','靓机'),('几乎全新，无拆修','靓机'),('无拆修',None),
    ('99新，但有轻微划痕','小花'),('99新，有轻微磕碰','小花'),
    ('几乎全新，但是边框掉漆','大花'),('无明显划痕无磕碰','靓机'),
    ('99新，无明显划痕，但有明显磕碰','大花'),('不是99新',None),
])
def test_grade_prefers_specific_damage_and_understands_negation(quotes,text,grade):
    item=listing(text)
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    assert facts.grade==grade
    facts.verify(evidence_text(item))


def test_missing_conditions_are_normal_without_inventing_health_or_repair(quotes):
    item=listing('几乎全新',price=5300)
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    result=price_facts(item,facts,quotes,[],SmartConfig(),Rules(),'17','256')
    assert result['status']=='matched' and result['value_low']==5200
    assert result['deductions']==[] and result['uncertainties']==[]
    assert facts.findings['battery_health'].value is None
    assert facts.findings['repaired'].value is None
    assert '拆修情况未说明，按正常处理' in result['defaults']


@pytest.mark.parametrize('text,field,value,status',[
    ('99新，无水印无拆修','screen_watermark','no','matched'),
    ('99新，不保证无水印','screen_watermark',None,'pending'),
    ('99新，图片有水印','screen_watermark',None,'matched'),
    ('99新，屏幕有水印','screen_watermark','yes','rejected'),
    ('99新，功能正常，但面容失灵','function_fault','yes','rejected'),
    ('99新，无拆无修无进水','water_damage','no','matched'),
    ('99新，ID机','locked','yes','rejected'),
    ('99新，安卓爆改','function_fault','yes','rejected'),
    ('99新，仅卖手机壳','whole_phone','no','rejected'),
    ('99新，定金价格','full_price','no','rejected'),
    ('99新，配件齐全','whole_phone','yes','matched'),
    ('99新，无锁机','locked','no','matched'),
])
def test_explicit_faults_and_negative_descriptions(quotes,text,field,value,status):
    item=listing(text)
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    assert facts.findings[field].value==value
    result=price_facts(item,facts,quotes,[],SmartConfig(),Rules(),'17','256')
    assert result['status']==status


def fee(name,amount):
    row=dict(name=name,text=f'{name}扣{amount}',amount_min=amount,amount_max=amount,open_ended=False)
    row['policy']=automatic_policy(row).model_dump()
    return row


def test_explicit_battery_uses_quote_tiers_not_cycle_count_or_double_charge(quotes):
    item=listing('99新，电池健康78%，循环100次',price=4900)
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    assert facts.findings['battery_health'].value=='78'
    result=price_facts(item,facts,quotes,[fee('电池低于85%',100),fee('电池低于80%',300)],SmartConfig(),Rules(),'17','256')
    assert result['status']=='matched' and result['value_low']==4900
    assert len(result['deductions'])==1 and result['deductions'][0]['min']==300
    absent=extract_local(listing('99新，循环100次'),SmartConfig(),quotes,Rules())
    assert absent.findings['battery_health'].value is None


def test_conflicting_battery_stays_pending_and_unsupported_explicit_note_is_visible(quotes):
    item=listing('99新，电池78%，电池90%')
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    assert facts.findings['battery_health'].value is None
    assert price_facts(item,facts,quotes,[],SmartConfig(),Rules(),'17','256')['status']=='pending'
    item=listing('99新，电池78%')
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    result=price_facts(item,facts,quotes,[fee('美版电池低于80%',300)],SmartConfig(),Rules(),'17','256')
    assert result['status']=='pending' and result['deductions']==[]


@pytest.mark.parametrize('description,note,applies', [
    ('99新，电池健康75%', '电池低于70％按3单电', False),
    ('99新，电池健康70%', '电池低于70%按弹电', False),
    ('99新，电池健康69%', '电池低于70%按弹电', True),
    ('99新，电池健康70%', '电池70%以下按弹电', True),
    ('99新，电池健康75%', '电池70%以下按弹电', False),
    ('99新', '电池低于70%按弹电', False),
    ('99新，电池健康75%', '电池鼓', False),
    ('99新，电池健康75%，无鼓包', '电池鼓', False),
    ('99新，电池不鼓包', '电池鼓', False),
    ('99新，电池没有鼓包', '电池鼓', False),
    ('99新，电池鼓包', '电池鼓', True),
    ('99新，电池膨胀', '电池鼓', True),
    ('99新，电池疑似鼓包', '电池鼓', True),
])
def test_unsupported_battery_note_requires_its_condition(quotes, description, note, applies):
    item = listing(description)
    facts = extract_local(item, SmartConfig(), quotes, Rules())
    row = fee(note, 80) if note == '电池鼓' else dict(
        name=note, text=note, amount_min=None, policy=automatic_policy({'name': note, 'text': note}).model_dump())
    result = price_facts(item, facts, quotes, [row], SmartConfig(), Rules(), '17', '256')
    supported = note == '电池鼓' and applies and '疑似' not in description
    assert result['status'] == ('matched' if supported or not applies else 'pending')
    assert bool(result['uncertainties']) == (applies and not supported)
    assert len(result['deductions']) == int(supported)
    if supported:
        assert result['value_low'] == 5120


def test_unsupported_swelling_note_checks_title_and_detail_parameters(quotes):
    for item in [listing('99新', title='iPhone 17 256G 电池鼓包'),
                 {**listing('99新'), 'detail_parameters': ['电池疑似鼓包']}]:
        facts = extract_local(item, SmartConfig(), quotes, Rules())
        result = price_facts(item, facts, quotes, [fee('电池鼓', 80)], SmartConfig(), Rules(), '17', '256')
        if '鼓包' in item['title']:
            assert result['status'] == 'matched' and result['value_low'] == 5120
        else:
            assert result['status'] == 'pending' and result['deductions'] == []


def test_ai_cannot_downgrade_missing_info_policy_or_erase_explicit_risks(quotes):
    item=listing('99新')
    local=extract_local(item,SmartConfig(),quotes,Rules())
    review=Facts(grade='靓机',grade_evidence='99新',uncertainties=['电池健康度未说明','拆修情况未知'])
    assert merge_review(local,review).uncertainties==[]
    local.findings['screen_watermark']=Finding(value='yes',evidence='有水印')
    review.findings['screen_watermark']=Finding(value='no',evidence='无水印')
    merged=merge_review(local,review)
    assert merged.findings['screen_watermark'].value=='yes' and merged.uncertainties


async def test_local_mode_needs_no_ai_and_explicit_rejections_never_call_ai(database,quotes,monkeypatch):
    from app_monitor.config import settings
    service=SmartService(database,settings)
    calls=[]
    async def fail(*args):calls.append(1);raise AssertionError('Must not invoke model')
    monkeypatch.setattr(service,'request_facts',fail)
    service.save_config(SmartConfig())
    result=await service.assess(listing('99新'),Rules(),'17','256',True)
    assert result['status']=='matched' and result['source']=='local' and not calls
    database.state_set('smart_config',SmartConfig(ai_enabled=True).model_dump())
    result=await service.assess(listing('99新，屏幕有水印'),Rules(),'17','256',True)
    assert result['status']=='rejected' and not calls


def test_capacity_context_does_not_override_explicit_other_capacity(quotes):
    assert not candidate_decision(listing('99新',title='iPhone 17 512G'),Rules(),quotes,SmartConfig(),True,'256','17').matched


def test_model_and_capacity_evidence_must_support_values():
    with pytest.raises(ValueError,match='数字'):
        Facts(findings={'battery_health':Finding(value='100',evidence='电池78%，循环100次')}).verify('电池78%，循环100次')
    with pytest.raises(ValueError,match='机型'):
        Facts(findings={'model':Finding(value='17',evidence='iPhone 15')}).verify('iPhone 15')
    f=Facts(findings={'storage':Finding(value='256GB',evidence='256G')}).verify('256G')
    assert f.findings['storage'].value=='256'
    with pytest.raises(ValueError,match='容量'):
        Facts(findings={'storage':Finding(value='512',evidence='256G')}).verify('256G')


def test_stated_repair_without_price_rule_is_pending_with_zero_deduction(quotes):
    item=listing('99新，换过屏')
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    result=price_facts(item,facts,quotes,[],SmartConfig(),Rules(),'17','256')
    assert result['status']=='pending' and any('换屏' in u for u in result['uncertainties'])
    assert result['deductions']==[] and result['value_high']==5200 and result['threshold_high']==5400
    result=price_facts(item,facts,quotes,[fee('换屏',300)],SmartConfig(),Rules(),'17','256')
    assert result['status']=='matched' and result['value_low']==4900


@pytest.mark.parametrize('title,actual',[
    ('女大自用i\u200bP\u200bh\u200bo\u200bn\u200be\u200b7P\u200bl\u200bu\u200bs\u200b 128G 原装 功能正常','7 Plus'),
    ('个人闲置 iPhone 6 128G 金色 国行 插卡即用','6'),
    ('苹果8Plus 128G 备用机','8 Plus'),
    ('iPhone XR 128G 备用机','XR'),
])
def test_unquoted_explicit_model_never_inherits_search_target(title,actual):
    quotes=[dict(id=1,model='16 Plus',storage='128',grade='靓机',price=3500)]
    result=candidate_decision(listing('99新',198,title),Rules(),quotes,SmartConfig(),True,'128','16 Plus')
    assert not result.matched and result.code=='other_model'
    assert result.model==actual and result.storage=='128'


def test_search_context_can_still_admit_a_genuinely_unspecified_model(quotes):
    item=listing('99新',4500,'个人闲置手机 256G，电池健康96%')
    assert candidate_decision(item,Rules(),quotes,SmartConfig(),True,'256','17').matched


def test_explicit_old_model_in_detail_is_not_replaced_with_listing_model(quotes):
    item=listing('iPhone 7 Plus 256G，99新')
    facts=extract_local(item,SmartConfig(),quotes,Rules())
    assert facts.findings['model'].value=='7 Plus'
    assert price_facts(item,facts,quotes,[],SmartConfig(),Rules(),'17','256')['status']=='rejected'


@pytest.mark.parametrize('title,blocked',[
    ('苹果 i\u200bP\u200bh\u200bo\u200bn\u200be\u200b 16 P\u200bl\u200bus 黑色 128G 美版官换有锁 双卡槽 插卡即用',True),
    ('iPhone 16 Plus 128G 美版无锁，屏幕无水印',False),
    ('iPhone 16 Plus 128G 不是有锁机',False),
    ('iPhone 16 Plus 128G 不确定是否有锁',False),
    ('iPhone 16 Plus 128G 屏幕有水印',True),
    ('iPhone 16 Plus 128G 图片有水印',False),
])
def test_explicit_configured_fault_is_filtered_before_opening_detail(title,blocked):
    quotes=[dict(id=1,model='16 Plus',storage='128',grade='靓机',price=3300)]
    result=candidate_decision(listing('99新',3260,title),Rules(),quotes,SmartConfig(),True,'128','16 Plus')
    assert result.matched is not blocked
    if blocked:assert result.code=='excluded' and '不收问题' in result.reason


def test_title_fault_filter_respects_receiving_configuration():
    quotes=[dict(id=1,model='16 Plus',storage='128',grade='靓机',price=3300)]
    item=listing('99新',3260,'iPhone 16 Plus 128G 有锁')
    assert candidate_decision(item,Rules(),quotes,SmartConfig(blocked_conditions=[]),True,'128','16 Plus').matched
