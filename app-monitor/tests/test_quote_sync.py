import asyncio
import threading
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from app_monitor.config import settings
from app_monitor.quotes import QuoteEntry, QuoteService, latest_downloaded_batch, quote_status


@pytest.fixture
def service(database,monkeypatch):
    service=QuoteService(database,replace(settings,database=database.path))
    async def payload(*args,**kwargs):return {'data':'https://example.test/20261021/quote.png'}
    async def download(*args):return b'test-quote-image'
    monkeypatch.setattr(service,'_get_json',payload)
    monkeypatch.setattr(service,'_download',download)
    return service


def good_parse(path):
    assert path.is_file()
    return [QuoteEntry('17','256','靓机',5200)],[]


async def test_new_quote_api_sends_public_context_without_login_headers(database, monkeypatch):
    import httpx

    calls = []
    def respond(request):
        calls.append(request)
        if request.url.path.endswith('/getQuoteImage'):
            return httpx.Response(200, json={'code': 200, 'data': 'https://cdn.example.test/20261022/latest.png'})
        if request.url.path.endswith('/categoryTree'):
            return httpx.Response(200, json={'code': 200, 'data': [
                {'categoryId': '100025', 'categoryName': '苹果无保', 'status': 1},
            ]})
        return httpx.Response(200, content=b'image' * 300, headers={'Content-Type': 'image/png'})

    client_type = httpx.AsyncClient
    transport = httpx.MockTransport(respond)
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: client_type(transport=transport, **kwargs))
    quote_settings = replace(settings, database=database.path,
                             quote_api_root='https://api.zuphone.com/biji-product/api/home',
                             quote_app_id='wx5d70a654307f7bbf', quote_platform='weapp-lite-aiji-report')
    service = QuoteService(database, quote_settings)
    monkeypatch.setattr(service.parser, 'parse_image', good_parse)
    assert (await service.sync('100025', '苹果无保'))['entry_count'] == 1
    assert (await service.categories())[0]['category_id'] == '100025'
    for request in calls:
        assert not any(name in request.headers for name in ['Cookie', 'Authorization', 'wzw-access-token'])
        if request.url.host == 'api.zuphone.com':
            assert request.url.params['appid'] == quote_settings.quote_app_id
            assert request.url.params['platform'] == quote_settings.quote_platform
            assert request.headers['wzw-app-id'] == quote_settings.quote_app_id
            assert request.headers['wzw-client-type'] == quote_settings.quote_platform
        else:
            assert 'wzw-app-id' not in request.headers
    assert calls[0].url.params['categoryId'] == '100025'


@pytest.mark.parametrize('new_url,new_content,changed', [
    ('https://example.test/20261022/revised.png', b'new-image', True),
    ('https://example.test/20261022/revised.png', b'original-image', False),
    ('https://example.test/20261022/original.png', b'new-image', True),
])
async def test_intraday_checks_follow_latest_url_and_compare_image_content(
    database, service, monkeypatch, new_url, new_content, changed,
):
    from app_monitor.quotes import latest_entries

    monkeypatch.setattr('app_monitor.quotes.APPLE_PHONE_CATEGORIES', {'100025': '苹果无保'})
    revision = {'url': 'https://example.test/20261022/original.png', 'content': b'original-image', 'price': 5200}
    requests = []
    parses = []
    async def payload(*args, **kwargs):
        requests.append(kwargs['params']['categoryId'])
        return {'data': revision['url']}
    async def download(url):
        assert url == revision['url']
        return revision['content']
    def parse(path):
        parses.append(path)
        return [QuoteEntry('17', '256', '靓机', revision['price'])], []
    monkeypatch.setattr(service, '_get_json', payload)
    monkeypatch.setattr(service, '_download', download)
    monkeypatch.setattr(service.parser, 'parse_image', parse)
    first = (await service.sync_if_stale())[0]
    assert quote_status(database)[0]['fresh']
    revision.update(url=new_url, content=new_content, price=4900)
    second = (await service.sync_if_stale())[0]
    assert second['updated'] is changed
    assert requests == ['100025', '100025']
    assert len(parses) == (2 if changed else 1)
    assert latest_downloaded_batch(database, '100025')['image_url'] == new_url
    assert latest_entries(database, '100025')[0]['price'] == (4900 if changed else 5200)
    assert (first['batch_id'] != second['batch_id']) is changed
    assert database.state_get('quote_last_check')['results'][0]['updated'] is changed


async def test_first_sync_parses_then_only_valid_nonempty_data_is_unchanged(database,service,monkeypatch):
    calls=[]
    def parse(path):calls.append(path);return good_parse(path)
    monkeypatch.setattr(service.parser,'parse_image',parse)
    first=await service.sync('100025','苹果无保')
    second=await service.sync('100025','苹果无保')
    assert first['updated'] and first['parse_status']=='parsed' and first['entry_count']==1
    assert second['updated'] is False and second['entry_count']==1
    assert len(calls)==1
    with database.connect() as c:assert c.execute('SELECT COUNT(*) FROM quote_batches').fetchone()[0]==1


@pytest.mark.parametrize('status',['failed','parsing','needs_review','parsed'])
async def test_same_image_with_incomplete_or_empty_data_retries_same_batch(database,service,monkeypatch,status):
    monkeypatch.setattr(service.parser,'parse_image',lambda _: ([],[]))
    initial=await service.sync('100025','苹果无保')
    with database.connect() as c:
        c.execute('UPDATE quote_batches SET parse_status=? WHERE id=?',(status,initial['batch_id']))
    monkeypatch.setattr(service.parser,'parse_image',good_parse)
    recovered=await service.sync('100025','苹果无保')
    assert recovered['updated'] and recovered['reparsed'] and recovered['entry_count']==1
    assert recovered['batch_id']==initial['batch_id'] and recovered['parse_status']=='parsed'
    with database.connect() as c:assert c.execute('SELECT COUNT(*) FROM quote_batches').fetchone()[0]==1


async def test_first_ocr_failure_retains_image_error_and_can_recover_preserving_manual_price(database,service,monkeypatch):
    def fail(_):raise RuntimeError('Vision OCR test failure')
    monkeypatch.setattr(service.parser,'parse_image',fail)
    with pytest.raises(RuntimeError,match='Vision OCR'):await service.sync('100025','苹果无保')
    batch=latest_downloaded_batch(database,'100025')
    assert batch['parse_status']=='failed' and Path(batch['image_path']).is_file()
    status=quote_status(database)[0]
    assert status['image_available'] and status['parse_status']=='failed' and not status['fresh']
    assert 'Vision OCR test failure' in status['parse_error']
    with database.connect() as c:
        c.execute("""INSERT INTO quote_entries(batch_id,category_id,model,storage,grade,price,source,enabled,updated_at)
                     VALUES(?,'100025','17','256','靓机',5100,'manual',1,'2026-10-21')""",(batch['id'],))
    monkeypatch.setattr(service.parser,'parse_image',good_parse)
    recovered=await service.sync('100025','苹果无保')
    assert recovered['entry_count']==1 and recovered['parse_error']==''
    with database.connect() as c:
        row=c.execute('SELECT price,source FROM quote_entries').fetchone()
        assert tuple(row)==(5100,'manual')


async def test_zero_entries_does_not_satisfy_daily_refresh(database,service,monkeypatch):
    monkeypatch.setattr(service.parser,'parse_image',lambda _: ([],[]))
    result=await service.sync('100025','苹果无保')
    assert result['parse_status']=='needs_review' and result['entry_count']==0 and result['parse_error']
    assert not quote_status(database)[0]['fresh']
    synced=[]
    async def retry(category,*args):synced.append(category);return {'updated':False}
    monkeypatch.setattr(service,'sync',retry)
    await service.sync_if_stale(datetime.now().astimezone().date().isoformat())
    assert synced==['100025','100027']


async def test_startup_and_manual_sync_wait_for_same_inflight_parse(database,service,monkeypatch):
    started=threading.Event();release=threading.Event();calls=[]
    def parse(path):
        calls.append(1);started.set()
        if not release.wait(4):raise RuntimeError('Test parse timed out')
        return good_parse(path)
    monkeypatch.setattr(service.parser,'parse_image',parse)
    first=asyncio.create_task(service.sync('100025','苹果无保'))
    second=None
    try:
        assert await asyncio.to_thread(started.wait,3)
        second=asyncio.create_task(service.sync('100025','苹果无保'))
        await asyncio.sleep(0)
        assert not second.done()
    finally:
        release.set()
        if second is None:await first
    results=await asyncio.gather(first,second)
    assert [r['updated'] for r in results]==[True,False] and len(calls)==1
    assert all(r['entry_count']==1 and r['parse_status']=='parsed' for r in results)


async def test_reparse_latest_includes_failed_batch(database,service,monkeypatch):
    def fail(_):raise RuntimeError('first parse failed')
    monkeypatch.setattr(service.parser,'parse_image',fail)
    with pytest.raises(RuntimeError):await service.sync('100025','苹果无保')
    monkeypatch.setattr(service.parser,'parse_image',good_parse)
    results=await service.reparse_latest()
    assert len(results)==1 and results[0]['parse_status']=='parsed' and results[0]['entry_count']==1


async def test_moved_or_missing_original_is_restored_even_when_price_data_unchanged(database,service,monkeypatch):
    monkeypatch.setattr(service.parser,'parse_image',good_parse)
    result=await service.sync('100025','苹果无保')
    Path(result['image_path']).unlink()
    with database.connect() as c:
        c.execute("UPDATE quote_batches SET image_path='/previous-mac/quote.png'")
    result=await service.sync('100025','苹果无保')
    assert result['updated'] is False
    batch=latest_downloaded_batch(database,'100025')
    assert Path(batch['image_path']).is_file() and str(database.path.parent) in batch['image_path']


def test_existing_database_adds_parse_error_without_losing_batches(tmp_path):
    import sqlite3
    from app_monitor.db import Database,SCHEMA
    path=tmp_path/'legacy.sqlite3'
    with sqlite3.connect(path) as c:
        c.executescript(SCHEMA.replace("    parse_error TEXT NOT NULL DEFAULT '',\n",''))
        c.execute("""INSERT INTO quote_batches(category_id,category_name,image_url,image_path,image_sha256,fetched_at,parse_status)
                     VALUES('100025','苹果无保','https://example.test/q.png','/old/q.png','legacy','2026-10-21','failed')""")
    migrated=Database(path);migrated.initialize()
    with migrated.connect() as c:
        row=c.execute('SELECT parse_status,parse_error,image_sha256 FROM quote_batches').fetchone()
        assert tuple(row)==('failed','','legacy')
