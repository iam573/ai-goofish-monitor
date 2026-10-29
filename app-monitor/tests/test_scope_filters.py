from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app_monitor.local_rules import extract_local
from app_monitor.rules import Rules
from app_monitor.scope_filters import scope_rejection
from app_monitor.service import MonitorService
from app_monitor.smart import SmartConfig, candidate_decision, price_facts
from conftest import add_quotes


def item(title='iPhone 15 Pro 256G', **changes):
    return {'item_id':'1090000000011','title':title,'price':2500,'description':'轻微划痕，功能正常',
            'display_region':'厦门','item_url':'https://www.goofish.com/item?id=1090000000011',**changes}


@pytest.mark.parametrize('text', ['美版无锁','美国版','日版','日本版','韩版','欧版','欧洲版','港版','香港版','港澳版',
                                 '台版','加版','海外版','外版','水货','国际版','新加坡版','非国行','美\u200b版'])
def test_clear_overseas_phone_is_filtered_from_title(text):
    code,reason=scope_rejection(item(title=f'iPhone 15 Pro 256G {text}'),SmartConfig())
    assert code=='variant_filtered' and '版本' in reason


@pytest.mark.parametrize('text', ['国行','未说明版本','不是美版，国行','非美版，国行','不确定是否美版','可能是日版',
                                 '国行，换了美版原装屏','国行，换过美版电池','国行，送美版手机壳',
                                 '国行，不收美版','国行，支持日版配件'])
def test_unknown_denied_uncertain_or_component_versions_are_not_filtered(text):
    assert scope_rejection(item(title=f'iPhone 15 Pro 256G {text}'),SmartConfig()) is None


@pytest.mark.parametrize('text', [
    '美版和国行都有，版本需确认',
    '美版有面容解锁，美版双卡双待全网通5g\n国行双卡双待插卡直接使用',
    '国行双卡双待\n美国版无锁，插卡即用',
    '港版、国行均有现货',
])
@pytest.mark.parametrize('detail', [False, True])
def test_domestic_claim_does_not_cancel_explicit_blocked_versions(text,detail):
    raw=item(description=text) if detail else item(title=f'iPhone 15 Pro 256G {text}')
    code,reason=scope_rejection(raw,SmartConfig(),detail=detail)
    assert code=='variant_filtered' and ('美版' in reason or '港澳版' in reason)


def test_mixed_versions_still_respect_selected_exclusions():
    raw=item(title='iPhone 15 Pro 256G 美版，国行双卡双待')
    assert scope_rejection(raw,SmartConfig(blocked_variants=['港澳版'])) is None
    assert scope_rejection(raw,SmartConfig(blocked_variants=['美版']))[0]=='variant_filtered'
    assert scope_rejection(raw,SmartConfig(exclude_overseas=False)) is None


def test_enabled_toggle_selected_versions_and_detail_parameters():
    us=item(title='iPhone 15 Pro 256G 美版')
    assert scope_rejection(us,SmartConfig(exclude_overseas=False)) is None
    assert scope_rejection(us,SmartConfig(blocked_variants=['日版'])) is None
    raw=item(detail_parameters=['版本：美版'])
    assert scope_rejection(raw,SmartConfig()) is None
    assert scope_rejection(raw,SmartConfig(),detail=True)[0]=='variant_filtered'
    assert scope_rejection(item(title='iPhone 15 Pro 256G 日版海外版'),SmartConfig(blocked_variants=['美版'])) is None


@pytest.mark.parametrize('seller', ['示例通讯','示例通信','示例手机专卖','示例手机店','二手机优选铺','手机回收置换',
                                   '卖手机的示例','示例严选数码…'])
def test_business_nickname_patterns_are_filtered(seller):
    rejection=scope_rejection(item(seller_name=seller),SmartConfig())
    assert rejection[0]=='seller_filtered' and seller in rejection[1]


@pytest.mark.parametrize('seller', ['', '普通个人', '玩数码的朋友', '电器数码爱好者', '爱玩数码的人', '数码追梦人',
                                   '示例数码', '示例数码1号店', '示例数码厦门店', '科技馆可乐', '示例科技', '喜欢手机的我', '二手车专卖', '数码…'])
def test_nickname_unknown_hobbyist_and_generic_technology_are_retained(seller):
    assert scope_rejection(item(seller_name=seller,is_fish_shop=True),SmartConfig()) is None
    assert scope_rejection(item(description='在示例通讯买的手机，个人自用'),SmartConfig(),detail=True) is None


def test_nickname_keyword_editing_literal_matching_normalization_and_toggles():
    assert scope_rejection(item(seller_name='示\u200b例 通信'),SmartConfig())[0]=='seller_filtered'
    assert scope_rejection(item(seller_name='示例通讯'),SmartConfig(exclude_trade_sellers=False)) is None
    assert scope_rejection(item(seller_name='示例数码'),SmartConfig(seller_exclude_keywords=['示例数码']))[0]=='seller_filtered'
    config=SmartConfig(seller_exclude_keywords=['MY SHOP', 'a+b'])
    assert scope_rejection(item(seller_name='my\u200b shop'),config)[0]=='seller_filtered'
    assert scope_rejection(item(seller_name='a+b的店'),config)[0]=='seller_filtered'
    assert scope_rejection(item(seller_name='aaab的店'),config) is None
    with pytest.raises(ValidationError):SmartConfig(blocked_variants=['无效版本'])
    with pytest.raises(ValidationError):SmartConfig(seller_exclude_keywords=[' '])
    with pytest.raises(ValidationError):SmartConfig(seller_exclude_keywords=['\u200b'])
    assert SmartConfig(seller_exclude_keywords=['通讯',' 通讯 ']).seller_exclude_keywords==['通讯']


def test_list_prefilter_and_detail_pricing_use_identical_scope_decisions():
    quotes=[{'id':1,'model':'15 Pro','storage':'256','grade':'小花','price':3000}]
    config=SmartConfig()
    raw=item(seller_name='示例通讯')
    assert candidate_decision(raw,Rules(),quotes,config,True,'256','15 Pro').code=='seller_filtered'
    raw=item(description='轻微划痕，美版无锁')
    assert candidate_decision(raw,Rules(),quotes,config,True,'256','15 Pro').matched
    facts=extract_local(raw,config,quotes,Rules())
    result=price_facts(raw,facts,quotes,[],config,Rules(),'15 Pro','256')
    assert result['rejection_code']=='variant_filtered' and result['status']=='rejected'


@pytest.mark.parametrize('smart_enabled',[False,True])
@pytest.mark.parametrize('raw,code',[(item(seller_name='示例通讯'),'seller_filtered'),
                                   (item(description='轻微划痕，美版无锁'),'variant_filtered'),
                                   (item(title='iPhone 15 Pro 256G 美版有面容解锁',
                                         description='美版双卡双待全网通5g\n国行双卡双待插卡直接使用'),
                                    'variant_filtered')])
async def test_scope_filters_block_notifications_in_fixed_and_description_modes(database,smart_enabled,raw,code):
    database.state_set('smart_config',SmartConfig(enabled=smart_enabled).model_dump())
    add_quotes(database,[('15 Pro','256','小花',3000)])
    class Notifier:
        async def send(self,*args):raise AssertionError('Excluded item must never notify')
    service=MonitorService(database,None,Notifier())
    result=await service.process_items([raw],Rules(),'iPhone 15 Pro',True,'256',expected_model='15 Pro')
    assert result['notified']==result['matched']==0
    assert result['decisions'][0]['code']==code


def test_run_card_keeps_list_seller_for_the_saved_filter_reason(database):
    database.state_set('smart_config',SmartConfig().model_dump())
    add_quotes(database,[('15 Pro','256','小花',3000)])
    from app_monitor.db import utc_now
    with database.connect() as c:
        run_id=c.execute("INSERT INTO search_runs(model,keyword,started_at) VALUES('15 Pro','iPhone 15 Pro',?)",(utc_now(),)).lastrowid
    service=MonitorService(database,None,None)
    service._record_cards(run_id,[item(seller_name='示例通讯')],Rules(),{'storage':'256','filter_applied':True,'region_applied':True},'15 Pro')
    with database.connect() as c:
        row=c.execute('select code,decision from run_items where run_id=?',(run_id,)).fetchone()
    assert row['code']=='seller_filtered' and '示例通讯' in row['decision']


@pytest.mark.parametrize('raw,old_config',[
    (item(seller_name='示例通讯'),SmartConfig(exclude_trade_sellers=False)),
    (item(title='iPhone 15 Pro 256G 美版无锁',description='轻微划痕，国行双卡双待'),
     SmartConfig(exclude_overseas=False)),
])
async def test_changed_scope_blocks_delivery_of_previously_assessed_item(database,raw,old_config):
    add_quotes(database,[('15 Pro','256','小花',3000)])
    database.state_set('smart_config',old_config.model_dump())
    record=await MonitorService(database,None,None).smart.assess(raw,Rules(),'15 Pro','256',True)
    assert record['status'] in {'pending','matched'}
    database.state_set('smart_config',SmartConfig().model_dump())
    class Notifier:
        async def send(self,*args):raise AssertionError('New scope must also apply to old assessments')
    assert await MonitorService(database,None,Notifier()).publish_smart(record,Rules()) == (False,False)
    with database.connect() as c:
        assert c.execute('SELECT notification_status FROM smart_assessments WHERE id=?',(record['id'],)).fetchone()[0]=='none'
        assert c.execute('SELECT count(*) FROM smart_notifications').fetchone()[0]==0


async def test_scope_settings_api_roundtrip_and_validation(database,monkeypatch):
    from app_monitor import api
    from fastapi.testclient import TestClient
    service=MonitorService(database,None,None)
    monkeypatch.setattr(api,'monitor_service',service)
    initial=await api.smart_config()
    assert initial['exclude_overseas'] and initial['exclude_trade_sellers']
    fields=SmartConfig(exclude_overseas=False,blocked_variants=['美版','日版'],
                       seller_exclude_keywords=['示例通讯','示例数码']).model_dump()
    await api.save_smart_config(api.SmartConfigPayload(**fields))
    saved=await api.smart_config()
    assert all(saved[key]==value for key,value in fields.items())
    client=TestClient(api.app)
    assert client.put('/api/smart/config',json={**fields,'blocked_variants':['无效']}).status_code==422
    assert client.put('/api/smart/config',json={**fields,'seller_exclude_keywords':['\u200b']}).status_code==422
    monkeypatch.setattr(api,'monitor_service',SimpleNamespace(busy=True,smart=service.smart))
    assert (await api.save_smart_config(api.SmartConfigPayload(**fields)))['blocked_variants']==['美版','日版']


@pytest.mark.parametrize('enabled', [False, True])
def test_retired_digital_shop_rule_is_ignored_in_saved_config(enabled):
    config=SmartConfig.model_validate({'exclude_digital_shop_names':enabled,'seller_exclude_keywords':['通讯']})
    assert 'exclude_digital_shop_names' not in config.model_dump()
    assert scope_rejection(item(seller_name='示例数码厦门店'),config) is None
    assert scope_rejection(item(seller_name='示例通讯'),config)[0]=='seller_filtered'
