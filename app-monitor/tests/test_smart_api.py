from fastapi.testclient import TestClient
from app_monitor import api
from app_monitor.smart import SmartConfig, Facts, Policy, policies_for
from app_monitor.rules import Rules, save_rules
from conftest import add_quotes
from test_smart import ITEM, facts


async def noop():pass


def test_smart_configuration_policies_reanalysis_and_review(tmp_path,monkeypatch):
    monkeypatch.setattr(api.database,'path',tmp_path/'smart-api.sqlite3')
    monkeypatch.setattr(api,'refresh_quotes_if_stale',noop)
    monkeypatch.setattr(api,'backfill_deductions',noop)
    monkeypatch.setattr(api.monitor_service.smart,'api_key','never-return-this')
    async def fake_request(*args):return facts()
    monkeypatch.setattr(api.monitor_service.smart,'request_facts',fake_request)
    with TestClient(api.app) as client:
        initial=client.get('/api/smart/config').json()
        assert initial['enabled'] is True and initial['ai_enabled'] is False
        assert 'never-return-this' not in client.get('/api/smart/config').text
        assert client.put('/api/smart/config',json=SmartConfig().model_dump()).status_code==200
        assert client.put('/api/smart/config',json=SmartConfig(ai_enabled=True).model_dump()).status_code==422
        payload=SmartConfig(enabled=True,shadow=True,ai_enabled=True,base_url='https://model.test/v1',model='example').model_dump()
        assert client.put('/api/smart/config',json=payload).status_code==200
        assert client.post('/api/smart/test').json()=={'ok':True}
        assert client.put('/api/smart/config',json={**payload,'base_url':'https://secret@model.test/v1'}).status_code==422
        assert client.get('/api/smart/assessments/9999').status_code==404
        batch=add_quotes(api.database,[('17','256','靓机',5200),('17','256','小花',4600)])
        with api.database.connect() as c:
            c.execute("""INSERT INTO quote_deductions(batch_id,category_id,model,text,name,amount_min,amount_max)
                VALUES(?,'100025','17','电池低于80%扣300','电池低于80%',300,300)""",(batch,))
        rows=client.get('/api/smart/policies?model=17').json()['items']
        update={'rule_key':rows[0]['rule_key'],'policy':Policy(reviewed=True).model_dump()}
        assert client.put('/api/smart/policies?model=17',json=[update]).status_code==200
        assert client.put('/api/smart/policies?model=17',json=[{**update,'rule_key':'a'*64}]).status_code==409
        # A pure monetary update preserves confirmation; semantic text changes invalidate it.
        with api.database.connect() as c:
            c.execute("UPDATE quote_deductions SET text='电池低于80%扣350',amount_min=350,amount_max=350")
        assert client.get('/api/smart/policies?model=17').json()['items'][0]['policy']['reviewed']
        result=client.post('/api/targets/17/observations?storage=256',json=[ITEM]).json()
        assert result['notified']==0 # Shadow mode must never send a real message.
        records=client.get('/api/smart/assessments').json()
        assert len(records)==1 and records[0]['snapshot']['description']==ITEM['description']
        record_id=records[0]['id']
        assert client.get('/api/smart/assessments?status=matched').json()[0]['id']==record_id
        corrected=client.put(f'/api/smart/assessments/{record_id}/correction',json=facts().model_dump()).json()
        assert corrected['source']=='manual'
        assert client.post(f"/api/smart/assessments/{corrected['id']}/review").status_code==200
        assert client.get('/api/smart/config').json()['reviewed_samples']==1
        assert client.post(f'/api/smart/assessments/{record_id}/reanalyze').status_code==200
        assert client.get('/api/smart/assessments?status=invalid').status_code==422
        with api.database.connect() as c:
            c.execute("UPDATE quote_deductions SET text='电池低于75%扣350',name='电池低于75%'")
        revised=client.get('/api/smart/policies?model=17').json()['items'][0]['policy']
        assert revised['reviewed'] and revised['value']=='75' # Recompiled semantics, not stale override.
        html=client.get('/').text
        assert 'smart-panel' not in html and 'smart-form' not in html
        assert 'valuation-controls' in html and 'valuation-detail' in html
