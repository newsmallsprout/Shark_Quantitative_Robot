import re

with open("rl/planning/planner.go", "r") as f:
    content = f.read()

comments = """// buildPlan — AI 主导（DeepSeek→数学审计兜底）
// 【核心逻辑说明】
// 1. 优先尝试调用 AI (DeepSeek) 模型生成交易计划。
// 2. 将当前价格、宏观因子、订单簿等环境数据构建为 prompt，交给 AI 判断。
// 3. AI 返回的结果会经过数学维度的交叉验证 (如：止损空间是否合理)。
// 4. 如果 AI 调用失败或验证不通过，则降级为纯数学指标生成的兜底计划。
// 5. 计划中包含: 方向(Bias), 入场带(EntryZone), 止损(StopLoss), 止盈(TakeProfit), 杠杆(Leverage)。"""

content = re.sub(r'// buildPlan — AI 主导[^\n]*\n// v3: AI 优先，失败降级到纯数学', comments, content)

with open("rl/planning/planner.go", "w") as f:
    f.write(content)
