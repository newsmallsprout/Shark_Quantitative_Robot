"""
Signal Engine — 纯数学方向判定（v3）
从 RangePlan 的区间中点判定方向：价格<中点→做多，价格>中点→做空。
AI 委员会职责：计划生成 + 进化优化，不参与信号决策。
"""

from dataclasses import dataclass, field


@dataclass
class SignalResult:
    side: str = ""
    signal_src: str = ""
    ai_confidence: int = 0
    ai_use: bool = False
    learner_feat: list = field(default_factory=list)
    stop_mult: float = 2.0
    tp_mult: float = 3.0


class SignalEngine:
    """基于 RangePlan 区间的数学方向判定"""

    def decide(self, runner, sym: str, px: float, funding: float,
               change: float, vol: float, cfg: dict, now: float,
               regime_cache: dict, _regime) -> SignalResult:

        r = SignalResult()

        plan = None
        if runner._plan_gate:
            plan = runner._plan_gate.get_plan(sym)

        if not plan:
            return r  # 无计划 = 不交易

        bias = plan.get("bias", "")

        if bias == "both":
            # 震荡：中点为界，低吸高抛
            mid = (plan.get("range_low", 0) + plan.get("range_high", 0)) / 2
            if px < mid:
                r.side = "long"
                r.signal_src = f"计划震荡 价{px:.0f}<中{mid:.0f}→多"
            else:
                r.side = "short"
                r.signal_src = f"计划震荡 价{px:.0f}>中{mid:.0f}→空"

        elif bias in ("long", "short"):
            # 趋势：跟计划方向
            r.side = bias
            r.signal_src = f"计划趋势 {bias}"

        return r
