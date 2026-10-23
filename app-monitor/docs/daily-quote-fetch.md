# 每日报价单抓取方案

## 1. 报价接口

报价接口返回指定分类当前最新的报价单图片地址：

```http
GET https://api.zuphone.com/biji-product/api/home/getQuoteImage
```

主要参数：

| 参数 | 示例 | 说明 |
| --- | --- | --- |
| `categoryId` | `100025` | 报价分类 ID，建议始终传入 |
| `appid` | `wx5d70a654307f7bbf` | 小程序 App ID |
| `platform` | `weapp-lite-aiji-report` | 客户端平台 |

2026-10-22 验证接口已迁移；旧的 api.ajhuishou.com 接口仍返回旧图。新接口还需要公开请求头 `wzw-app-id` 与 `wzw-client-type`，值分别与上述 appid、platform 一致。不带公开上下文时服务返回 HTTP 500；带上这两个头即可获取新图，不需要 wzw-access-token、Cookie、时间戳或签名。

获取“苹果无保”报价单的最简请求：

```bash
curl --compressed \
  -H 'wzw-app-id: wx5d70a654307f7bbf' \
  -H 'wzw-client-type: weapp-lite-aiji-report' \
  'https://api.zuphone.com/biji-product/api/home/getQuoteImage?appid=wx5d70a654307f7bbf&platform=weapp-lite-aiji-report&categoryId=100025'
```

响应示例：

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": "https://cdn.zuphone.com/file-storage/20261021/example.png"
}
```

`data` 是报价单图片地址。接口当前不需要 Cookie、Token 或请求体。

## 2. 获取分类

需要抓取其他品类时，先请求分类树：

```http
GET https://api.zuphone.com/biji-product/api/home/categoryTree
```

分类树同样传入上述公开请求头和 appid、platform 参数。后续迁移可在 `.env` 设置 `APP_MONITOR_QUOTE_API_ROOT`、`APP_MONITOR_QUOTE_APP_ID`、`APP_MONITOR_QUOTE_PLATFORM`，无需修改代码。

常用分类：

| 分类 ID | 分类名称 |
| ---: | --- |
| `100027` | 苹果有保 |
| `100025` | 苹果无保 |
| `100028` | 华为旗舰 |
| `100044` | 苹果平板 |
| `100049` | 苹果电脑 |
| `100092` | 全新苹果国行 |

应使用分类树实时返回的叶子分类，不要长期依赖手工维护的完整分类列表。

## 3. 每日抓取流程

当前实现每天北京时间 09:00 同步一次，当天已成功同步的分类不再自动检查；手动「立即同步」仍可获取最新图片：

1. 首次接入或分类变更时调用 `categoryTree` 核对分类；常规同步只检查苹果无保和有保两类；
2. 对需要的 `categoryId` 调用 `getQuoteImage`；
3. 读取响应中的图片 URL；
4. 下载接口当前返回的图片，计算 SHA-256；
5. 内容发生变化时保存新批次并识别价格及扣费；
6. URL 变化但内容相同时更新来源地址、不重复识别；URL 不变而内容变化时也生成新批次。失败、中断或零条的同图批次会重试解析。

接口返回的是“当前最新报价单”，不保证每个分类每天更新。因此应以图片 URL 或图片 SHA-256 是否变化判断更新，不能只看任务执行日期。

2026-10-19 实测：早上发布的新报价图片 URL 中的日期仍是前一天（`20261018`），因此“今天是否已更新”以当天是否抓到新图片为准，URL 日期只用于展示。

建议保存目录：

```text
data/daily-quotes/<category_id>/<quote_date>/<image_name>.png
```

同时保存一份状态文件：

```json
{
  "100025": {
    "image_url": "https://cdn.zuphone.com/file-storage/20261021/example.png",
    "image_sha256": "...",
    "fetched_at": "2026-10-18T19:30:00+08:00"
  }
}
```

## 4. 调度与限流

项目内调度器负责定时检查；服务在当天配置的开始时间之后启动时，仅对当天尚未成功同步的分类补做一次检查。手动「立即同步」同样获取最新 URL，不受当天已同步状态限制。

接口存在频率限制。两个分类的批量同步间隔3秒；遇到响应 `code=101` 或其他失败时最多尝试3次，并使用指数退避。定时检查与手动同步共用图片内容去重，同图不重复 OCR。

## 5. 异常处理

- HTTP 请求失败：最多重试 3 次；
- 返回 `code != 200`：记录错误，本轮不下载；
- `data` 不是 HTTPS 图片地址：拒绝保存；
- 图片下载不完整：校验 Content-Type、文件大小和 SHA-256；
- 图片 URL 中的日期仅用于展示，不能用来判断是否已取得最新版本；
- 不保存 Cookie、Token、抓包文件或其他登录信息。
