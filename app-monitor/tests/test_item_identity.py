from types import SimpleNamespace

import pytest

from app_monitor import api
from app_monitor.item_identity import has_model_evidence
from app_monitor.local_rules import extract_local
from app_monitor.rules import Rules
from app_monitor.service import MonitorService
from app_monitor.smart import SmartConfig, candidate_decision, price_facts, load_config
from conftest import add_quotes

TOY='点绛唇佩佩系列 舞狮35 纤纤30 秋叶已出 清平已出 飞仙已出 只剩舞狮、纤纤全新未拆封 不包官瑕 单买运费5元，打包55包邮非偏远'
JUNK=[TOY,'全新扭蛋，带原装盒，高度10cm以下，不包官瑕', '紫色链条包，只能装折叠小手机',
      '无线麦克风全新未拆封，功能完好，现有3个', '手工蝴蝶结发夹，每款一个，三个39元包邮',
      '手游蓝牙手柄，适配华为手机，配件齐全','抱枕毯，包邮非偏远',
      '玩具电池车尺寸14cm，带原装盒','苹果14元一斤，水果礼盒', '全新17厘米玩偶，送电池，128G字样标签',
      '适配iPhone 14保护壳，128G版本也可用']


def listing(title,price=30,**kwargs):
    return {'item_id':'1090000000888','title':title,'description':title,'price':price,
            'display_region':'厦门','item_url':'https://www.goofish.com/item?id=1090000000888',**kwargs}


@pytest.mark.parametrize('title',JUNK)
def test_unrelated_product_cannot_inherit_model_even_when_it_is_cheap(title):
    quotes=[{'id':1,'model':'14','storage':'512','grade':'小花','price':2000}]
    raw=listing(title)
    config=SmartConfig()
    first=candidate_decision(raw,Rules(),quotes,config,True,'512','14')
    assert not first.matched and first.code=='model_unconfirmed'
    facts=extract_local(raw,config,quotes,Rules())
    final=price_facts(raw,facts,quotes,[],config,Rules(),'14','512')
    assert final['status']=='rejected' and final['rejection_code']=='model_unconfirmed'
    assert final['base_price'] is None and final['value_high'] is None


@pytest.mark.parametrize('model,title',[
    ('14','iPhone 14 512G 国行'),('14 Plus','苹果14plus 512G'),
    ('13 Pro Max','国行13p\u200bm128全原电池85'),('16 Pro','苹果16pro128G'),
    ('12 Pro','苹果12Pro纯原装128g'),('14','14 256G 国行电池85'),
    ('17 Air','iPhone Air 256G'),('17 Air','iphoneair256G'),
    ('17','iPhone17 1TB'),('XR','iPhoneXR 128G'),
    ('15 Pro','iPhone15 pro 256G 国行无拆无修，边框有手机壳轻微磨损，主板无修'),
    ('15','出自用苹果15 国行128G 全原装 无维修 功能都正常 送手机壳 电池80%'),
    ('15 Pro','苹果15pro国行256原色 全原无修，一手自用，带手机壳，电池还有76'),
    ('13 Pro','iPhone 13 Pro 128g 电池刚换 屏幕有轻微划痕贴膜就看不到'),
])
def test_real_phone_names_shorthand_and_zero_width_evidence_remain_valid(model,title):
    assert has_model_evidence(model,title)


@pytest.mark.parametrize('text', ['电池循环14次，报价128元','高度14cm，送电池','手机包14寸，送电池',
                                  '华为14 Pro 256G','适用于iPhone14 Pro Max','iPhone14手机壳','',
                                  '喜欢可以私聊我，质量有保证'])
def test_incidental_numbers_and_model_compatibility_do_not_establish_identity(text):
    assert not has_model_evidence('14',text)


def test_detail_and_parameters_can_establish_identity_without_fabricating_it():
    raw=listing('个人闲置手机',price=1900,detail_parameters=['品牌：Apple','机型：iPhone 14','容量：512GB'])
    quotes=[{'id':1,'model':'14','storage':'512','grade':'小花','price':2000}]
    config=SmartConfig();facts=extract_local(raw,config,quotes,Rules())
    assert facts.findings['model'].value=='14'
    assert candidate_decision(raw,Rules(),quotes,config,True,'512','14').matched
    assert price_facts(raw,facts,quotes,[],config,Rules(),'14','512')['status']=='pending'
    assert raw['title']=='个人闲置手机'


def test_title_model_is_kept_when_description_does_not_repeat_model_or_capacity():
    raw=listing('iPhone 14',price=1900,description='轻微划痕，功能正常，无拆修')
    quotes=[{'id':1,'model':'14','storage':'512','grade':'小花','price':2000}]
    facts=extract_local(raw,SmartConfig(),quotes,Rules())
    assert facts.findings['model'].value=='14'
    result=price_facts(raw,facts,quotes,[],SmartConfig(),Rules(),'14','512')
    assert result['status']=='pending' and result['value_high']==2000  # Capacity still needs confirmation.


@pytest.mark.parametrize('enabled',[False,True])
async def test_irrelevant_products_never_notify_but_real_pending_phone_still_does(database,enabled):
    add_quotes(database,[('14','512','小花',2000)])
    database.state_set('smart_config',SmartConfig(enabled=enabled).model_dump())
    class Notifier:
        def __init__(self):self.sent=[]
        async def send(self,item,reason):self.sent.append(item);return {'test':{'success':True}}
    notifier=Notifier();service=MonitorService(database,None,notifier)
    result=await service.process_items([listing(TOY)],Rules(),'iPhone 14',True,'512',expected_model='14')
    assert not notifier.sent and result['notified']==0
    if enabled:
        good=listing('iPhone 14 512G 国行 功能正常',price=1900,item_id='1090000000889')
        result=await service.process_items([good],Rules(),'iPhone 14',True,'512',expected_model='14')
        assert result['notified']==1 and notifier.sent[0]['smart_assessment']['status']=='pending'


async def test_cached_old_pending_record_cannot_bypass_publish_gate_or_hide_sent_audit(database,monkeypatch):
    from app_monitor import smart
    add_quotes(database,[('14','512','小花',2000)])
    service=MonitorService(database,None,None)
    raw=listing(TOY)
    # Seed a pre-fix assessment with the old erroneous result.
    old={'status':'pending','model':'14','storage':'512','reason':'旧搜索上下文兜底','value_low':None,
         'value_high':2000,'base_price':2000,'threshold_high':2200,'uncertainties':[]}
    monkeypatch.setattr(smart,'assess_local',lambda *a,**k:(smart.Facts(),dict(old)))
    monkeypatch.setattr(smart,'candidate_decision',lambda *a,**k:SimpleNamespace(matched=True))
    record=await service.smart.assess(raw,Rules(),'14','512',True)
    class Notifier:
        async def send(self,*a):pytest.fail('Old candidates must also be rejected')
    service.notifications=Notifier()
    with database.connect() as c:c.execute("UPDATE smart_assessments SET notification_status='sent' WHERE id=?",(record['id'],))
    assert await service.publish_smart(record,Rules())==(False,False)
    with database.connect() as c:
        assert c.execute('SELECT notification_status FROM smart_assessments WHERE id=?',(record['id'],)).fetchone()[0]=='sent'
    monkeypatch.setattr(api,'database',database);monkeypatch.setattr(api,'monitor_service',service)
    assert not await api.smart_assessments('pending',100)
    assert (await api.smart_assessment(record['id']))['result']['status']=='pending'  # Historical evidence remains readable.
