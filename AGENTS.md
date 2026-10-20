# Repository Guidelines

## 顶层结构

- `web-monitor/`：原有网页版闲鱼监控方案，包含 FastAPI、Playwright 爬虫、Vue Web UI、Docker 与测试。修改该目录时继续遵循 `web-monitor/AGENTS.md`。
- `app-monitor/`：已登录 Mac 版闲鱼监控方案。当前以技术设计和验证材料为主，实验代码位于 `app-monitor/experiments/`。
- `.github/`：仓库级工作流。网页 Docker 镜像的构建上下文是 `web-monitor/`。

## 工作原则

- 不在两个方案之间直接导入业务模块；需要复用时先定义稳定的数据契约。
- 网页方案的开发、测试和 Docker 命令均在 `web-monitor/` 下执行。
- App 方案优先使用正式 App 生成请求，只读本地可访问性数据和曝光数据库，不保存 Cookie、Token、签名或其他登录凭据。
- 不提交 `.env`、登录状态、App 容器数据库、截图中的个人信息或运行日志。

## 提交约定

- 使用 `feat(...)`、`fix(...)`、`refactor(...)`、`docs(...)`、`chore(...)` 等 Conventional Commits 风格。
- PR 说明变更影响的是 `web-monitor`、`app-monitor`，或仓库级基础设施，并列出相应验证。
