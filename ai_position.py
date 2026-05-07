"""Shark 2.0 AI 仓位管理 — 多层建仓 + 分批止盈 + 动态防守"""

import time

class AIPositionManager:
    """AI 驱动的多层仓位管理"""
    
    def __init__(self):
        self.positions: dict = {}  # sym -> layered position state

    def init_position(self, sym: str, entry_price: float, side: str,
                      total_margin: float, leverage: int,
                      ai_plan: dict) -> dict:
        """初始化多层仓位：1/3 底仓 + 2/3 预留"""
        base_margin = total_margin * 0.35  # 35% 开底仓
        reserve = total_margin - base_margin  # 65% 预留给加仓/补仓
        
        pos = {
            "sym": sym, "side": side,
            "entry_price": entry_price,
            "total_margin": total_margin,
            "used_margin": base_margin,
            "reserve_margin": reserve,
            "leverage": leverage,
            "size": (base_margin * leverage) / entry_price,
            "opened_at": time.time(),
            "layers": [{
                "price": entry_price,
                "margin": base_margin,
                "type": "base",
                "size": (base_margin * leverage) / entry_price,
            }],
            # AI 目标
            "targets": ai_plan.get("targets", []),
            "support_levels": ai_plan.get("supports", []),
            "resistance_levels": ai_plan.get("resistances", []),
            "stop_loss": ai_plan.get("stop_loss"),
            "add_zone": ai_plan.get("add_zone"),
            "reduce_zone": ai_plan.get("reduce_zone"),
            # 状态
            "best_price": entry_price,
            "trailing_stop": None,
            "phase": "entry",  # entry / adding / partial_tp / full_tp / defending
            "partial_tp_done": False,
            "confidence": ai_plan.get("confidence", 60),
        }
        return pos

    def check_layers(self, pos: dict, current_price: float, volume_ratio: float = 1.0) -> list:
        """检查所有层级触发条件，返回待执行动作列表"""
        actions = []
        side, entry = pos["side"], pos["entry_price"]
        
        # 计算盈亏
        if side == "long":
            pnl_pct = (current_price - entry) / entry * 100
            price_move = current_price - entry
        else:
            pnl_pct = (entry - current_price) / entry * 100
            price_move = entry - current_price

        # 1. 绝对止损
        if pos["stop_loss"]:
            sl = pos["stop_loss"]
            triggered = (side == "long" and current_price <= sl) or (side == "short" and current_price >= sl)
            if triggered:
                actions.append({"type": "hard_stop", "price": current_price, "reason": f"触发止损{sl}"})
                return actions

        # 2. 更新最高价（移动止盈用）
        if (side == "long" and current_price > pos["best_price"]) or \
           (side == "short" and current_price < pos["best_price"]):
            pos["best_price"] = current_price
            # 止损移到保本
            if pos["trailing_stop"] is None and pnl_pct > 1.5:
                pos["trailing_stop"] = entry
                pos["phase"] = "adding"

        # 3. 移动止盈
        if pos["trailing_stop"]:
            ts = pos["trailing_stop"]
            trail_hit = (side == "long" and current_price <= ts) or (side == "short" and current_price >= ts)
            if trail_hit and pnl_pct > 0:
                actions.append({"type": "trailing_stop", "price": current_price, "reason": "移动止盈"})

        # 4. 分层目标
        for t in pos["targets"]:
            tp = t.get("price", 0)
            action_type = t.get("action", "take_profit")
            ratio = t.get("ratio", 0.5)
            
            hit = (side == "long" and current_price >= tp) or (side == "short" and current_price <= tp)
            if hit:
                if action_type == "pyramid_add" and pos["phase"] in ("entry", "adding"):
                    # 浮盈加仓
                    add_margin = pos["reserve_margin"] * 0.4
                    if add_margin > 0.5 and pos["reserve_margin"] >= add_margin:
                        actions.append({
                            "type": "pyramid_add",
                            "price": current_price,
                            "margin": add_margin,
                            "reason": f"突破{tp}，成交量确认"
                        })
                        pos["reserve_margin"] -= add_margin
                        pos["phase"] = "adding"
                elif action_type == "take_profit":
                    if not pos.get("partial_tp_done") and ratio < 0.8:
                        # 部分止盈
                        actions.append({
                            "type": "partial_tp",
                            "price": current_price,
                            "ratio": ratio,
                            "reason": f"触及阻力{tp}，平{ratio*100:.0f}%"
                        })
                        pos["partial_tp_done"] = True
                        pos["trailing_stop"] = tp * 0.99 if side == "long" else tp * 1.01
                        pos["phase"] = "partial_tp"
                    elif pos.get("partial_tp_done"):
                        # 最终止盈
                        actions.append({
                            "type": "full_tp",
                            "price": current_price,
                            "reason": f"终极目标{tp}"
                        })
                        pos["phase"] = "full_tp"

        # 5. 防守区
        if pos["add_zone"]:
            az = pos["add_zone"].get("price", 0)
            in_zone = (side == "long" and current_price <= az) or (side == "short" and current_price >= az)
            if in_zone and pos["phase"] != "defending" and pnl_pct < 0:
                # 判断是缩量回调还是放量暴跌
                if volume_ratio < 1.2:  # 缩量 → 补仓
                    add_m = pos["reserve_margin"] * 0.3
                    if add_m > 0.5 and pos["reserve_margin"] >= add_m:
                        actions.append({
                            "type": "dip_add",
                            "price": current_price,
                            "margin": add_m,
                            "reason": "缩量回调补仓"
                        })
                        pos["reserve_margin"] -= add_m
                        pos["phase"] = "defending"
                else:  # 放量 → 减仓
                    actions.append({
                        "type": "reduce",
                        "price": current_price,
                        "ratio": 0.3,
                        "reason": "放量下跌减仓"
                    })
                    pos["phase"] = "defending"

        return actions

    def execute(self, pos: dict, action: dict) -> dict:
        """执行动作，返回执行结果"""
        return {"pos": pos, "action": action, "executed": True}
