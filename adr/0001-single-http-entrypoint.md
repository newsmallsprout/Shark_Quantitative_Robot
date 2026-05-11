# ADR-0001: 单一 HTTP / WebSocket 对外入口

## 状态

已采纳（2026-05-09）

## 上下文

- 仓库内曾存在两套 FastAPI 应用：
  - **`main.py`**：`app = FastAPI(...)`，与 `StrategyRunner`、行情循环、`dialogue_ammo` 同进程启动；**Docker / `python main.py` 当前唯一正式路径**。
  - **`api/server.py`**：另一 `FastAPI` 实例 + 全局 `_orchestrator` / `_exchange`，通过 `create_api_server(...)` 启动；**源码树内无任何引用**，与 React 看板、Compose 默认流程脱节。

两套并存会导致：运维文档与实情不符、安全控制面重复、WS/REST 契约分叉、排障时误判「哪一个是真入口」。

## 决策

1. **唯一规范 ASGI 应用**：`main:app`（模块 `main`，属性 `app`）。
2. **启动方式**：`python main.py` 或 `uvicorn main:app --host 0.0.0.0 --port <port>`（需自行保证与策略循环同进程时只能用现有 `main()` 编排）。
3. **`api/server.py`**：移除可独立启动的第二套路由；保留模块仅作**迁移说明与显式失败**（若仍调用 `create_api_server`，立即 `RuntimeError` 并提示使用 `main`）。
4. 若将来需要「可嵌入的 API 包」，应在 `application` / `interfaces/http` 下从 **`main` 抽取路由工厂**，而不是再复制一份 `FastAPI()`。

## 后果

- **正面**：单一路径、单一路由表、安全与观测只接一处。
- **负面**：曾依赖 `from api.server import app` 的外部脚本（本仓库未检出）需改为 `from main import app` 或自建挂载。

## 参考

- `docker-compose.yml` → `CMD ["python", "main.py"]`
- `Dockerfile` 未以 `api.server` 为入口
