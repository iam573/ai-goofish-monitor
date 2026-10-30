"""可选的 iPhone 代理采集通道：mitmproxy 工作进程只输出白名单商品字段，不保存原始流量或登录凭据。

proxy_protocol.py 与 proxy_worker.py 移植自 app-monitor，工作进程在独立的 uv Python 3.12 环境运行。
"""
