# 沙箱与混沌测试执行方案 (Chaos Testing Plan)

## 1. 沙箱仿真测试 (Paper Trading / Testnet)
当前网关已默认支持 Gate.io Testnet（通过 `sandbox_mode` 配置开关）。
**执行步骤**：
1. 启动 `main.py`。
2. 确保在前端或配置文件中开启 `sandbox_mode: true`。
3. 系统将连接到 `wss://fx-ws-testnet.gateio.ws` 和 `https://fx-api-testnet.gateio.ws`。
4. 让系统保持运行 24~72 小时，持续接收 WS 行情流，观察终端日志 `logs/` 是否出现死锁、内存溢出（OOM）或 `asyncio` 任务堆积。

## 2. 极限压力与灾难测试 (Chaos Testing)

### 2.1 断网测试 (Network Partition)
**目的**：验证 WebSocket 连接意外断开时的指数退避重连机制。
**执行步骤**：
1. 在运行过程中，断开物理网络或在服务器上执行 `sudo iptables -A OUTPUT -p tcp --dport 443 -j DROP`。
2. **预期表现**：
   - GateGateway 抛出 `WS Disconnected` 错误。
   - 网关进入指数退避（1s, 2s, 4s, 8s... max 30s）。
   - `RiskEngine` 由于收不到行情可能导致净值更新暂停，但已有的硬性止损应当能在网络恢复后第一时间触发。
3. 恢复网络：观察系统是否成功重连 `Connecting to Gate.io WS`，并自动重新发送订阅（`_send_subscription`）。

### 2.2 AI 宕机测试 (LLM Timeout Injection)
**目的**：验证耗时的 AI API 请求是否会阻塞高频交易主线程（发单、风控）。
**执行步骤**：
1. 故意将配置文件中的大模型 API 地址修改为一个不可达地址（如 `http://10.255.255.1`）或断网。
2. **预期表现**：
   - `MarketAnalyzer` 抛出 Timeout 错误并重试，由于我们采用了 `asyncio.create_task(market_analyzer.start())` 独立循环，它**绝对不会**阻塞 `StrategyEngine`。
   - 高频主线程继续处理毫秒级的 `process_ws_tick`，但读取的 AI 评分 `AIContext.get_latest_score()` 会保持过期状态，直到超时被判定为失效。

### 2.3 终极熔断测试 (Kill Switch / Panic Button)
**目的**：验证紧急情况下系统能否在几百毫秒内清空所有持仓并拒绝新订单。
**执行步骤**：
1. 在前端仪表盘狂按 **KILL SWITCH** 按钮，或者使用 curl 发送请求：
   `curl -X POST http://localhost:8002/api/control -H "Content-Type: application/json" -d '{"action":"KILL_SWITCH"}'`
2. **预期表现**：
   - 接口立即调用 `engine.pause()` 暂停后续策略信号的产生。
   - 立即执行 `exchange.close_all_positions()`，向交易所发送 `close: True` 或市价反向对冲单。
   - 在日志中观察 `KILL SWITCH ACTIVATED: Bot paused and all positions closed` 的耗时（应在毫秒级别）。
   - `RiskEngine` 即便未触发，整个系统也会处于 `paused` 状态，拒绝任何通过 REST 接口意外发出的新单。