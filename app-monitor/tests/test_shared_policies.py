from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app_monitor import api
from app_monitor.local_rules import extract_local
from app_monitor.quotes import QuoteDeduction, _save_deductions, latest_entries, parse_deduction_text
from app_monitor.rules import Rules
from app_monitor.smart import Policy, SmartConfig, policies_for, policy_templates_for, policy_template_key, price_facts
from conftest import add_quotes


def seed(database, texts=('换屏-100', '换屏-300'), category='100025', quote_date='2026-10-19'):
    batch=add_quotes(database,[(model,'256','小花',3000) for model in ('14','15')],category,quote_date)
    deductions=[]
    for model, text in zip(('14','15'),texts):
        name,lo,hi,more,kind=parse_deduction_text(text)
        deductions.append(QuoteDeduction(model,0,0,text,name,lo,hi,more,kind))
    with database.connect() as c:
        _save_deductions(c,batch,category,deductions)


def test_same_note_uses_shared_judgment_but_each_models_own_fee(database):
    seed(database)
    key=policy_template_key('100025','换屏')
    policy=Policy(reviewed=True,field='battery_replaced',operator='eq',value='yes',group='battery_part')
    database.state_set('smart_policy_templates',{key:policy.model_dump()})
    catalog=policy_templates_for(database,'100025')
    assert len(catalog['items'])==1 and catalog['items'][0]['models']==['14','15']
    for model, fee in [('14',100),('15',300)]:
        rows=policies_for(database,model,'100025')
        assert rows[0]['policy']==policy.model_dump() and rows[0]['policy_source']=='shared'
        item={'title':f'iPhone {model} 256G','description':'轻微划痕，换过电池','price':2500}
        quotes=latest_entries(database,'100025')
        facts=extract_local(item,SmartConfig(),quotes,Rules())
        result=price_facts(item,facts,quotes,rows,SmartConfig(),Rules(),model,'256')
        assert result['value_high']==3000-fee and result['deductions'][0]['min']==fee


def test_amount_only_updates_keep_shared_rule_and_new_amount(database):
    seed(database)
    key=policy_template_key('100025','换屏')
    policy=Policy(reviewed=True,action='ignore').model_dump()
    database.state_set('smart_policy_templates',{key:policy})
    seed(database,('换屏-150','换屏-350'),quote_date='2026-10-20')
    for model,fee in [('14',150),('15',350)]:
        row=policies_for(database,model,'100025')[0]
        assert row['template_key']==key and row['policy']==policy and row['amount_min']==fee


def test_thresholds_capacity_scope_and_quote_category_are_distinct():
    names=['电池低于70%','电池低于80%','256g蓝色','512g蓝色','靓机换屏','换屏']
    assert len({policy_template_key('100025',name) for name in names})==len(names)
    assert policy_template_key('100025','换屏')!=policy_template_key('100027','换屏')
    assert policy_template_key('100025','  换 屏 ')==policy_template_key('100025','换屏')


def test_true_legacy_model_override_wins_but_saved_automatic_rows_do_not(database):
    seed(database)
    fourteen=policies_for(database,'14','100025')[0]
    fifteen=policies_for(database,'15','100025')[0]
    shared=Policy(reviewed=True,action='ignore').model_dump()
    override=Policy(reviewed=True,field='screen_replaced',value='no',operator='eq').model_dump()
    database.state_set('smart_policy_templates',{fourteen['template_key']:shared})
    database.state_set('smart_policies',{fourteen['rule_key']:override,fifteen['rule_key']:fifteen['policy']})
    assert policies_for(database,'14','100025')[0]['policy']==override
    assert policies_for(database,'15','100025')[0]['policy']==shared
    assert policy_templates_for(database,'100025')['items'][0]['override_models']==['14']


@pytest.fixture
def api_db(database,monkeypatch):
    monkeypatch.setattr(api,'database',database)
    monkeypatch.setattr(api,'monitor_service',SimpleNamespace(busy=False))
    seed(database)
    return database


async def test_api_shared_save_single_model_exception_and_reset(api_db):
    catalog=await api.smart_policy_templates()
    assert catalog['models']==['14','15'] and 'amount_min' not in catalog['items'][0]
    key=catalog['items'][0]['template_key']
    common=Policy(reviewed=True,action='ignore')
    await api.save_policy_templates([api.TemplatePolicyUpdate(template_key=key,policy=common)])
    row=(await api.smart_policies('14'))['items'][0]
    assert row['policy_source']=='shared'
    await api.save_smart_policies([api.PolicyUpdate(rule_key=row['rule_key'],policy=Policy(reviewed=True,field='screen_replaced',value='yes',operator='eq'))],'14')
    assert policies_for(api_db,'14','100025')[0]['policy_source']=='model'
    assert policies_for(api_db,'15','100025')[0]['policy_source']=='shared'
    await api.save_smart_policies([api.PolicyUpdate(rule_key=row['rule_key'],policy=None)],'14')
    assert policies_for(api_db,'14','100025')[0]['policy_source']=='shared'
    await api.save_policy_templates([api.TemplatePolicyUpdate(template_key=key,policy=None)])
    assert policies_for(api_db,'14','100025')[0]['policy_source']=='automatic'


async def test_api_rejects_stale_keys_and_incomplete_rules_without_mutating(api_db):
    for update in [api.TemplatePolicyUpdate(template_key='f'*64,policy=Policy()),
                   api.TemplatePolicyUpdate(template_key=policy_template_key('100025','换屏'),policy=Policy(reviewed=True,value='nan'))]:
        with pytest.raises(HTTPException) as exc:
            await api.save_policy_templates([update])
        assert exc.value.status_code in (409,422)
    assert not api_db.state_get('smart_policy_templates',{})
    api.monitor_service.busy=True
    update=api.TemplatePolicyUpdate(template_key=policy_template_key('100025','换屏'),policy=Policy(reviewed=True,action='ignore'))
    assert (await api.save_policy_templates([update]))['items'][0]['policy_source']=='shared'
    row=(await api.smart_policies('14'))['items'][0]
    await api.save_smart_policies([api.PolicyUpdate(rule_key=row['rule_key'],policy=Policy(reviewed=True,field='screen_replaced',operator='eq',value='yes'))],'14')
    assert policies_for(api_db,'14','100025')[0]['policy_source']=='model'


async def test_shared_money_cannot_be_invented_for_notes_without_amount(api_db):
    seed(api_db,('未知说明','未知说明'),quote_date='2026-10-20')
    row=(await api.smart_policy_templates())['items'][0]
    assert not row['has_amount']
    with pytest.raises(HTTPException) as exc:
        await api.save_policy_templates([api.TemplatePolicyUpdate(template_key=row['template_key'],policy=Policy(reviewed=True))])
    assert exc.value.status_code==422


async def test_saving_unchanged_inherited_rows_does_not_create_model_exceptions(api_db):
    rows=(await api.smart_policies('14'))['items']
    await api.save_smart_policies([api.PolicyUpdate(rule_key=r['rule_key'],policy=Policy.model_validate(r['policy'])) for r in rows],'14')
    assert not api_db.state_get('smart_policies',{})
    key=rows[0]['template_key']
    await api.save_policy_templates([api.TemplatePolicyUpdate(template_key=key,policy=Policy(reviewed=True,action='ignore'))])
    assert policies_for(api_db,'14','100025')[0]['policy_source']=='shared'
