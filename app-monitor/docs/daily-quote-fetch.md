# 每日报价单抓取方案

## 1. 报价接口

报价接口返回指定分类当前最新的报价单图片地址：

```http
GET https://api.ajhuishou.com/ajhuishou-product/api/home/getQuoteImage
```

主要参数：

| 参数 | 示例 | 说明 |
| --- | --- | --- |
| `categoryId` | `100025` | 报价分类 ID，建议始终传入 |
| `appid` | `wx5d70a654307f7bbf` | 小程序 App ID，目前实测可省略 |
| `platform` | `weapp-lite` | 客户端平台，目前实测可省略 |

获取“苹果无保”报价单的最简请求：

```bash
curl --compressed \
  'https://api.ajhuishou.com/ajhuishou-product/api/home/getQuoteImage?categoryId=100025'
```

响应示例：

```json
{
  "code": 200,
  "msg": "操作成功",
  "data": "https://cdn.ajhuishou.com/file-storage/20261018/example.png"
}
```

`data` 是报价单图片地址。接口当前不需要 Cookie、Token 或请求体。

## 2. 获取分类

需要抓取其他品类时，先请求分类树：

```http
GET https://api.ajhuishou.com/ajhuishou-product/api/home/categoryTree
```

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

报价每天早上更新。当前实现从北京时间 09:00 起每 30 分钟检查一次，持续 3 小时，当天抓到新图片后停止：

1. 调用 `categoryTree` 获取有效分类；
2. 对需要的 `categoryId` 调用 `getQuoteImage`；
3. 读取响应中的图片 URL；
4. 与上一次保存的 URL 比较；
5. URL 发生变化时下载图片并记录抓取时间；
6. URL 没有变化时跳过，避免重复保存。

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
    "image_url": "https://cdn.ajhuishou.com/file-storage/20261018/example.png",
    "image_sha256": "...",
    "fetched_at": "2026-10-18T19:30:00+08:00"
  }
}
```

## 4. 调度与限流

可以使用系统 `cron`、launchd 或项目内调度器执行每日任务。例如 cron：

```cron
30 19 * * * cd /path/to/ai-goofish-monitor && python app-monitor/fetch_daily_quotes.py
```

接口存在频率限制。抓取多个分类时，每次请求之间建议随机等待 3 至 5 秒；遇到响应 `code=101` 时暂停后重试，并使用指数退避。单个分类每天请求一次通常足够。

## 5. 异常处理

- HTTP 请求失败：最多重试 3 次；
- 返回 `code != 200`：记录错误，本轮不下载；
- `data` 不是 HTTPS 图片地址：拒绝保存；
- 图片下载不完整：校验 Content-Type、文件大小和 SHA-256；
- 图片日期早于当天：正常保存为最新版本，但标记该分类当天未更新；
- 不保存 Cookie、Token、抓包文件或其他登录信息。
