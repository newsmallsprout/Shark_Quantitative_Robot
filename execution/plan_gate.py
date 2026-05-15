"""FastLoop 门禁：读取 RangePlan → 判断是否允许开仓 / 单币对熔断检查"""
import json
import time
from typing import Optional, Dict, List, Tuple


class PlanGate:
    """从 Redis 读取 RangePlan，在每个 tick 判断是否允许开仓。
    v2: 单币对独立熔断 — 一个币对异常不阻塞其他币对交易。触发后自动请求重规划。"""

    FUSE_COOLDOWN = 30  # 熔断冷却秒数（留给 Go planner 重规划时间）

    def __init__(self, redis_client):
        self._redis = redis_client
        self._plan_cache: Dict[str, dict] = {}
        self._last_fetch: Dict[str, float] = {}
        self._fuse_per_symbol: Dict[str, dict] = {}  # {symbol: {triggered_at, reason, chg_pct}}
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}
        self._last_all_fetch = 0.0

    # ── 计划读取与清理 ──

    def clear_plan(self, symbol: str) -> None:
        """清除过期或无效的计划"""
        self._plan_cache.pop(symbol, None)
        self._last_fetch.pop(symbol, None)
        try:
            self._redis.delete(f"shark:plan:{symbol}")
        except Exception:
            pass

    def get_plan(self, symbol: str) -> Optional[dict]:
        now = time.time()
        if symbol in self._plan_cache and now - self._last_fetch.get(symbol, 0) < 5:
            plan = self._plan_cache[symbol]
            if plan.get("valid_until", 0) and plan.get("valid_until", 0) < now:
                self.clear_plan(symbol)
                return None
            return plan
        try:
            raw = self._redis.get(f"shark:plan:{symbol}")
            if raw:
                plan = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                if plan.get("valid_until", 0) and plan.get("valid_until", 0) < now:
                    self.clear_plan(symbol)
                    return None
                self._plan_cache[symbol] = plan
                self._last_fetch[symbol] = now
                return plan
        except Exception:
            pass
        return None

    def get_all_plans(self) -> Dict[str, dict]:
        now = time.time()
        for sym, plan in list(self._plan_cache.items()):
            if plan.get("valid_until", 0) and plan.get("valid_until", 0) < now:
                self.clear_plan(sym)

        if self._plan_cache and now - self._last_all_fetch < 5:
            return dict(self._plan_cache)
        try:
            for key in self._redis.scan_iter(match="shark:plan:*", count=50):
                if isinstance(key, bytes):
                    key = key.decode()
                sym = key.replace("shark:plan:", "")
                raw = self._redis.get(key)
                if raw:
                    plan = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                    if plan.get("valid_until", 0) and plan.get("valid_until", 0) < now:
                        self.clear_plan(sym)
                    else:
                        self._plan_cache[sym] = plan
                        self._last_fetch[sym] = now
            self._last_all_fetch = now
        except Exception:
            pass
        return dict(self._plan_cache)

    # ── 单币对独立熔断 ──

    def check_fuse(self, prices: Dict[str, float]) -> Optional[List[str]]:
        """追踪每币对价格历史。触发熔断时：
        1. 标记该币对熔断（30秒冷却）
        2. 向 Go planner 发送重规划请求（Redis pub/sub）
        3. 返回新触发币对列表（供日志记录）
        
        不再全局阻塞 — 单币对阻塞在 can_open() 中处理。
        """
        now = time.time()

        # 清理过期熔断
        expired = [sym for sym, fs in self._fuse_per_symbol.items()
                   if now - fs["triggered_at"] >= self.FUSE_COOLDOWN]
        for sym in expired:
            del self._fuse_per_symbol[sym]

        # 追踪价格历史
        for sym, px in prices.items():
            if px <= 0:
                continue
            if sym not in self._price_history:
                self._price_history[sym] = []
            self._price_history[sym].append((now, px))
            cutoff = now - 90
            self._price_history[sym] = [(t, p) for t, p in self._price_history[sym] if t > cutoff]

        # 检测各币对熔断（只检查有计划的币对）
        triggered = []
        for sym, px in prices.items():
            if sym in self._fuse_per_symbol:
                continue  # 已在熔断冷却中

            # 无计划 → 不检查熔断（该币对不在交易范围内）
            plan = self.get_plan(sym)
            if plan is None:
                continue

            hist = self._price_history.get(sym, [])
            if len(hist) < 2:
                continue

            # 找 ~1分钟前的价格
            px_1m_ago = None
            for t, p in reversed(hist):
                if now - t >= 55:
                    px_1m_ago = p
                    break
            if px_1m_ago is None or px_1m_ago <= 0:
                continue

            chg_pct = abs((px - px_1m_ago) / px_1m_ago) * 100
            # 从计划中读取该币对的自适应熔断阈值
            threshold = plan.get("fuse_threshold_pct", 3.0)
            if chg_pct > threshold:
                self._fuse_per_symbol[sym] = {
                    "triggered_at": now,
                    "reason": f"1分钟波动{chg_pct:.1f}% > {threshold:.1f}%阈值",
                    "chg_pct": chg_pct,
                }
                # 请求 Go planner 重规划该币对
                try:
                    self._redis.publish("shark:plan:replan", json.dumps({"symbol": sym}))
                except Exception:
                    pass
                triggered.append(sym)

        return triggered if triggered else None

    def is_fused_for(self, symbol: str) -> bool:
        """检查指定币对是否在熔断冷却中"""
        fs = self._fuse_per_symbol.get(symbol)
        if fs is None:
            return False
        if time.time() - fs["triggered_at"] >= self.FUSE_COOLDOWN:
            del self._fuse_per_symbol[symbol]
            return False
        return True

    def fuse_reason_for(self, symbol: str) -> str:
        """返回指定币对熔断原因"""
        fs = self._fuse_per_symbol.get(symbol)
        if fs and self.is_fused_for(symbol):
            return fs.get("reason", "")
        return ""

    @property
    def is_fused(self) -> bool:
        """是否有任何币对在熔断中（前端面板聚合用）"""
        self.check_fuse({})  # 触发过期清理
        return len(self._fuse_per_symbol) > 0

    @property
    def fuse_remaining(self) -> float:
        """最长剩余熔断秒数（前端面板用）"""
        now = time.time()
        max_rem = 0.0
        for sym, fs in list(self._fuse_per_symbol.items()):
            rem = self.FUSE_COOLDOWN - (now - fs["triggered_at"])
            if rem > max_rem:
                max_rem = rem
            elif rem <= 0:
                del self._fuse_per_symbol[sym]
        return max(0, max_rem)

    @property
    def fuse_reason(self) -> str:
        """所有熔断币对的汇总原因（前端面板用）"""
        reasons = []
        for sym, fs in list(self._fuse_per_symbol.items()):
            if self.is_fused_for(sym):
                reasons.append(f"{sym}: {fs.get('reason', '')}")
        return "; ".join(reasons) if reasons else ""

    def get_fused_symbols(self) -> Dict[str, dict]:
        """返回所有熔断中币对的状态（供 API 和前端面板使用）"""
        result = {}
        for sym, fs in list(self._fuse_per_symbol.items()):
            if self.is_fused_for(sym):
                result[sym] = {
                    "reason": fs.get("reason", ""),
                    "remaining": max(0, self.FUSE_COOLDOWN - (time.time() - fs["triggered_at"])),
                    "chg_pct": fs.get("chg_pct", 0),
                }
        return result

    # ── 开仓门禁 ──

    def can_open(self, symbol: str, side: str, px: float) -> tuple:
        """判断是否允许开仓。返回 (允许:bool, 原因:str)"""
        # 单币对熔断检查（不再全局）
        if self.is_fused_for(symbol):
            return False, f"熔断保护中({self.fuse_reason_for(symbol)})"

        plan = self.get_plan(symbol)
        if plan is None:
            return False, f"无计划({symbol})"

        if plan.get("valid_until", 0) < time.time():
            return False, "计划过期"

        state = plan.get("state", "")
        if state in ("BOOTSTRAP", "PAUSED", "REPLAN_PENDING"):
            return False, f"状态={state}"

        if plan.get("news_risk_level", 0) >= 2:
            return False, f"风险={plan.get('news_risk_level')}"

        bias = plan.get("bias", "")
        if side not in ("long", "short"):
            return False, f"方向无效({side})"

        # 双方向：激进快开，只拦大区间外；入场带外由 Python 风险降档处理。
        if bias == "both":
            range_low = plan.get("range_low", 0)
            range_high = plan.get("range_high", 0)
            if px < range_low or px > range_high:
                return False, f"价格({px:.0f})不在区间[{range_low:.0f},{range_high:.0f}]"
        elif bias in ("long", "short"):
            if side != bias:
                return False, f"方向不匹配(plan={bias}, side={side})"
            range_low = plan.get("range_low", 0)
            range_high = plan.get("range_high", 0)
            if range_low > 0 and range_high > 0 and (px < range_low or px > range_high):
                return False, f"价格({px:.0f})不在区间[{range_low:.0f},{range_high:.0f}]"
        else:
            return False, f"计划方向无效({bias})"

        # Beta过滤：BTC下行趋势 → 山寨禁多
        if symbol not in ("BTC/USDT", "ETH/USDT"):
            btc_plan = self.get_plan("BTC/USDT")
            if btc_plan and btc_plan.get("macro_regime") == "trend_down" and side == "long":
                return False, "BTC下跌趋势，山寨禁多"

        return True, "OK"

    def get_stop_loss(self, symbol: str, side: str = "long") -> float:
        """获取计划止损价，bias=both 时按 side 返回"""
        plan = self.get_plan(symbol)
        if plan is None:
            return 0
        bias = plan.get("bias", "")
        if bias == "both":
            return plan.get("long_stop_loss" if side == "long" else "short_stop_loss", 0)
        return plan.get("stop_loss", 0)

    def get_plan_range(self, symbol: str) -> tuple:
        plan = self.get_plan(symbol)
        if plan is None:
            return 0, 0
        return plan.get("range_low", 0), plan.get("range_high", 0)
