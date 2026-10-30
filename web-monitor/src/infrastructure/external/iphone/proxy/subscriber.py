"""任务进程按手机 IP 订阅 Web 服务转发的代理事件，在内存里按时间、关键词、排序与商品 ID 匹配。"""

from __future__ import annotations

import json
import socket
import threading
import time
import unicodedata
from collections import deque
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

EVENT_TTL_SECONDS = 180


def normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).casefold().split())


class ProxySubscriber:
    def __init__(self, socket_path: Path, *, check_stop: Callable[[], None] | None = None):
        self.socket_path = socket_path
        self.check_stop = check_stop or (lambda: None)
        self._condition = threading.Condition()
        self._events: deque[dict] = deque(maxlen=128)
        self._sequence = 0
        self._ready = False
        self._error = ""
        self._closed = False
        self.client_ip = ""

    @contextmanager
    def session(self, client_ip: str, timeout: float = 10) -> Iterator["ProxySubscriber"]:
        conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        conn.settimeout(timeout)
        try:
            conn.connect(str(self.socket_path))
        except OSError as exc:
            conn.close()
            raise RuntimeError("代理服务未运行：请在 Web 界面的「采集设备」中为该手机启用代理采集") from exc
        reader = conn.makefile("r", encoding="utf-8")
        try:
            conn.sendall((json.dumps({"subscribe": client_ip}) + "\n").encode("utf-8"))
            reply = json.loads(reader.readline() or "{}")
            if reply.get("event") != "subscribed":
                raise RuntimeError(reply.get("error") or "代理服务拒绝了订阅")
            conn.settimeout(None)
            self.client_ip = client_ip
            self._ready, self._error = bool(reply.get("ready")), str(reply.get("error") or "")
            threading.Thread(target=self._read, args=(reader,), daemon=True).start()
            yield self
        finally:
            with self._condition:
                self._closed = True
                self._events.clear()
                self._condition.notify_all()
            try:
                conn.close()
            except OSError:
                pass

    def _read(self, reader) -> None:
        try:
            for line in reader:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    self.ingest(event)
        except (OSError, ValueError):
            pass
        finally:
            with self._condition:
                if not self._closed:
                    self._error = "与代理服务的连接已断开（Web 服务可能已重启）"
                self._condition.notify_all()

    def ingest(self, event: dict) -> None:
        with self._condition:
            if event.get("event") == "status":
                self._ready = bool(event.get("ready"))
                self._error = str(event.get("error") or "")
            elif event.get("client_ip") == self.client_ip:
                now = time.time()
                while self._events and self._events[0]["received_at"] < now - EVENT_TTL_SECONDS:
                    self._events.popleft()
                self._sequence += 1
                self._events.append({**event, "sequence": self._sequence, "received_at": now})
            self._condition.notify_all()

    def wait_ready(self, timeout: float = 60) -> None:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self._ready and not self._error and time.monotonic() < deadline:
                self.check_stop()
                self._condition.wait(min(0.2, max(0, deadline - time.monotonic())))
            if not self._ready:
                raise RuntimeError(self._error or "代理服务仍在准备依赖，请稍后重试")

    def events(
        self, since: float, kind: str = "search", keyword: str | None = None, fingerprint: str | None = None,
        target: str | None = None, after_sequence: int = 0, sort_field: str | None = None,
        sort_value: str | None = None, verification_since: float | None = None,
    ) -> list[dict]:
        with self._condition:
            now = time.time()
            result = []
            for event in self._events:
                is_verification = event["event"] == "verification"
                started = event.get("request_started", 0)
                if started < (verification_since if is_verification and verification_since is not None else since):
                    continue
                if event.get("sequence", 0) <= after_sequence or event["received_at"] < now - EVENT_TTL_SECONDS:
                    continue
                if is_verification or (
                    event["event"] == kind
                    and (keyword is None or normalized(event.get("keyword", "")) == normalized(keyword))
                    and (fingerprint is None or event.get("filter_fingerprint") == fingerprint)
                    and (target is None or event.get("item_id") == target)
                    and (sort_field is None or event.get("sort_field") == sort_field)
                    and (sort_value is None or event.get("sort_value") == sort_value)
                ):
                    result.append(dict(event))
            return result

    def wait(self, since: float, timeout: float = 15, **filters) -> list[dict]:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                self.check_stop()
                events = self.events(since, **filters)
                if events:
                    return events
                if self._error:
                    raise RuntimeError(self._error)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return []
                self._condition.wait(min(0.2, remaining))
