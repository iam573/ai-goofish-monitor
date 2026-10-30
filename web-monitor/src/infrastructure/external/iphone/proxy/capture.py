"""服务进程内管理代理工作进程，并经本机 Unix socket 把各手机的商品事件转给任务进程。

手机 Wi-Fi 代理一直指向这里，工作进程随 Web 服务常驻；任务进程按手机 IP 订阅，
只在订阅期间转发该 IP 的白名单事件，不保存原始流量、Cookie 或 Token。
"""

from __future__ import annotations

import ipaddress
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time
from pathlib import Path

from src.infrastructure.external.iphone.settings import IPhoneSettings

WORKER_PATH = Path(__file__).with_name("proxy_worker.py")
FORWARDED_EVENTS = {"search", "detail", "verification", "parse_error"}
_CA_FILES = ("mitmproxy-ca.pem", "mitmproxy-ca-cert.pem", "mitmproxy-ca-cert.cer", "mitmproxy-ca-cert.p12")


def normalize_ip(value: str) -> str:
    return str(ipaddress.ip_address(str(value).strip()))


def local_addresses() -> list[str]:
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 80))  # 只选择本机路由，不会发出数据包
            addresses.add(probe.getsockname()[0])
    except OSError:
        pass
    return sorted(value for value in addresses if not ipaddress.ip_address(value).is_loopback)


class _Subscriber:
    def __init__(self, conn: socket.socket):
        self.conn = conn
        self.lock = threading.Lock()

    def send(self, payload: dict) -> bool:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            with self.lock:
                self.conn.sendall(data)
            return True
        except OSError:
            return False


class ProxyService:
    def __init__(self, settings: IPhoneSettings):
        self.settings = settings
        self._condition = threading.Condition(threading.RLock())
        self._process: subprocess.Popen | None = None
        self._ready = False
        self._error = ""
        self._clients: set[str] = set()
        self._subscribers: dict[str, _Subscriber] = {}
        self._activity: dict[str, dict] = {}
        self._server: socket.socket | None = None

    @property
    def certificate(self) -> Path:
        return self.settings.proxy_certificate

    def configure(self, clients: list[str]) -> None:
        values = {normalize_ip(value) for value in clients if value}
        with self._condition:
            self._clients = values
            self._send_clients()

    def _send_clients(self) -> None:
        process = self._process
        if process and process.poll() is None and process.stdin:
            try:
                process.stdin.write(json.dumps({"clients": sorted(self._clients | set(self._subscribers))}) + "\n")
                process.stdin.flush()
            except (BrokenPipeError, OSError):
                self._error = "代理服务已退出，请在设备列表重新启用"

    def _prepare_runtime(self) -> Path:
        runtime = self.settings.proxy_runtime
        runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
        ca = runtime / "ca"
        ca.mkdir(exist_ok=True, mode=0o700)
        os.chmod(ca, 0o700)
        trusted = self.settings.proxy_trusted_ca_dir
        # 复用手机已信任的 CA（例如 app-monitor 生成的），免得重新安装证书
        if trusted and not (ca / "mitmproxy-ca.pem").exists() and (trusted / "mitmproxy-ca.pem").is_file():
            for name in _CA_FILES:
                if (trusted / name).is_file():
                    shutil.copyfile(trusted / name, ca / name)
                    os.chmod(ca / name, 0o600)
        return ca

    def start(self) -> None:
        with self._condition:
            if self._process and self._process.poll() is None:
                return
            uv = shutil.which("uv")
            if not uv:
                self._error = "代理采集需要 uv，请先安装 uv（https://docs.astral.sh/uv/）后重试"
                raise RuntimeError(self._error)
            try:
                ca = self._prepare_runtime()
                self._ensure_socket_server()
                self._ready, self._error = False, ""
                self._process = subprocess.Popen(
                    [uv, "run", "--script", str(WORKER_PATH), "--host", self.settings.proxy_host,
                     "--port", str(self.settings.proxy_port), "--confdir", str(ca)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, bufsize=1, start_new_session=True,
                )
            except (OSError, RuntimeError) as exc:
                self._error = f"代理服务启动失败：{exc}"
                raise RuntimeError(self._error) from exc
            process = self._process
            self._send_clients()
            threading.Thread(target=self._read_worker, args=(process,), daemon=True).start()

    def _read_worker(self, process: subprocess.Popen) -> None:
        try:
            for line in process.stdout:
                if len(line) > 5_000_000:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.ingest(event)
        finally:
            with self._condition:
                if self._process is process:
                    self._ready = False
                    self._error = "代理服务已退出；请检查端口是否被占用（如 app-monitor 正在使用同一端口）后重新启用"
                    self._broadcast({"event": "status", "ready": False, "error": self._error})
                    self._condition.notify_all()

    def ingest(self, event: dict) -> None:
        """只接受工作进程的白名单事件；也是测试入口。"""
        kind = event.get("event")
        if kind not in {"ready", "traffic", "tls_failed"} | FORWARDED_EVENTS:
            return
        with self._condition:
            if kind == "ready":
                self._ready = True
                self._broadcast({"event": "status", "ready": True, "error": ""})
                self._condition.notify_all()
                return
            peer = event.get("client_ip")
            if peer not in self._clients | set(self._subscribers):
                return
            self._activity[peer] = {"last_seen": time.time(), "tls_failed": kind == "tls_failed"}
            subscriber = self._subscribers.get(peer)
            if subscriber is not None and kind in FORWARDED_EVENTS and not subscriber.send(event):
                self._drop_subscriber(peer, subscriber)
            self._condition.notify_all()

    def _broadcast(self, payload: dict) -> None:
        for peer, subscriber in list(self._subscribers.items()):
            if not subscriber.send(payload):
                self._drop_subscriber(peer, subscriber)

    def _drop_subscriber(self, peer: str, subscriber: _Subscriber) -> None:
        if self._subscribers.get(peer) is subscriber:
            del self._subscribers[peer]
            self._send_clients()
        try:
            subscriber.conn.close()
        except OSError:
            pass

    def _ensure_socket_server(self) -> None:
        if self._server is not None:
            return
        path = self.settings.proxy_socket_path
        if len(str(path).encode()) > 100:
            raise RuntimeError(f"代理 socket 路径过长：{path}；请把 IPHONE_PROXY_RUNTIME 设到更短的目录")
        path.unlink(missing_ok=True)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(path))
        os.chmod(path, 0o600)
        server.listen(8)
        self._server = server
        threading.Thread(target=self._accept, args=(server,), daemon=True).start()

    def _accept(self, server: socket.socket) -> None:
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._serve_subscriber, args=(conn,), daemon=True).start()

    def _serve_subscriber(self, conn: socket.socket) -> None:
        subscriber = _Subscriber(conn)
        reader = conn.makefile("r", encoding="utf-8")
        peer = ""
        try:
            try:
                request = json.loads(reader.readline() or "{}")
                peer = normalize_ip(request.get("subscribe", ""))
            except (ValueError, TypeError):
                subscriber.send({"event": "error", "error": "代理订阅请求无效"})
                return
            with self._condition:
                if peer in self._subscribers:
                    subscriber.send({"event": "error", "error": "该手机已有代理采集任务"})
                    peer = ""
                    return
                if peer not in self._clients:
                    subscriber.send({"event": "error", "error": "该手机未启用代理采集，请先在设备列表保存手机的 Wi-Fi IP"})
                    peer = ""
                    return
                self._subscribers[peer] = subscriber
                self._send_clients()
                subscriber.send({"event": "subscribed", "ready": self._ready, "error": self._error})
            # 任务进程断开即结束订阅
            while reader.readline():
                pass
        except OSError:
            pass
        finally:
            with self._condition:
                if peer:
                    self._drop_subscriber(peer, subscriber)
            try:
                conn.close()
            except OSError:
                pass

    def status(self, client_ip: str = "") -> dict:
        with self._condition:
            running = bool(self._process and self._process.poll() is None)
            activity = self._activity.get(client_ip, {})
            connected = bool(activity and time.time() - activity.get("last_seen", 0) < 180
                             and not activity.get("tls_failed"))
            return {
                "running": running, "ready": running and self._ready, "error": self._error,
                "port": self.settings.proxy_port, "addresses": local_addresses(),
                "certificate_ready": self.certificate.is_file(),
                "client_connected": connected, "tls_failed": bool(activity.get("tls_failed")),
                "client_ip": client_ip, "last_seen": activity.get("last_seen"),
                "busy": client_ip in self._subscribers,
            }

    def stop(self) -> None:
        with self._condition:
            process, self._process = self._process, None
            server, self._server = self._server, None
            self._ready = False
            for peer, subscriber in list(self._subscribers.items()):
                self._drop_subscriber(peer, subscriber)
        if server is not None:
            server.close()
            self.settings.proxy_socket_path.unlink(missing_ok=True)
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
