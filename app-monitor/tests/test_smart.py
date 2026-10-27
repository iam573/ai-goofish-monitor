import json
from dataclasses import replace

import httpx
import pytest

from app_monitor.config import settings
from app_monitor.matching import evaluate
from app_monitor.quotes import latest_entries
from app_monitor.rules import Rules, eligible_quotes, save_rules
from app_monitor.smart import (Facts, Finding, Policy, SmartConfig, SmartService, candidate_decision, price_facts, policies_for)
from app_monitor.service import MonitorService
from app_monitor.notifications import NotificationService
from conftest import add_quotes

TEXT = 'iPhone 17 256G 国行整机，完整售价5050元。无明显划痕无磕碰。电池78%。无水印无碎屏，功能正常，无锁，无拆修。'
ITEM = {'item_id':'1090413599999','title':'iPhone 17 256G 国行自用','price':5050,
        'description':TEXT,'display_region':'厦门','seller_name':'小鱼','is_fish_shop':True,
        'item_url':'https://www.goofish.com/item?id=1090413599999'}


def facts():
    return Facts(grade='靓机',grade_evidence='无明显划痕无磕碰',findings={
        'model':Finding(value='17',evidence='iPhone 17'), 'storage':Finding(value='256',evidence='256G'),
        'whole_phone':Finding(value='yes',evidence='国行整机'), 'full_price':Finding(value='yes',evidence='完整售价5050元'),
        'battery_health':Finding(value='78',evidence='电池78%'),
        'screen_watermark':Finding(value='no',evidence='无水印'), 'screen_broken':Finding(value='no',evidence='无碎屏'),
        'function_fault':Finding(value='no',evidence='功能正常'), 'locked':Finding(value='no',evidence='无锁'),
        'repaired':Finding(value='no',evidence='无拆修')})


def quote_setup(database):
    add_quotes(database,[('17','256','靓机',5200),('17','256','小花',4600),('17','256','大花',4000)])
    return latest_entries(database,'100025')


def deduction(low=300,high=300,open_ended=False):
    return {'name':'电池低于80%','text':'电池低于80%扣300','amount_min':low,'amount_max':high,
            'open_ended':open_ended,'policy':Policy(reviewed=True).model_dump()}


def test_candidate_uses_highest_accepted_grade_and_not_fixed_grade(database):
    quotes=quote_setup(database); rules=Rules(quote_grade='小花',region_city='厦门')
    assert not evaluate(ITEM,rules,eligible_quotes(database,rules)).matched
    candidate = candidate_decision(ITEM,rules,quotes,SmartConfig(),True,'256','17')
    assert candidate.matched and candidate.quote_grade == '靓机' and candidate.quote_basis == 'candidate'
    assert candidate_decision({**ITEM,'price':1},rules,quotes,SmartConfig(),True,'256','17').matched
    assert not candidate_decision({**ITEM,'price':5500},rules,quotes,SmartConfig(),True,'256','17').matched
    assert not candidate_decision({**ITEM,'display_region':'北京'},rules,quotes,SmartConfig(),True,'256','17').matched


def test_facts_must_have_real_evidence_and_correct_battery_number():
    assert facts().verify(TEXT).grade=='靓机'
    fake=facts();fake.findings['battery_health']=Finding(value='98',evidence='电池78%')
    with pytest.raises(ValueError,match='数字'):fake.verify(TEXT)
    fake=facts();fake.grade_evidence='完全不存在的描述'
    with pytest.raises(ValueError,match='原文'):fake.verify(TEXT)
    assert Facts(grade='靓机',grade_evidence='几乎全新').verify('几乎全新').grade == '靓机'
    with pytest.raises(ValueError):Facts(findings={'money':{'value':'6000','evidence':'xxx'}})


def test_deterministic_pricing_uses_quote_money_and_fixed_deduction(database):
    result=price_facts(ITEM,facts(),quote_setup(database),[deduction()],SmartConfig(),Rules(),'17','256')
    assert result['status']=='matched'
    assert (result['base_price'],result['value_low'],result['value_high'],result['threshold_low'])==(5200,4900,4900,5100)


@pytest.mark.parametrize('price,fee,status,ceiling', [
    (4800, 0, 'pending', 4600), (4801, 0, 'rejected', 4600),
    (4500, 300, 'pending', 4300), (4501, 300, 'rejected', 4300),
])
def test_unknown_grade_uses_xiaohua_and_applies_deductions(database, price, fee, status, ceiling):
    f = facts(); f.grade = None; f.grade_evidence = ''
    value = price_facts({**ITEM, 'price': price}, f, quote_setup(database), [deduction(fee, fee)] if fee else [],
                        SmartConfig(), Rules(), '17', '256')
    assert value['status'] == status and value['grade'] is None
    assert value['reference_grade'] == '小花' and value['base_price'] == 4600
    assert value['value_high'] == ceiling and value['threshold_high'] == ceiling + 200
    assert value['value_low'] is None and value['threshold_low'] is None
    assert value['quote_entry_id'] is None


@pytest.mark.parametrize('missing_quote', [True, False])
def test_unknown_grade_does_not_use_liangji_if_xiaohua_is_unavailable(database, missing_quote):
    quotes = quote_setup(database)
    config = SmartConfig()
    if missing_quote:
        quotes = [quote for quote in quotes if quote['grade'] != '小花']
    else:
        config = SmartConfig(accepted_grades=['靓机'])
    f = facts(); f.grade = None
    value = price_facts(ITEM, f, quotes, [], config, Rules(), '17', '256')
    assert value['status'] == 'rejected' and '小花报价' in value['reason']
    assert value['base_price'] is None and value['value_high'] is None


async def test_pricing_version_recalculates_without_repeating_ai_facts(database, monkeypatch):
    from app_monitor import smart
    quote_setup(database)
    database.state_set('smart_config', SmartConfig(ai_enabled=True).model_dump())
    service = SmartService(database, settings)
    calls = []
    async def extract(*args):
        calls.append(True)
        f = facts(); f.grade = None; f.grade_evidence = ''
        return f
    monkeypatch.setattr(service, 'request_facts', extract)
    rules = Rules(max_over_quote=600)
    item = {**ITEM, 'description': TEXT.replace('无明显划痕无磕碰。', '')}
    first = await service.assess(item, rules, '17', '256', True)
    monkeypatch.setattr(smart, 'PRICING_VERSION', 'test-new-pricing-version')
    second = await service.assess(item, rules, '17', '256', True)
    assert first['id'] != second['id'] and calls == [True]
    assert second['result']['base_price'] == 4600 and second['result']['grade'] is None
    assert service.get(first['id'])['result'] == first['result']


@pytest.mark.parametrize('price,status',[(5000,'matched'),(5100,'pending'),(5201,'rejected')])
def test_interval_deduction_uses_conservative_threshold(database,price,status):
    result=price_facts({**ITEM,'price':price},facts(),quote_setup(database),[deduction(200,400)],SmartConfig(),Rules(),'17','256')
    assert result['status']==status
    assert result['value_high']==5000


def test_unmentioned_conditions_default_normal_but_open_ended_fee_is_pending(database):
    f=facts();f.findings.pop('battery_health')
    result=price_facts(ITEM,f,quote_setup(database),[deduction()],SmartConfig(),Rules(),'17','256')
    assert result['status']=='matched' and result['value_low'] == 5200
    assert not result['deductions'] and '电池健康度未说明，按正常处理' in result['defaults']
    result=price_facts({**ITEM,'price':3500},facts(),latest_entries(database,'100025'),[deduction(1000,1000,True)],SmartConfig(),Rules(),'17','256')
    assert result['status']=='pending' and result['value_low'] is None


def test_default_blocked_fault_and_non_whole_price_reject(database):
    quotes=quote_setup(database)
    for key,value in [('screen_watermark','yes'),('whole_phone','no'),('full_price','no'),('locked','yes')]:
        f=facts();f.findings[key].value=value
        assert price_facts(ITEM,f,quotes,[],SmartConfig(),Rules(),'17','256')['status']=='rejected'


def test_unconfirmed_and_overlapping_rules_require_review(database):
    quotes=quote_setup(database); row=deduction();row['policy']['reviewed']=False
    result=price_facts(ITEM,facts(),quotes,[row],SmartConfig(),Rules(),'17','256')
    assert result['status']=='pending' and result['value_high']==5200
    assert not result['deductions'] and result['unpriced_notes']
    result=price_facts(ITEM,facts(),quotes,[deduction(),deduction()],SmartConfig(),Rules(),'17','256')
    assert result['status']=='pending' and any('重复' in text for text in result['uncertainties'])


def test_downgrade_rules_obey_allowed_grades(database):
    quotes=quote_setup(database); row=deduction();row['policy'].update(action='grade',grade='大花')
    result=price_facts({**ITEM,'price':3900},facts(),quotes,[row],SmartConfig(),Rules(),'17','256')
    assert result['grade']=='大花' and result['base_price']==4000
    assert result['deductions']==[]


async def test_model_retries_invalid_json_checks_evidence_and_redacts_errors(database,monkeypatch):
    from app_monitor import smart
    config=SmartConfig(base_url='https://model.test/v1',model='test')
    service=SmartService(database,replace(settings,ai_api_key='test-secret'))
    attempts=[]
    def handler(request):
        attempts.append(request)
        content='not JSON' if len(attempts)==1 else facts().model_dump_json()
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]})
    real=httpx.AsyncClient
    monkeypatch.setattr(smart.httpx,'AsyncClient',lambda **kwargs:real(transport=httpx.MockTransport(handler),**kwargs))
    assert (await service.request_facts(ITEM,config)).grade=='靓机'
    assert len(attempts)==2
    with service.run_settings():
        service.api_key='next-test-secret'
        await service.request_facts(ITEM,config)
        assert attempts[-1].headers['Authorization']=='Bearer test-secret'
    await service.request_facts(ITEM,config)
    assert attempts[-1].headers['Authorization']=='Bearer next-test-secret'
    assert 'test-secret' not in json.dumps(service.public_config())


async def test_assessment_cache_snapshot_failure_and_manual_correction(database,monkeypatch):
    quote_setup(database); database.state_set('smart_config',SmartConfig(enabled=True,ai_enabled=True).model_dump())
    service=SmartService(database,settings);calls=[]
    async def fake_request(*args):calls.append(1);return facts()
    monkeypatch.setattr(service,'request_facts',fake_request)
    a=await service.assess(ITEM,Rules(),'17','256',True)
    b=await service.assess(ITEM,Rules(),'17','256',True)
    assert a['id']==b['id'] and len(calls)==1 and a['snapshot']['description']==TEXT
    changed=await service.assess(ITEM,Rules(max_over_quote=500),'17','256',True)
    assert changed['id']!=a['id'] and len(calls)==1 # monetary recalculation reuses facts
    corrected=await service.assess(ITEM,Rules(),'17','256',True,force=True,manual=facts())
    assert corrected['reviewed'] and corrected['source']=='manual'
    async def failed(*args):raise ValueError('a private server response')
    monkeypatch.setattr(service,'request_facts',failed)
    failure=await service.assess(ITEM,Rules(),'17','256',True,force=True)
    assert failure['status']=='pending' and not failure['result']['ai_reviewed']
    assert 'private' not in failure['result']['reason'] and failure['result']['value_low'] is None


class Notifier:
    def __init__(self):self.sent=[]
    async def send(self,item,reason):self.sent.append(item);return {'mock':{'success':True}}


async def test_pending_dedup_upgrade_and_shadow_delivery(database,monkeypatch):
    rules=Rules(region_city='厦门',max_over_quote=600)
    quote_setup(database);config=SmartConfig(enabled=True,shadow=False,ai_enabled=True)
    database.state_set('smart_config',config.model_dump());save_rules(database,rules)
    notifier=Notifier();service=MonitorService(database,None,notifier)
    async def extract(*args):f=facts();f.grade=None;return f
    monkeypatch.setattr(service.smart,'request_facts',extract)
    await service.process_items([ITEM],rules,'iPhone 17',True,'256',expected_model='17')
    await service.process_items([ITEM],rules,'iPhone 17',True,'256',expected_model='17')
    assert len(notifier.sent)==1 and notifier.sent[0]['smart_assessment']['status']=='pending'
    record=await service.smart.assess(ITEM,rules,'17','256',True,force=True,manual=facts())
    await service.publish_smart(record,rules)
    await service.publish_smart(record,rules)
    assert len(notifier.sent)==2 and notifier.sent[-1]['smart_assessment']['status']=='matched'
    assert notifier.sent[-1]['seller_name']=='小鱼'
    database.state_set('smart_config',config.model_copy(update={'shadow':True}).model_dump())
    record=await service.smart.assess({**ITEM,'item_id':'1090413599998'},Rules(),'17','256',True,force=True,manual=facts())
    await service.publish_smart(record,Rules())
    assert len(notifier.sent)==2


def test_pending_notification_shows_bounds_and_seller_not_profit(database):
    f=facts();f.grade=None
    value=price_facts(ITEM,f,quote_setup(database),[],SmartConfig(),Rules(max_over_quote=600),'17','256')
    item={**ITEM,'model':'17','storage':'256','smart_assessment':value,'assessment_facts':f.model_dump()}
    title,body=NotificationService._format_message(item,'','',[])
    assert title=='【待确认】iPhone 17 256G'
    assert '成色待确认' in body and '回收参考：≤ ¥4600（小花参考，未扣费，待确认）' in body
    assert '低于估值' not in body and '成色无法确定' not in body
    assert '小鱼（鱼小铺）' in body and '利润' not in body


async def test_previous_sent_deal_not_resent_after_switching_to_local_rules(database):
    quote_setup(database)
    notifier=Notifier();service=MonitorService(database,None,notifier)
    rules=Rules(quote_grade='靓机')
    item=service._normalize_item(ITEM,'iPhone 17')
    decision=evaluate(item,rules,eligible_quotes(database,rules),True,'256','17')
    assert decision.matched
    service._save_item(item,decision)
    deal_id=service._save_deal(item,decision,rules)
    with database.connect() as c:
        c.execute("UPDATE deals SET notification_status='sent' WHERE id=?",(deal_id,))
    record=await service.smart.assess(ITEM,Rules(),'17','256',True)
    assert await service.publish_smart(record,Rules())==(False,False)
    assert notifier.sent==[]
