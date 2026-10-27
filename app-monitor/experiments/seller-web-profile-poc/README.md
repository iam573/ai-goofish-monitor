# 匿名网页卖家资料验证

2026-10-27 验证：干净的、未登录的可见 Chrome 访问用户给定主页后，可通过闲鱼当前网页自带 `window.lib.mtop.request` 直接调用三个只读 API。未读取用户原有 Chrome 配置，不持久化浏览器状态、Cookie、Token、签名或原始请求。

| 用途 | API | 版本 |
| --- | --- | --- |
| 主页与信用标签 | `mtop.idle.web.user.page.head` | 1.0 |
| 商品列表与分组计数 | `mtop.idle.web.xyh.item.list` | 1.0 |
| 信用评价列表 | `mtop.idle.web.trade.rate.list` | 1.0 |

三者均实际返回 `SUCCESS::调用成功`。列表首批20件、评价首批20条均有 `nextPage=true`。商品可用 `itemStatus` 区分在售与已售，并有在售/已售分组计数；不把分组总数误当作本次已读取条数。评价接口的 `ratedUid` 使用主页返回的 `baseInfo.kcUserId`，不直接假定等于URL里的userId。未遍历完整历史，也不声称列表顺序等于严格成交时间顺序。信用标签是闲鱼公开的买卖家信用，不等同于芝麻信用分。

无会话、无签名的普通HTTP请求返回 `FAIL_SYS_TOKEN_EMPTY::令牌为空`；因此目前验证的是匿名网页会话和官方SDK生成请求，尚不是无状态纯HTTP客户端。普通无头浏览器访问主页曾被“非法访问，请使用正常浏览器”拦截；可见Chrome成功。遇验证/拦截停止，不自动登录或重试绕过。SDK协议来自页面公开资源 `https://g.alicdn.com/idle-pc/xy-site/0.0.179/js/p_personal-index.js`，正式接入前需重新确认页面版本。

运行（在app-monitor目录；默认另开临时可见Chrome，不操作iPhone）：

```sh
uv run --with playwright python experiments/seller-web-profile-poc/probe.py SELLER_USER_ID
```

脚本只输出公开昵称、信用标签、分组计数、首批商品摘要及评价数量；不写原始响应、请求URL、请求头或登录凭据。浏览器匿名上下文关闭后释放会话。该脚本仅为验证材料，尚未接入监控或同行过滤。后续可独立定义 `seller_id / seller_credit / counts / recent_items / observed_at` 数据契约，使用按卖家缓存及有界分页，避免两个方案直接导入业务模块。

## 商品详情验证

详情页当前使用 `mtop.taobao.idle.pc.detail` 1.0，参数为`itemId`，与iPhone代理的详情接口不是同一个API。2026-10-27 对用户指定商品进行匿名可见Chrome验证：正常打开官方商品页，两次独立干净会话的自然详情请求均返回SUCCESS，可读完整正文、挂牌价、机型/容量/版本/成色/拆修参数以及图片信息。正文与参数均明确电池更换。

但在卖家页上直接调用网页SDK读取该商品，以及在商品页已加载成功的会话中用SDK读取另一件商品，实际网络返回RGV587_ERROR；SDK随后尝试登录流程并最终TIMEOUT。即使SDK Promise报超时，诊断时也应保留实际网络层的风控原因，不能只记成网络慢；这不是“必须登录”的充分证据。未登录卖家页API成功也不能推断商品API具有相同限制。未尝试绕过验证、伪造签名或继续自动重试。

因此已证实可用的替代链路是：列表取得真实ID → 电脑Chrome正常打开商品页并读取其自然详情返回 → 判价。无需手机打开详情；纯SDK跨商品连续调用尚未验证成功。只有代理列表已取得ID时可直接使用该链路，原UI通道取得ID之前的问题不能由网页API自动解决。尚未接入生产监控。

只读商品页验证脚本（默认临时可见Chrome，不保存会话或原始报文）：

```sh
uv run --with playwright python experiments/seller-web-profile-poc/item_probe.py 'https://www.goofish.com/item?id=ITEM_ID&categoryId=CATEGORY_ID'
```
