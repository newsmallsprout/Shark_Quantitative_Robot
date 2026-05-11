"""
止损反思引擎 — 每单亏损后自动诊断原因，识别模式并调整战术
嵌入 main.py StrategyRunner，平仓时自动触发
"""

import time


class LossReason:
    SIGNAL_WEAK = "signal_weak"         # AI低置信/兜底信号
    REGIME_FLIP = "regime_flip"          # 行情反转（趋势→震荡）
    STOP_TOO_TIGHT = "stop_too_tight"    # 止损被正常波动穿透
    BAD_ENTRY = "bad_entry"              # 入场在区间极值（追高/抄底）
    WRONG_DIR = "wrong_dir"              # 方向错误（开仓后秒亏）
    MICRO_LOSS = "micro_loss"            # 微利被手续费吃掉
    NORMAL = "normal"                    # 正常亏损


class Reflector:
    """止损反思器，累积模式后触发战术调整"""

    def __init__(self):
        self.counters = {r: 0 for r in [
            LossReason.SIGNAL_WEAK, LossReason.REGIME_FLIP,
            LossReason.STOP_TOO_TIGHT, LossReason.BAD_ENTRY,
            LossReason.WRONG_DIR, LossReason.MICRO_LOSS, LossReason.NORMAL,
        ]}
        self.total_losses = 0
        self.total_wins = 0
        self.last_adjust_tick = 0
        self.ai_boost = 0           # AI阈值临时提高量
        self.stop_boost = 0         # 止损临时放宽量（百分点）
        self.signal_throttle = 0    # 兜底信号临时禁用计数器

    def analyze(self, sym, pos, realized, pnl_pct, reason, px,  # noqa: PLR0917
                regime_cache: dict, kline_cache) -> list:
        """
        分析单笔亏损，返回识别到的问题标签列表
        仅在亏损时调用
        """
        if realized >= 0:
            self.total_wins += 1
            return []

        self.total_losses += 1
        tags = []

        rc = regime_cache.get(sym, {})
        diag = rc.get("diag", {})
        rsi_entry = diag.get("rsi", 50)
        pos_in_range = diag.get("pos", 50)
        regime_entry = rc.get("regime", "?")
        signal_src = pos.get("signal_src", "")

        # 1. 信号质量
        ai_conf = pos.get("ai_confidence", 0)
        if ai_conf and ai_conf < 55:
            self.counters[LossReason.SIGNAL_WEAK] += 1
            tags.append(LossReason.SIGNAL_WEAK)
        elif "兜底" in str(signal_src):
            self.counters[LossReason.SIGNAL_WEAK] += 1
            tags.append(LossReason.SIGNAL_WEAK)

        # 2. 止损是否被正常波动穿透（实际亏损 > 入场波动 × 2.5）
        vol_at_entry = abs(pos.get("vol_chg", 3))
        actual_loss = abs(pnl_pct)
        if actual_loss > vol_at_entry * 2.5:
            self.counters[LossReason.STOP_TOO_TIGHT] += 1
            tags.append(LossReason.STOP_TOO_TIGHT)

        # 3. 入场位置（高位做多 / 低位做空）
        side = pos.get("side", "")
        if (side == "long" and pos_in_range > 75) or (side == "short" and pos_in_range < 25):
            self.counters[LossReason.BAD_ENTRY] += 1
            tags.append(LossReason.BAD_ENTRY)

        # 4. 方向错误（持仓<60秒就亏）
        opened = pos.get("opened", 0)
        held = time.time() - opened if opened else 999
        if held < 60:
            self.counters[LossReason.WRONG_DIR] += 1
            tags.append(LossReason.WRONG_DIR)

        # 5. 微利被手续费吃掉（亏损 < $0.02）
        if abs(realized) < 0.02:
            self.counters[LossReason.MICRO_LOSS] += 1
            tags.append(LossReason.MICRO_LOSS)

        # 无匹配 → 正常亏损
        if not tags:
            self.counters[LossReason.NORMAL] += 1
            tags.append(LossReason.NORMAL)

        # 日志
        tag_str = ", ".join(tags)
        print(
            f"[反思] {sym} {side} 亏{realized:.4f}({pnl_pct:.1f}%) "
            f"原因: {tag_str} | 行情={regime_entry} RSI={rsi_entry:.0f} "
            f"区间位={pos_in_range:.0f} 持{held:.0f}s",
            flush=True,
        )

        return tags

    def maybe_adjust(self) -> dict:
        """累积足够样本后触发战术调整，返回调整指令"""
        total = self.total_losses
        if total < 5:
            return {}  # 样本不够（放宽至5笔）

        adjustments = {}

        # 信号质量差（>50%亏损来自弱信号）→ 提高AI阈值
        weak_rate = self.counters[LossReason.SIGNAL_WEAK] / total
        if weak_rate > 0.5 and self.ai_boost < 10:
            self.ai_boost += 5
            adjustments["ai_boost"] = self.ai_boost
            print(f"[进化] 信号质量差({weak_rate:.0%})，AI阈值临时+{self.ai_boost}", flush=True)

        # 止损过紧（>40%亏损被波动穿透）→ 放宽止损
        tight_rate = self.counters[LossReason.STOP_TOO_TIGHT] / total
        if tight_rate > 0.4 and self.stop_boost < 4:
            self.stop_boost += 1.5
            adjustments["stop_boost"] = self.stop_boost
            print(f"[进化] 止损过紧({tight_rate:.0%})，止损放宽+{self.stop_boost}%", flush=True)

        # 入场差（>30%买在极值）→ 增加入场过滤
        entry_rate = self.counters[LossReason.BAD_ENTRY] / total
        if entry_rate > 0.3:
            adjustments["entry_filter"] = True
            print(f"[进化] 入场位差({entry_rate:.0%})，增加RSI入场过滤", flush=True)

        # 方向错误（>40%秒亏）→ 暂停兜底信号
        wrong_rate = self.counters[LossReason.WRONG_DIR] / total
        if wrong_rate > 0.4:
            self.signal_throttle = min(self.signal_throttle + 5, 20)
            adjustments["signal_throttle"] = self.signal_throttle
            print(f"[进化] 方向错误({wrong_rate:.0%})，兜底冷却+{self.signal_throttle}s", flush=True)

        # 微利亏损（>25%被手续费吃掉）→ 提高止盈门槛
        micro_rate = self.counters[LossReason.MICRO_LOSS] / total
        if micro_rate > 0.25:
            adjustments["tp_boost"] = True
            print(f"[进化] 微利亏损({micro_rate:.0%})，提高止盈净利门槛", flush=True)

        # 重置计数器（保留调整状态）
        if adjustments:
            self.counters = {r: 0 for r in self.counters}
            self.total_losses = 0
            self.total_wins = 0
            self.last_adjust_tick = time.time()

        return adjustments

    def summary(self) -> str:
        """返回当前反思统计"""
        total = self.total_losses
        if total == 0:
            return "无亏损记录"
        parts = []
        for reason, count in self.counters.items():
            if count > 0:
                parts.append(f"{reason}={count}({count/total:.0%})")
        boosts = []
        if self.ai_boost:
            boosts.append(f"AI+{self.ai_boost}")
        if self.stop_boost:
            boosts.append(f"SL+{self.stop_boost}%")
        return f"亏损{total}笔 " + " | ".join(parts) + (f" 调整:{','.join(boosts)}" if boosts else "")
