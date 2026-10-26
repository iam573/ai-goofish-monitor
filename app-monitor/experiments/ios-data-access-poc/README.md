# iOS 数据入口检查

实验入口，仅检查已配对 USB 设备上的闲鱼元数据、Library 目录访问能力和可检查 WebView/JSContext。不会创建配对、安装或启动 App、切换页面、注入进程、读取文件内容或发送商品业务请求。网页地址、原始页面、完整权限及登录凭据不输出。

在 `app-monitor/` 下运行，使用现有 iPhone 采集配置中的 USB UDID（不是 CoreDevice 的 UUID）：

```sh
uv run experiments/ios-data-access-poc/probe.py --help
uv run experiments/ios-data-access-poc/probe.py --udid '实际 USB UDID' --probe metadata
uv run experiments/ios-data-access-poc/probe.py --udid '实际 USB UDID' --probe webview --output .runtime/ios-data-access-probe.json
```

脚本使用 PEP 723 的独立依赖环境，不修改项目依赖；cryptography 固定为支持本次 Intel Mac 的 wheel 版本。默认依次检查三项，每项最多18秒；某项失败会记录错误并继续下一项，最终存在失败时退出码为1。

结果含义：

- `metadata.found`：系统能找到安装的 App；返回实际版本和已返回的调试权限。没有返回权限字段时不伪造其值。
- `container.accessible`：该次 HouseArrest 目录请求获准；只列目录候选，不打开数据库。`InstallationLookupFailed` 不足以单独判断 App 不存在或所有访问方式都不可用。
- `webview.inspector_connected`：系统 Web Inspector 握手成功。它不代表闲鱼开放调试；还需有 `goofish_pages` 才能继续验证页面桥接。列表只代表采样时已经创建且公开的页面，不代表整个 App 永远没有 WebView。

2026-10-26 实测及 Frida 条件检查见 `docs/validation.md` 第54节。Frida 的 USB/应用枚举成功不等于允许 attach；本脚本不执行 attach，也不尝试原生请求或签名外发。

## 原版 App 的只读代理实验

`proxy_probe.py` 使用独立的 mitmproxy 环境，不修改项目依赖。只对脚本 HOSTS 中的候选生产主机解密，其他 HTTPS 连接透传。输出连接状态、HTTP状态、白名单API名称和明确的itemId/item_id；无请求重放、响应改写或JS注入。只从正式App原有请求的data字段及商品API JSON响应提取明确ID，不保存请求头、原始参数、登录凭据、原始响应或流量文件。控制台同样不能开启mitmproxy的普通流量日志。

在 `app-monitor/` 下运行，默认只监听本机，15分钟自动退出：

```sh
uv run experiments/ios-data-access-poc/proxy_probe.py
```

手机实测需要先等采集空闲，再使用 `--listen-host '电脑的局域网IP'` 重启；手机与电脑必须互通，iPhone当前Wi-Fi的HTTP代理设为手动，服务器填该IP、端口8899。Safari打开 `http://mitm.it` 下载iOS证书，在系统设置安装后，还需在「通用 → 关于本机 → 证书信任设置」开启本次CA的完全信任。这些手机设置尚未执行。不要分发 `.runtime/ios-proxy-probe/ca/mitmproxy-ca.pem`，它包含CA私钥；公开证书只有 `mitmproxy-ca-cert.cer` / `mitmproxy-ca-cert.pem`。

完成后只打开一个商品，观察输出：

- `proxy_connect`：目标连接经过代理，不代表HTTPS已解密。
- `client_tls_failed`：客户端TLS未成功，可能是未信任CA、证书绑定或其他握手问题；单凭该事件不能断言证书绑定。
- `request_product_ids`：从正式App已有商品请求data字段取得的明确ID。当前实测详情API为mtop.taobao.idle.awesome.detail.unit，需以同一详情请求的ID作为当前商品的身份依据。
- `search_data`：解析搜索前缀接口中的候选公开商品对象，输出数值请求页码、字段计数和最多3条ID/标题/价格/地区样例。实测搜索API为mtop.taobao.idlemtopsearch.search，pageNumber从1增至2时获得两批不同商品。泛化扫描可能包含附属推荐，product_count不等于主列表条数；正式接入需限定主商品卡片边界。没有复制或重放分页请求。
- `response`：已看到HTTP返回。`api=null` 表示未识别白名单API；`format=unparsed`表示响应不是当前可解析的JSON，不等于没有商品ID；即使`format=json`且item_ids为空，也只说明该次解析没找到明确字段。
- 非空 `item_ids`：取得候选商品身份，下一步仍需核对当前商品、ID来源和详情链接，不自动写入产品数据。

最小统计保存在忽略的 `.runtime/ios-proxy-probe/summary.json`，保留计数、按API分组的有界ID并集、最多12条搜索观察和最后一条事件。详情响应可能含其他推荐商品，items_by_api并集不能直接用作主商品映射；取递归结果第一个ID会取错商品，后续接入须以同一详情请求关联响应。商品响应解析上限16MB，超过上限记录oversize，不输出正文。

实验不保证消除服务端验证。结束后先关闭手机Wi-Fi代理并移除本次CA，再停止电脑代理；代理超时退出后也需要关闭手机代理，避免手机断网。可用 `--duration` 调整时限，最大一小时。

参考：https://docs.mitmproxy.org/stable/concepts/certificates/ 。如果App拒绝实验CA，当前实验不尝试修改App或绕过校验。

## 核对同商品列表文字与详情正文

增加 `--compare-fields` 开启独立对照，复用相同代理/CA。主列表严格按 `data.resultList[].data.item.main.exContent` 读取，详情以正式App本次请求的明确商品ID关联，其他商品ID作用域跳过。只在忽略的 `.runtime/ios-proxy-probe/public-field-comparison.json` 保存白名单公开文字及参数/图片结构计数，文字上限20000字，记录原始长度、哈希及是否截断；不保存整包响应、请求参数、头或凭据。路径中的非白名单键名用星号替代。

`field_comparison` 事件报告字数及正文一致性，去空白/零宽字符；只认可当前商品作用域内正文字段，不把短标题或卖家昵称相同当全文一致。缺少参数解析结果不能证明原接口没有参数。

实测三件列表/详情为154/162、154/358、58/108字，均不完全相同；其中无省略号的短列表也是改写摘要。长描述详情补出了摄像头故障、电池78%和后玻璃更换等事实，见docs/validation.md第59节。当前实验支持批量列表初筛，尚不支持据列表无省略号就跳过拟推送候选的详情核对。
