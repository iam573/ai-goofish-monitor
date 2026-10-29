"""Manage a credential-free proxy IPC worker and bounded per-device observations."""
from __future__ import annotations

from collections import Counter, deque
from contextlib import contextmanager
import ipaddress
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import subprocess
import threading
import time
import unicodedata

from .config import ROOT
from .stop_control import checkpoint


def normalized(value):
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


class ProxyCapture:
    def __init__(self, settings):
        self.settings = settings
        self.runtime = Path(getattr(settings, "proxy_runtime", ROOT / ".runtime/proxy"))
        self.port = int(getattr(settings, "proxy_port", 8899))
        self.host = getattr(settings, "proxy_listen_host", "0.0.0.0")
        self._condition = threading.Condition(threading.RLock())
        self._events = deque(maxlen=128)
        self._sequence = 0
        self._process = None
        self._ready = False
        self._error = ""
        self._clients = set()
        self._activity = {}
        self._leases = set()
        self._reserved = Counter()

    @property
    def certificate(self):
        return self.runtime / "ca/mitmproxy-ca-cert.cer"

    def configure(self, clients):
        values = {str(ipaddress.ip_address(value)) for value in clients if value}
        with self._condition:
            self._clients = values
            if self._process and self._process.poll() is None and self._process.stdin:
                try:
                    self._process.stdin.write(json.dumps({"clients": sorted(values | self._leases | set(self._reserved))}) + "\n")
                    self._process.stdin.flush()
                except (BrokenPipeError, OSError):
                    self._error = "代理服务已退出，请重新启用"

    def start(self):
        with self._condition:
            if self._process and self._process.poll() is None:
                return
            uv = shutil.which("uv")
            if not uv:
                self._error = "代理采集需要 uv，请先安装 uv 后重试"
                raise RuntimeError(self._error)
            self.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
            ca = self.runtime / "ca"
            ca.mkdir(exist_ok=True, mode=0o700)
            os.chmod(ca, 0o700)
            # Preserve an already trusted experimental CA, without importing experiments.
            legacy = ROOT / ".runtime/ios-proxy-probe/ca"
            if not (ca / "mitmproxy-ca.pem").exists() and (legacy / "mitmproxy-ca.pem").exists():
                for name in ("mitmproxy-ca.pem", "mitmproxy-ca-cert.pem", "mitmproxy-ca-cert.cer", "mitmproxy-ca-cert.p12"):
                    source = legacy / name
                    if source.is_file():
                        shutil.copyfile(source, ca / name)
                        os.chmod(ca / name, 0o600)
            self._ready, self._error = False, ""
            self._events.clear()
            worker = Path(__file__).with_name("proxy_worker.py")
            self._process = subprocess.Popen(
                [uv, "run", "--script", str(worker), "--host", self.host, "--port", str(self.port), "--confdir", str(ca)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, bufsize=1, start_new_session=True,
            )
            process = self._process
            self.configure(self._clients)
            threading.Thread(target=self._read, args=(process,), daemon=True).start()

    def _read(self, process):
        try:
            for line in process.stdout:
                if len(line) > 5_000_000:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                self.ingest(event)
        finally:
            with self._condition:
                if self._process is process:
                    self._ready = False
                    self._error = "代理服务已退出；请检查端口是否被占用或重新启用"
                    self._condition.notify_all()

    def ingest(self, event):
        """Accept only sanitized worker events; also provides a deterministic test seam."""
        kind = event.get("event")
        if kind not in {"ready", "traffic", "tls_failed", "search", "detail", "verification", "parse_error"}:
            return
        with self._condition:
            if kind == "ready":
                self._ready = True
            elif event.get("client_ip") not in self._clients | self._leases | set(self._reserved):
                return
            else:
                now = time.time()
                peer = event["client_ip"]
                self._activity[peer] = {"last_seen": now, "tls_failed": kind == "tls_failed"}
                if peer in self._leases and kind not in {"traffic", "tls_failed"}:
                    while self._events and self._events[0]["received_at"] < now - 180:
                        self._events.popleft()
                    self._sequence += 1
                    self._events.append({**event, "sequence": self._sequence, "received_at": now})
            self._condition.notify_all()

    def wait_ready(self, timeout=30):
        checkpoint()
        self.start()
        with self._condition:
            deadline = time.monotonic() + timeout
            while not self._ready and not self._error and time.monotonic() < deadline:
                checkpoint()
                self._condition.wait(min(.2, max(0, deadline - time.monotonic())))
            checkpoint()
            if not self._ready:
                raise RuntimeError(self._error or "代理服务仍在准备依赖，请稍后重试")

    @contextmanager
    def session(self, client_ip):
        client_ip = str(ipaddress.ip_address(client_ip))
        with self._condition:
            if client_ip in self._leases:
                raise RuntimeError("该手机已有代理采集任务")
            if client_ip not in self._clients | set(self._reserved):
                raise RuntimeError("请先在设备列表保存手机的 Wi-Fi IP")
            self._leases.add(client_ip)
        try:
            yield
        finally:
            with self._condition:
                self._leases.discard(client_ip)
                self._events = deque((event for event in self._events if event.get("client_ip") != client_ip), maxlen=128)
                self.configure(self._clients)

    @contextmanager
    def reserve(self, clients):
        """Keep a run's saved device bindings valid while next-run settings change."""
        values = {str(ipaddress.ip_address(value)) for value in clients if value}
        with self._condition:
            self._reserved.update(values)
            self.configure(self._clients)
        try:
            yield
        finally:
            with self._condition:
                self._reserved.subtract(values)
                self._reserved = +self._reserved
                self.configure(self._clients)

    def events(self, client_ip, since, kind="search", keyword=None, fingerprint=None, target=None, after_sequence=0,
               sort_field=None, sort_value=None, verification_since=None):
        with self._condition:
            return [dict(event) for event in self._events
                    if event.get("client_ip") == client_ip
                    and event.get("request_started", 0) >= (verification_since if event["event"] == "verification"
                         and verification_since is not None else since)
                    and event.get("sequence", 0) > after_sequence
                    and event.get("received_at", 0) >= time.time() - 180
                    and (event["event"] == "verification" or event["event"] == kind
                         and (keyword is None or normalized(event.get("keyword", "")) == normalized(keyword))
                         and (fingerprint is None or event.get("filter_fingerprint") == fingerprint)
                         and (target is None or event.get("item_id") == target)
                         and (sort_field is None or event.get("sort_field") == sort_field)
                         and (sort_value is None or event.get("sort_value") == sort_value))]

    def wait(self, client_ip, since, timeout=15, **filters):
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                checkpoint()
                events = self.events(client_ip, since, **filters)
                if events:
                    return events
                if self._error:
                    raise RuntimeError(self._error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(min(.2, remaining))

    def status(self, client_ip=""):
        with self._condition:
            running = bool(self._process and self._process.poll() is None)
            activity = self._activity.get(client_ip, {})
            connected = bool(activity and time.time() - activity.get("last_seen", 0) < 180 and not activity.get("tls_failed"))
            return {"running": running, "ready": running and self._ready, "error": self._error,
                    "port": self.port, "addresses": local_addresses(), "certificate_ready": self.certificate.is_file(),
                    "client_connected": connected, "tls_failed": bool(activity.get("tls_failed")),
                    "client_ip": client_ip, "last_seen": activity.get("last_seen")}

    def stop(self):
        with self._condition:
            process, self._process = self._process, None
            self._ready = False
            self._events.clear()
        if process and process.poll() is None:
            if process.stdin:
                process.stdin.close()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=3)


def local_addresses():
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))  # Select local route; sends no packet.
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(value for value in addresses if not ipaddress.ip_address(value).is_loopback)
