"""Saving during a paused search must not mix old and new valuation rules."""
import asyncio

import pytest

from app_monitor import api
from app_monitor.collector import CollectResult
from app_monitor.devices import Device
from app_monitor.quotes import QuoteDeduction, _save_deductions
from app_monitor.rules import Rules, save_rules
from app_monitor.service import MonitorService
from app_monitor.smart import Policy, SmartConfig, load_config, policies_for
from conftest import add_quotes


@pytest.mark.parametrize('enabled',[False,True])
async def test_save_during_search_keeps_thread_and_stream_rules_until_next_run(database,monkeypatch,enabled):
    batch=add_quotes(database,[('15 Pro','256','小花',3000)])
    with database.connect() as c:
        _save_deductions(c,batch,'100025',[QuoteDeduction('15 Pro',1,0,'第三方-300','第三方',300,300)])
    row=policies_for(database,'15 Pro','100025')[0]
    old_policy=Policy(reviewed=True,action='ignore',field='battery_replaced',operator='eq',value='yes').model_dump()
    database.state_set('smart_policy_templates',{row['template_key']:old_policy})
    old=SmartConfig(enabled=enabled,exclude_trade_sellers=False)
    database.state_set('smart_config',old.model_dump())
    database.state_set('collector_devices',['ios:test'])
    rules=Rules(region_city='厦门',max_over_quote=200)
    save_rules(database,rules)
    monkeypatch.setattr('app_monitor.service.discover_devices',lambda:[Device('ios:test','测试手机','ios',True,'test','17.1')])
    started=asyncio.Event();resume=asyncio.Event();observed={};sent=[]
    raw={'item_id':'1090000000111','title':'iPhone 15 Pro 256G 国行 轻微划痕',
         'description':'个人自用整机，功能正常，轻微划痕，换过第三方电池','price':3100,
         'seller_name':'示例通讯','display_region':'厦门','item_url':'https://www.goofish.com/item?id=1090000000111'}
    class Notifier:
        async def send(self,item,reason):
            sent.append(item)
            return {'test':{'success':True}}
    service=MonitorService(database,None,Notifier())
    monkeypatch.setattr(api,'database',database)
    monkeypatch.setattr(api,'monitor_service',service)
    class Phone:
        async def collect(self,udid,version,keyword,city,steps,storages,pages,is_candidate,on_items=None,check_detail=None):
            started.set();await resume.wait()
            def in_phone_thread():
                observed['candidate']=is_candidate(raw,True,'256')
                observed['detail']=check_detail(raw,True,'256')
                observed['policy']=policies_for(database,'15 Pro','100025')[0]['policy']
                observed['enabled']=load_config(database).enabled
                on_items([raw],storage='256',region_applied=True,filter_applied=True)
            await asyncio.to_thread(in_phone_thread)
            return CollectResult(items=[],region_applied=True,filter_applied=True,
                passes=[{'storage':'256','filter_applied':True,'region_applied':True,'observed':1,'error':None}])
    service.iphone_collector=Phone()
    task=asyncio.create_task(service.run_model('15 Pro','256'))
    try:
        await asyncio.wait_for(started.wait(),3)
        assert service.busy
        updated=old.model_copy(update={'enabled':not enabled,'exclude_trade_sellers':True})
        saved=await api.save_smart_config(api.SmartConfigPayload(**updated.model_dump()))
        assert saved['enabled'] is not enabled and saved['exclude_trade_sellers']
        await api.save_policy_templates([api.TemplatePolicyUpdate(template_key=row['template_key'],policy=Policy(reviewed=True,field='battery_third_party',operator='eq',value='yes',group='battery_part'))])
        await api.save_smart_policies([api.PolicyUpdate(rule_key=row['rule_key'],policy=Policy(reviewed=True,field='battery_replaced',operator='eq',value='yes',group='battery_part'))],'15 Pro')
        save_rules(database,Rules(region_city='厦门',max_over_quote=0))
        # The API request sees saved changes immediately, outside the collector context.
        assert load_config(database).enabled is not enabled
        assert policies_for(database,'15 Pro','100025')[0]['policy_source']=='model'
    finally:
        resume.set()
        result=await asyncio.wait_for(task,5)
    assert observed['enabled'] is enabled and observed['policy']==old_policy
    assert observed['candidate'] and observed['detail']['matched']
    assert result['matched']==1 and len(sent)==1
    with database.connect() as c:
        row_result=c.execute('SELECT code,quote_basis FROM run_items WHERE run_id=?',(result['run_id'],)).fetchone()
    assert row_result['quote_basis']==('detail' if enabled else 'fixed')
    if enabled:
        assert sent[0]['smart_assessment']['value_high']==3000  # Saved fee changes did not affect stream valuation.
    # A new round picks up the changed seller scope and never notifies this seller.
    again=await service.run_model('15 Pro','256',injected_items=[raw],region_applied=True,filter_applied=True)
    assert again['matched']==0 and again['decisions'][0]['code']=='seller_filtered'
    assert len(sent)==1


async def test_run_settings_reset_after_failure_and_are_isolated_between_databases(database,monkeypatch,tmp_path):
    from app_monitor.db import Database
    service=MonitorService(database,None,None)
    original=SmartConfig(exclude_overseas=False)
    service.smart.save_config(original)
    other=Database(tmp_path/'other.sqlite3');other.initialize()
    try:
        with service.smart.run_settings():
            service.smart.save_config(SmartConfig())
            assert not load_config(database).exclude_overseas
            assert load_config(other).exclude_overseas
            assert service.smart.public_config()['exclude_overseas']  # Editors always see saved config.
            raise RuntimeError('collector failed')
    except RuntimeError:
        pass
    assert load_config(database).exclude_overseas
