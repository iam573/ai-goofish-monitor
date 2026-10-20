# AI Goofish Monitor

仓库现在按数据采集方式拆成两个独立方案：

| 目录 | 用途 | 状态 |
| --- | --- | --- |
| [`web-monitor/`](web-monitor/) | Playwright + 网页 PC API 的原有监控系统 | 可运行、功能完整 |
| [`app-monitor/`](app-monitor/) | 已登录 Mac 闲鱼 App + 每日报价 + 匹配推送 | 首版已实现 |

## 网页方案

```bash
cd web-monitor
bash start.sh
```

详细说明见 [`web-monitor/README.md`](web-monitor/README.md)。

## App 方案

App 方案不配置 HTTPS 代理，也不重放原生 MTOP 请求。正式闲鱼 App 负责搜索，监控程序组合：

- 可访问性树中的标题、展示价格、地区和标签；
- 本地曝光数据库中的商品 ID、精确发布时间、关键词和列表索引。

技术设计与实测证据见 [`app-monitor/`](app-monitor/)。
