# Appium/XCUITest 旧方案实验

这是最初用于验证“Mac + Appium + XCUITest 控制真机闲鱼”的实验实现，保留用于复盘，
不代表当前推荐架构。

## 内容

- `monitor.py`：发现连接的 iPhone、启动闲鱼、输入关键词、滚动并解析可访问性 XML。
- `test_monitor.py`：商品标题和价格解析的最小测试。
- `open_mac_search.py`：用闲鱼深链直接打开 Mac 版搜索结果。
- `artifacts/`：实验截图和 Appium 页面 XML，本地保留但不提交。
- `appium.log`：历史实验日志，本地保留但不提交。

## 运行

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
npm install
APPIUM_HOME="$PWD/.appium" npx appium driver install xcuitest
APPIUM_HOME="$PWD/.appium" npx appium
```

另一个终端运行：

```bash
.venv/bin/python monitor.py --keyword "MacBook Air M1"
```

Mac 版深链验证：

```bash
python3 open_mac_search.py "trackpad"
```

## 实验结论

真机 Appium 链路可以工作，但完整 UI 自动化步骤多，页面控件结构也会随版本变化。
后续验证发现，已登录 Mac 版闲鱼可以通过深链打开搜索、用少量可访问性操作切换“新发”，
并从本地曝光数据库取得商品 ID 和精确发布时间。新方案见
[`../../docs/technical-design.md`](../../docs/technical-design.md)。
