"""
雷达猎物战术扩展点（未默认接入引擎）：

- 战术 A：CVD 堆积 + 卖盘断层 → Taker 点火，动量衰竭市价砸出。
- 战术 B：极高正费率 + 断崖下跌 → 深渊挂 Maker 网格接针，死猫跳约 2% 止盈。

实现时建议读 config.volume_radar + l1_fast_loop CVD/OBI + paper_engine；
叙事否决沿用 volume_radar.narrative_allow_entry 或独立 LLM 调用。
"""
