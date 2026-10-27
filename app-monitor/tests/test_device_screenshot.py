import io
import subprocess
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app_monitor import api
from app_monitor import device_screenshot as capture
from app_monitor.devices import Device


def png():
    buffer=io.BytesIO()
    Image.new('RGB',(8,16),'#218a62').save(buffer,format='PNG')
    return buffer.getvalue()


def test_screenshot_api_uses_requested_device_during_collection_and_disables_cache(monkeypatch):
    devices=[Device('ios:one','One','ios',True,'udid-one'),Device('ios:two','Two','ios',True,'udid-two')]
    monkeypatch.setattr(api,'discover_devices',lambda:devices)
    monkeypatch.setattr(api,'monitor_service',SimpleNamespace(busy=True))
    calls=[]
    monkeypatch.setattr(api,'capture_screenshot',lambda udid:calls.append(udid) or png())
    client=TestClient(api.app)
    response=client.get('/api/devices/ios:two/screenshot')
    assert response.status_code==200 and calls==['udid-two']
    assert response.content==png() and response.headers['content-type']=='image/png'
    assert response.headers['cache-control']=='no-store, private'
    assert response.headers['x-captured-at']
    assert response.headers['x-content-type-options']=='nosniff'


@pytest.mark.parametrize('device_id,devices,status',[
    ('ios:unknown',[],404),('mac',[Device('mac','Mac','mac',True)],404),
    ('ios:one',[Device('ios:one','One','ios',False,'one')],409),
])
def test_unknown_or_disconnected_device_is_rejected_before_capture(monkeypatch,device_id,devices,status):
    monkeypatch.setattr(api,'discover_devices',lambda:devices)
    def forbidden(*args):raise AssertionError('No screenshot for invalid device')
    monkeypatch.setattr(api,'capture_screenshot',forbidden)
    response=TestClient(api.app).get(f'/api/devices/{device_id}/screenshot')
    assert response.status_code==status


@pytest.mark.parametrize('error,status',[(capture.ScreenshotBusy('正在截图'),429),(RuntimeError('截图通道未就绪'),503)])
def test_capture_errors_are_readable_and_never_return_a_stale_image(monkeypatch,error,status):
    monkeypatch.setattr(api,'discover_devices',lambda:[Device('ios:one','One','ios',True,'one')])
    def fail(*args):raise error
    monkeypatch.setattr(api,'capture_screenshot',fail)
    response=TestClient(api.app).get('/api/devices/ios:one/screenshot')
    assert response.status_code==status and response.json()['detail']==str(error)
    assert response.headers['content-type'].startswith('application/json')


def test_capture_passes_exact_udid_to_read_only_bridge_and_validates_png(monkeypatch):
    monkeypatch.setattr(capture.shutil,'which',lambda _: '/fake/node')
    calls=[]
    def run(command,**kwargs):
        calls.append((command,kwargs))
        return SimpleNamespace(returncode=0,stdout=png(),stderr=b'')
    monkeypatch.setattr(capture.subprocess,'run',run)
    assert capture.capture_screenshot('one')==png()
    assert calls[0][0][-2:]==['one',str(capture.WDA_PORT)]
    assert calls[0][0][1].endswith('device_screenshot.cjs') and calls[0][1]['timeout']==18


@pytest.mark.parametrize('result',[
    SimpleNamespace(returncode=1,stdout=b'',stderr=b'private log'),
    SimpleNamespace(returncode=0,stdout=b'not an image',stderr=b''),
    subprocess.TimeoutExpired('node',18),
    OSError('private filesystem path'),
])
def test_capture_failure_releases_lock_and_hides_subprocess_details(monkeypatch,result):
    monkeypatch.setattr(capture.shutil,'which',lambda _: '/fake/node')
    def fail(*args,**kwargs):
        if isinstance(result,Exception):raise result
        return result
    monkeypatch.setattr(capture.subprocess,'run',fail)
    with pytest.raises(RuntimeError) as error:capture.capture_screenshot('one')
    assert 'private' not in str(error.value)
    monkeypatch.setattr(capture.subprocess,'run',lambda *a,**k:SimpleNamespace(returncode=0,stdout=png()))
    assert capture.capture_screenshot('one')==png()


def test_duplicate_capture_is_bounded_and_does_not_start_another_process(monkeypatch):
    def forbidden(*args,**kwargs):raise AssertionError('Already capturing')
    monkeypatch.setattr(capture.subprocess,'run',forbidden)
    with capture._capture_lock:
        with pytest.raises(capture.ScreenshotBusy):capture.capture_screenshot('one')


@pytest.mark.parametrize('success',[True,False])
def test_node_bridge_targets_one_phone_and_only_reads_screenshot(tmp_path,monkeypatch,success):
    import base64
    import json
    import threading
    from http.server import BaseHTTPRequestHandler,HTTPServer
    if not capture.shutil.which('node'):pytest.skip('Node.js required for installed iPhone transport')
    requests=[]
    class WDA(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.command,self.path))
            body=json.dumps({'value':base64.b64encode(png()).decode()} if success else {'value':{'error':'private device details'}}).encode()
            self.send_response(200 if success else 500)
            self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
        def log_message(self,*args):pass
    server=HTTPServer(('127.0.0.1',0),WDA)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    module=tmp_path/'.appium/node_modules/appium-xcuitest-driver/node_modules/appium-ios-device'
    module.mkdir(parents=True)
    (module/'index.js').write_text("const net=require('node:net'); exports.utilities={connectPort:async(udid,port)=>{"
        "if(udid!=='phone-two'||port!==8101)throw new Error('wrong device');"
        f"return net.connect({server.server_port},'127.0.0.1');"+"}};")
    monkeypatch.setattr(capture,'ROOT',tmp_path)
    try:
        if success:assert capture.capture_screenshot('phone-two')==png()
        else:
            with pytest.raises(RuntimeError) as error:capture.capture_screenshot('phone-two')
            assert 'private' not in str(error.value)
        assert requests==[('GET','/screenshot')]  # No /session creation or deletion.
    finally:
        server.shutdown();server.server_close();worker.join(timeout=2)
