import threading
import time
from types import SimpleNamespace

import pytest

from app_monitor.proxy_capture import ProxyCapture

IP, OTHER_IP = "192.168.1.20", "192.168.1.21"


def make_capture(tmp_path):
    capture = ProxyCapture(SimpleNamespace(proxy_runtime=tmp_path, proxy_port=18999))
    capture.configure([IP, OTHER_IP])
    return capture


def event(**changes):
    return {"event": "search", "client_ip": IP, "keyword": "iPhone 15 Pro", "request_started": time.time(),
            "filter_fingerprint": "one", "items": [], **changes}


def test_observation_is_scoped_by_device_start_keyword_filters_and_target(tmp_path):
    capture = make_capture(tmp_path)
    since = time.time()
    with capture.session(IP):
        capture.ingest(event(request_started=since - 1))  # Late old response.
        capture.ingest(event(client_ip=OTHER_IP))
        capture.ingest(event(keyword="iPhone 16"))
        capture.ingest(event(filter_fingerprint="two"))
        capture.ingest(event())
        found = capture.events(IP, since, keyword="iphone15pro", fingerprint="one")
        assert len(found) == 1
        capture.ingest(event(event="detail", item_id="1093306353441"))
        assert not capture.events(IP, since, kind="detail", target="1092690649226")
        assert len(capture.events(IP, since, kind="detail", target="1093306353441")) == 1
    assert not capture._events  # No browsing/product cache after a run.


def test_wait_is_notified_early_and_verification_overrides_context_filters(tmp_path):
    capture = make_capture(tmp_path)
    since = time.time()
    with capture.session(IP):
        timer = threading.Timer(.02, lambda: capture.ingest(event(event="verification", keyword="", item_id="")))
        timer.start()
        found = capture.wait(IP, since, kind="detail", target="1093306353441", timeout=1)
        timer.join()
        assert found[0]["event"] == "verification"


def test_switching_channel_does_not_remove_an_active_device_lease(tmp_path):
    capture = make_capture(tmp_path)
    since = time.time()
    with capture.session(IP):
        capture.configure([])
        capture.ingest(event())
        assert len(capture.events(IP, since)) == 1
        with pytest.raises(RuntimeError, match="已有"):
            with capture.session(IP):
                pass
    capture.ingest(event())
    assert not capture._events


def test_idle_browsing_records_only_connection_state(tmp_path):
    capture = make_capture(tmp_path)
    capture.ingest(event())
    assert not capture._events and IP in capture._activity
    capture.ingest(event(client_ip="192.168.1.99"))
    assert "192.168.1.99" not in capture._activity


def test_reserved_next_device_survives_mid_run_configuration_change(tmp_path):
    capture = make_capture(tmp_path)
    with capture.reserve([IP]):
        capture.configure([])
        with capture.session(IP):
            capture.ingest(event())
            assert capture._events
    with pytest.raises(RuntimeError, match="Wi-Fi IP"):
        with capture.session(IP):
            pass
