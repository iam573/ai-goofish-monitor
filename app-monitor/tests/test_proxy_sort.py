"""Exercise sort confirmation against actual proxy queues, including response races."""
import threading
import time
from types import SimpleNamespace

import pytest

from app_monitor.iphone_collector import _VerificationRequired
from app_monitor.iphone_proxy_collector import IPhoneProxyCollector
from app_monitor.proxy_capture import ProxyCapture

IP='192.168.1.20'
DRIVER=SimpleNamespace(page_source='<Application/>')


def event(**changes):
    return {'event':'search','client_ip':IP,'keyword':'iPhone 15','request_started':time.time(),
            'sort_field':'create','sort_value':'desc','filter_fingerprint':'same-filters','page':1,
            'items':[{'item_id':'1090000000001'}],'has_more':False,**changes}


def setup(tmp_path):
    proxy=ProxyCapture(SimpleNamespace(proxy_runtime=tmp_path));proxy.configure([IP])
    real=proxy.wait
    proxy.wait=lambda *a,timeout=18,**kw:real(*a,timeout=min(timeout,.2),**kw)
    return proxy,IPhoneProxyCollector(SimpleNamespace(),proxy)


def test_intermediate_default_response_waits_for_correct_sort_without_another_click(tmp_path,monkeypatch):
    proxy,collector=setup(tmp_path);since=time.time()
    monkeypatch.setattr(collector,'_sort_new',lambda *_:pytest.fail('No toggle for an intermediate response'))
    with proxy.session(IP):
        proxy.ingest(event(sort_field='',sort_value='',items=[{'item_id':'wrong'}]))
        timer=threading.Timer(.02,lambda:proxy.ingest(event()))
        timer.start()
        try:items,count=collector._pages(DRIVER,IP,'iPhone 15',since,0)
        finally:timer.join()
    assert items==[{'item_id':'1090000000001'}] and count==1


@pytest.mark.parametrize('old_sort',[('', ''),('create','asc')])
def test_late_old_response_does_not_override_newer_request(tmp_path,monkeypatch,old_sort):
    proxy,collector=setup(tmp_path);since=time.time()-1
    monkeypatch.setattr(collector,'_sort_new',lambda *_:pytest.fail('Newest request already confirms desc'))
    with proxy.session(IP):
        proxy.ingest(event(request_started=since+.5))
        proxy.ingest(event(request_started=since+.1,sort_field=old_sort[0],sort_value=old_sort[1],items=[{'item_id':'wrong'}]))
        items,_=collector._pages(DRIVER,IP,'iPhone 15',since,0)
    assert items==[{'item_id':'1090000000001'}]


@pytest.mark.parametrize('earlier_desc',[False,True])
def test_confirmed_ascending_is_corrected_once_using_a_fresh_request(tmp_path,monkeypatch,earlier_desc):
    proxy,collector=setup(tmp_path);since=time.time()-1;clicks=[];actions=[]
    def click(*_):
        clicks.append(True)
        proxy.ingest(event(items=[{'item_id':'fresh-desc'}]))
    monkeypatch.setattr(collector,'_sort_new',click)
    with proxy.session(IP):
        if earlier_desc:proxy.ingest(event(request_started=since+.1,items=[{'item_id':'old-desc'}]))
        proxy.ingest(event(request_started=since+.2,sort_value='asc',items=[{'item_id':'wrong'}]))
        items,_=collector._pages(DRIVER,IP,'iPhone 15',since,0,actions=actions)
    assert clicks==[True] and items==[{'item_id':'fresh-desc'}]
    assert [a['action'] for a in actions]==['纠正新发排序','确认新发排序']


@pytest.mark.parametrize('field,value,click_count',[('create','asc',1),('price','desc',0)])
def test_sort_still_wrong_stops_with_actual_values_and_no_retry_loop(tmp_path,monkeypatch,field,value,click_count):
    proxy,collector=setup(tmp_path);since=time.time();clicks=[]
    monkeypatch.setattr(collector,'_sort_new',lambda *_:clicks.append(True))
    with proxy.session(IP):
        proxy.ingest(event(sort_field=field,sort_value=value))
        with pytest.raises(RuntimeError,match=f'实际 {field} / {value}'):
            collector._pages(DRIVER,IP,'iPhone 15',since,0)
    assert len(clicks)==click_count


def test_delayed_verification_from_original_request_still_stops_sort_correction(tmp_path,monkeypatch):
    proxy,collector=setup(tmp_path);since=time.time()-1;old=since+.1
    def click(*_):proxy.ingest(event(event='verification',request_started=old,keyword='',sort_value=''))
    monkeypatch.setattr(collector,'_sort_new',click)
    with proxy.session(IP):
        proxy.ingest(event(request_started=old,sort_value='asc'))
        with pytest.raises(_VerificationRequired):collector._pages(DRIVER,IP,'iPhone 15',since,0)


@pytest.mark.parametrize('current_sort',[True,False])
def test_capacity_submission_already_in_new_sort_needs_no_repeat_tap(tmp_path,monkeypatch,current_sort):
    proxy,collector=setup(tmp_path);clicks=[];actions=[]
    def capacity(*_):
        collector._storage_requested_at=time.time()
        proxy.ingest(event(sort_field='create' if current_sort else '',sort_value='desc' if current_sort else ''))
        return ['256GB','确定']
    def click(*_):clicks.append(True);proxy.ingest(event())
    monkeypatch.setattr(collector,'_apply_storage',capacity)
    monkeypatch.setattr(collector,'_sort_new',click)
    with proxy.session(IP):items,_=collector._prepare_pages(DRIVER,IP,'iPhone 15','256',0,actions)
    assert items==[{'item_id':'1090000000001'}] and len(clicks)==int(not current_sort)
    assert actions[0]['action']=='容量筛选'
    if current_sort:assert any(a['action']=='保持新发排序' for a in actions)


def test_unchanged_capacity_refreshes_and_bounds_direction_correction(tmp_path,monkeypatch):
    proxy,collector=setup(tmp_path);clicks=[]
    monkeypatch.setattr(collector,'_apply_storage',lambda *_:['已确认 256GB'])
    def click(*_):
        clicks.append(True)
        proxy.ingest(event(sort_value='asc' if len(clicks)==1 else 'desc'))
    monkeypatch.setattr(collector,'_sort_new',click)
    with proxy.session(IP):items,_=collector._prepare_pages(DRIVER,IP,'iPhone 15','256',0,[])
    assert len(clicks)==2 and items==[{'item_id':'1090000000001'}]


def test_pagination_keeps_new_sort_even_if_filter_hash_is_equal(tmp_path,monkeypatch):
    proxy,collector=setup(tmp_path);since=time.time()
    def drag(*_):
        proxy.ingest(event(sort_field='price',items=[{'item_id':'wrong-sort'}]))
        proxy.ingest(event(page=2,items=[{'item_id':'second-page'}]))
    monkeypatch.setattr(collector,'_drag_list',drag)
    with proxy.session(IP):
        proxy.ingest(event(has_more=True))
        items,count=collector._pages(DRIVER,IP,'iPhone 15',since,1)
    assert items==[{'item_id':'1090000000001'},{'item_id':'second-page'}] and count==2


def test_capacity_change_never_reuses_previous_capacity_response(tmp_path,monkeypatch):
    proxy,collector=setup(tmp_path);clicks=[]
    def capacity(*_):
        collector._storage_requested_at=time.time()
        proxy.ingest(event(request_started=collector._storage_requested_at-.01,items=[{'item_id':'old-capacity'}]))
        proxy.ingest(event(sort_field='',sort_value='',items=[{'item_id':'intermediate'}]))
        return ['256GB','确定']
    def click(*_):clicks.append(True);proxy.ingest(event(items=[{'item_id':'fresh-capacity'}]))
    monkeypatch.setattr(collector,'_apply_storage',capacity)
    monkeypatch.setattr(collector,'_sort_new',click)
    with proxy.session(IP):items,_=collector._prepare_pages(DRIVER,IP,'iPhone 15','256',0,[])
    assert clicks==[True] and items==[{'item_id':'fresh-capacity'}]


def test_no_fresh_search_response_stops_without_blind_repeated_taps(tmp_path,monkeypatch):
    proxy,collector=setup(tmp_path)
    monkeypatch.setattr(collector,'_sort_new',lambda *_:pytest.fail('Do not blindly retry without evidence'))
    with proxy.session(IP):
        with pytest.raises(RuntimeError,match='未收到本次搜索'):
            collector._pages(DRIVER,IP,'iPhone 15',time.time(),0)
