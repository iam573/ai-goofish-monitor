"""User-editable shared vocabularies must affect actual valuation and notifications."""
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app_monitor import api
from app_monitor.keyword_rules import default_condition_keywords
from app_monitor.local_rules import extract_local
from app_monitor.quotes import latest_entries
from app_monitor.rules import Rules
from app_monitor.service import MonitorService
from app_monitor.smart import SmartConfig, candidate_decision, price_facts, policies_for, SmartService
from test_shared_policies import seed


def item(model='14',description='轻微划痕，换了某品牌显示总成',price=3100):
    return dict(item_id='1090000000777',title=f'iPhone {model} 256G 国行',description=description,
                price=price,display_region='厦门',item_url='https://www.goofish.com/item?id=1090000000777')


def custom(key,word):
    config=SmartConfig()
    config.condition_keywords[key].append(word)
    return config


@pytest.mark.parametrize('model,fee',[('14',100),('15',300)])
def test_one_shared_screen_keyword_uses_each_models_fee(database,model,fee):
    seed(database)
    config=custom('screen_replaced','某品牌显示总成')
    raw=item(model);quotes=latest_entries(database,'100025')
    facts=extract_local(raw,config,quotes,Rules());facts.verify(api.evidence_text(raw))
    value=price_facts(raw,facts,quotes,policies_for(database,model,'100025'),config,Rules(),model,'256')
    assert facts.findings['screen_replaced'].value=='yes'
    assert value['value_high']==3000-fee
    assert value['threshold_high']==3200-fee
    assert value['status']==('matched' if model=='14' else 'rejected')


@pytest.mark.parametrize('description,expected',[
    ('国产屏','yes'),('华星屏','yes'),('天马屏','yes'),
    ('没有国产屏','no'),('不是华星屏','no'),('可能是国产屏',None),
])
def test_default_screen_words_and_negation(description,expected):
    facts=extract_local(item(description=description),SmartConfig(),[],Rules())
    assert facts.findings['screen_replaced'].value==expected


def test_removing_keyword_really_disables_that_match():
    config=SmartConfig();config.condition_keywords['screen_replaced'].remove('国产屏')
    facts=extract_local(item(description='国产屏'),config,[],Rules())
    assert facts.findings['screen_replaced'].value is None
    # A different enabled word can still establish a replacement.
    assert extract_local(item(description='换过屏，是国产屏'),config,[],Rules()).findings['screen_replaced'].value=='yes'


def test_empty_keyword_group_disables_all_its_forms():
    config=SmartConfig();config.condition_keywords['battery_swollen']=[]
    assert extract_local(item(description='电池膨胀，鼓包'),config,[],Rules()).findings['battery_swollen'].value is None


@pytest.mark.parametrize('description,code',[
    ('屏幕有芝麻斑','excluded'),('屏幕没有芝麻斑','matched'),('屏幕疑似芝麻斑','matched'),
])
def test_custom_blocked_fault_word_is_shared_by_list_and_detail(database,description,code):
    seed(database)
    config=custom('screen_watermark','芝麻斑')
    raw=item(description='轻微划痕',price=2800);raw['title']+=' '+description
    quotes=latest_entries(database,'100025')
    broad=candidate_decision(raw,Rules(),quotes,config,True,'256','14')
    assert broad.code==code
    facts=extract_local(raw,config,quotes,Rules())
    if code=='excluded':
        assert facts.findings['screen_watermark'].value=='yes'
        assert price_facts(raw,facts,quotes,[],config,Rules(),'14','256')['status']=='rejected'


def test_custom_terms_are_literals_not_regular_expressions():
    config=custom('screen_replaced','品牌[屏].*')
    assert extract_local(item(description='品牌屏幕'),config,[],Rules()).findings['screen_replaced'].value is None
    assert extract_local(item(description='品牌[屏].*'),config,[],Rules()).findings['screen_replaced'].value=='yes'


def test_grade_words_are_shared_and_search_title_is_also_used():
    config=SmartConfig();config.grade_keywords['小花'].append('日常磨痕')
    raw=item(description='功能正常');raw['title']+=' 日常磨痕'
    facts=extract_local(raw,config,[],Rules())
    assert facts.grade=='小花' and '日常磨痕' in facts.grade_evidence
    for grade in config.grade_keywords:
        config.grade_keywords[grade]=[word for word in config.grade_keywords[grade] if word not in {'无划痕','无磕碰'}]
    config.grade_keywords['小花']+=['无划痕','无磕碰']
    assert extract_local(item(description='无划痕，无磕碰'),config,[],Rules()).grade=='小花'


@pytest.mark.parametrize('edit',[
    lambda words:words.pop('screen_replaced'),
    lambda words:words.update(unknown=['屏幕']),
    lambda words:words.update(screen_replaced=['']),
    lambda words:words.update(screen_replaced=['\u200b']),
    lambda words:words.update(screen_replaced=['x'*41]),
    lambda words:words.update(screen_replaced=[str(i) for i in range(51)]),
])
def test_invalid_keyword_config_is_rejected(edit):
    words=default_condition_keywords();edit(words)
    with pytest.raises(ValidationError):SmartConfig(condition_keywords=words)


async def test_old_config_defaults_and_save_during_run_uses_next_round(database,monkeypatch):
    # Old saved config has no new field; parsing supplies defaults without rewriting it.
    database.state_set('smart_config',{'enabled':True})
    smart=SmartService(database,SimpleNamespace(ai_api_key=''))
    old=smart.config();assert '国产屏' in old.condition_keywords['screen_replaced']
    monkeypatch.setattr(api,'database',database)
    monkeypatch.setattr(api,'monitor_service',SimpleNamespace(busy=True,smart=smart))
    with smart.run_settings():
        changed=old.model_copy(deep=True);changed.condition_keywords['screen_replaced'].append('换显示总成')
        await api.save_smart_config(api.SmartConfigPayload(**changed.model_dump()))
        assert '换显示总成' not in smart.config().condition_keywords['screen_replaced']
        assert '换显示总成' in (await api.smart_config())['condition_keywords']['screen_replaced']
    assert '换显示总成' in smart.config().condition_keywords['screen_replaced']


async def test_keyword_change_invalidates_assessment_and_changes_notification_eligibility(database):
    seed(database)
    config=SmartConfig();database.state_set('smart_config',config.model_dump())
    service=MonitorService(database,None,None)
    raw=item('15',price=3100)
    first=await service.smart.assess(raw,Rules(),'15','256',True)
    assert first['status']=='matched'
    config.condition_keywords['screen_replaced'].append('某品牌显示总成')
    service.smart.save_config(config)
    second=await service.smart.assess(raw,Rules(),'15','256',True)
    assert second['id']!=first['id'] and second['status']=='rejected'
    assert second['result']['value_high']==2700
    class Notifier:
        async def send(self,*args):pytest.fail('Deduction makes this item too expensive')
    service.notifications=Notifier()
    assert await service.publish_smart(second,Rules())==(False,False)
