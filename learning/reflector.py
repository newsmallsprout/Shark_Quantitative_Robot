"""
止损反思引擎 v2 — AI驱动多维诊断
每笔亏损后：本地快速分类 + AI深度分析 → 立即调整下笔策略
"""

import time


class LossReason:
    SIGNAL_WEAK = "signal_weak"
    REGIME_FLIP = "regime_flip"
    STOP_TOO_TIGHT = "stop_too_tight"
    BAD_ENTRY = "bad_entry"
    WRONG_DIR = "wrong_dir"
    MICRO_LOSS = "micro_loss"
    NORMAL = "normal"


class Reflector:
    """止损反思器：本地快速分类 + AI深度诊断 → 实时调整"""

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
        self.ai_insights = []       # AI深度分析历史
        self._last_ai_call = 0      # 节流AI调用

    def analyze(self, sym, pos, realized, pnl_pct, reason, px,
                regime_cache: dict, kline_cache) -> list:
        """快速本地分类 + 触发AI深度分析"""
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

        # 2. 止损过紧
        vol_at_entry = abs(pos.get("vol_chg", 3))
        actual_loss = abs(pnl_pct)
        if actual_loss > vol_at_entry * 2.5:
            self.counters[LossReason.STOP_TOO_TIGHT] += 1
            tags.append(LossReason.STOP_TOO_TIGHT)

        # 3. 入场位置
        side = pos.get("side", "")
        if (side == "long" and pos_in_range > 75) or (side == "short" and pos_in_range < 25):
            self.counters[LossReason.BAD_ENTRY] += 1
            tags.append(LossReason.BAD_ENTRY)

        # 4. 方向错误
        opened = pos.get("opened", 0)
        held = time.time() - opened if opened else 999
        if held < 60:
            self.counters[LossReason.WRONG_DIR] += 1
            tags.append(LossReason.WRONG_DIR)

        # 5. 微利
        if abs(realized) < 0.02:
            self.counters[LossReason.MICRO_LOSS] += 1
            tags.append(LossReason.MICRO_LOSS)

        if not tags:
            self.counters[LossReason.NORMAL] += 1
            tags.append(LossReason.NORMAL)

        tag_str = ", ".join(tags)
        print(
            f"[反思] {sym} {side} 亏{realized:.4f}({pnl_pct:.1f}%) "
            f"原因: {tag_str} | 行情={regime_entry} RSI={rsi_entry:.0f} "
            f"区间位={pos_in_range:.0f} 持{held:.0f}s",
            flush=True,
        )
        return tags

    def build_ai_prompt(self, sym, pos, realized, pnl_pct, reason, px,
                        regime_cache: dict, local_tags: list) -> str:
        """构建AI深度诊断的prompt"""
        rc = regime_cache.get(sym, {})
        diag = rc.get("diag", {})
        regime = rc.get("regime", "unknown")

        # 收集近期的调整状态
        adjustments = []
        if self.ai_boost:
            adjustments.append(f"AI阈值已+{self.ai_boost}")
        if self.stop_boost:
            adjustments.append(f"止损已放宽{self.stop_boost}%")

        return f"""你是量化交易策略分析师。分析这笔亏损的深层原因并给出具体的参数调整建议。

【交易信息】
币对: {sym}
方向: {pos.get('side', '?')}
入场价: {pos.get('entry', 0):.4f}
平仓价: {px:.4f}
亏损: {realized:.4f} USDT ({pnl_pct:.1f}%)
持仓时长: {time.time() - pos.get('opened', time.time()):.0f}秒
信号源: {pos.get('signal_src', 'unknown')}
AI置信度: {pos.get('ai_confidence', 0)}
原因标签: {', '.join(local_tags)}

【市场环境】
行情类型: {regime}
RSI: {diag.get('rsi', '?')}
价格区间位: {diag.get('pos', '?')}%
成交量变化: {pos.get('vol_chg', '?')}%
资金费率: {diag.get('funding', '?')}

【当前调整状态】
{chr(10).join(adjustments) if adjustments else '无历史调整'}

请输出JSON格式（只输出JSON，不要其他内容）：
{{
  "root_cause": "一句话根因（中文）",
  "dimensions": ["维度1分析", "维度2分析", ...],
  "adjustments": {{
    "ai_threshold": 数字,    // AI阈值调整（当前{self.ai_boost}，建议+5/-5/0）
    "stop_boost": 数字,      // 止损放宽（当前{self.stop_boost}%，建议值）
    "entry_filter": true/false, // 是否加强入场过滤
    "cooldown_bonus": 数字,  // 冷却增加（秒）
    "tp_boost": true/false   // 是否提高止盈门槛
  }},
  "confidence": 数字,        // 0-100 诊断置信度
  "next_action": "下笔建议（中文）"
}}"""

    def apply_ai_adjustments(self, adjustments: dict) -> str:
        """应用AI返回的调整建议"""
        msgs = []
        if "ai_threshold" in adjustments:
            delta = adjustments["ai_threshold"]
            if delta != 0:
                self.ai_boost = max(0, self.ai_boost + delta)
                msgs.append(f"AI阈值→{self.ai_boost}")
        if "stop_boost" in adjustments:
            self.stop_boost = float(adjustments["stop_boost"])
            msgs.append(f"止损→+{self.stop_boost}%")
        if adjustments.get("entry_filter"):
            msgs.append("入场过滤开启")
        if "cooldown_bonus" in adjustments:
            msgs.append(f"冷却+{adjustments['cooldown_bonus']}s")
        if adjustments.get("tp_boost"):
            msgs.append("止盈门槛提高")
        return ", ".join(msgs)

    def maybe_adjust(self) -> dict:
        """累积足够样本后返回统计摘要（不再实时改参，Go SlowLoop 统一驱动）"""
        total = self.total_losses
        if total < 5:
            return {}

        summary = {}

        weak_rate = self.counters[LossReason.SIGNAL_WEAK] / total
        if weak_rate > 0.5:
            summary["ai_boost_hint"] = f"信号弱{weak_rate:.0%}"

        tight_rate = self.counters[LossReason.STOP_TOO_TIGHT] / total
        if tight_rate > 0.4:
            summary["stop_hint"] = f"止损紧{tight_rate:.0%}"

        entry_rate = self.counters[LossReason.BAD_ENTRY] / total
        if entry_rate > 0.3:
            summary["entry_hint"] = f"入场差{entry_rate:.0%}"

        wrong_rate = self.counters[LossReason.WRONG_DIR] / total
        if wrong_rate > 0.4:
            summary["dir_hint"] = f"方向错{wrong_rate:.0%}"

        micro_rate = self.counters[LossReason.MICRO_LOSS] / total
        if micro_rate > 0.25:
            summary["micro_hint"] = f"微亏多{micro_rate:.0%}"

        if summary:
            self.counters = {r: 0 for r in self.counters}
            self.total_losses = 0
            self.total_wins = 0
            self.last_adjust_tick = time.time()

        return summary

    def summary(self) -> str:
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
