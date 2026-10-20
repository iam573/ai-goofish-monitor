# App Monitor Guidelines

- 以 `docs/technical-design.md` 为当前架构基线，实验结论发生变化时先更新 `docs/validation.md`。
- 正式 App 负责生成请求；不伪造原生签名，不复制 Cookie、Token 或 WUA。
- 本地数据库只以 SQLite `mode=ro` 打开，不修改闲鱼 App 容器。
- 采集层只输出最小商品数据契约，通知和报价筛选通过独立模块接入。
- 监控规则是全局的（`rules.py`），搜索机型从最新报价表推导，不再新增手动任务；判价逻辑保持为 `matching.evaluate` 纯函数。
- 实验文件放在 `experiments/`，正式代码落地前不要把实验脚本当作生产入口。
- 正式代码位于 `src/app_monitor/`，管理后台位于 `web/`，使用 `uv run pytest` 验证。
