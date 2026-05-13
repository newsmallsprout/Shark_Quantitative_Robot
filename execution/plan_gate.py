"""FastLoop 门禁：读取 RangePlan → 判断是否允许开仓 / 熔断检查"""
import json
import time
from typing import Optional, Dict, List, Tuple


class PlanGate:
    """从 Redis 读取 RangePlan，在每个 tick 判断是否允许开仓"""

    def __init__(self, redis_client):
        self._redis = redis_client
        self._plan_cache: Dict[str, dict] = {}
        self._last_fetch: Dict[str, float] = {}
        self._fuse_paused_until = 0.0
        self._fuse_reason = ""
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}
        self._last_all_fetch = 0.0

    def get_plan(self, symbol: str) -> Optional[dict]:
        now = time.time()
        if symbol in self._plan_cache and now - self._last_fetch.get(symbol, 0) < 5:
            return self._plan_cache[symbol]
        try:
            raw = self._redis.get(f"shark:plan:{symbol}")
            if raw:
                plan = json.loads(raw) if isinstance(raw, str) else json.loads(raw.decode())
                self._plan_cache[symbol] = plan
                self._last_fetch[symbol] = now
                return plan
        except Exception:
            pass
        return None

    def get_all_plans(self) -> Dict[str, dict]:
        now = time.time()
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
                    self._plan_cache[sym] = plan
            self._last_all_fetch = now
        except Exception:
            pass
        return dict(self._plan_cache)

    def check_fuse(self, prices: Dict[str, float]) -> Optional[str]:
        now = time.time()
        if self._fuse_paused_until > 0 and now > self._fuse_paused_until:
            self._fuse_paused_until = 0
            self._fuse_reason = ""
        if self._fuse_paused_until > now:
            return self._fuse_reason
        for sym, px in prices.items():
            if px <= 0:
                continue
            if sym not in self._price_history:
                self._price_history[sym] = []
            self._price_history[sym].append((now, px))
            cutoff = now - 90
            self._price_history[sym] = [(t, p) for t, p in self._price_history[sym] if t > cutoff]
        for sym, px in prices.items():
            hist = self._price_history.get(sym, [])
            if len(hist) < 2:
                continue
            px_1m_ago = None
            for t, p in reversed(hist):
                if now - t >= 55:
                    px_1m_ago = p
                    break
            if px_1m_ago is None or px_1m_ago <= 0:
                continue
            chg_pct = abs((px - px_1m_ago) / px_1m_ago) * 100
            if chg_pct > 3.0:
                self._fuse_paused_until = now + 300
                self._fuse_reason = f"{sym} 1分钟波动{chg_pct:.1f}% > 3%阈值"
                return self._fuse_reason
        return None

    @property
    def is_fused(self) -> bool:
        return self._fuse_paused_until > time.time()

    @property
    def fuse_remaining(self) -> float:
        return max(0, self._fuse_paused_until - time.time()) if self.is_fused else 0

    @property
    def fuse_reason(self) -> str:
        return self._fuse_reason if self.is_fused else ""

    # ── 开仓门禁 ──

    def can_open(self, symbol: str, side: str, px: float) -> tuple:
        """判断是否允许开仓。返回 (允许:bool, 原因:str)"""
        if self.is_fused:
            return False, f"熔断保护中({self._fuse_reason})"

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

        # 双方向：必须在入场带内才开仓（硬门禁）
        if bias == "both":
            range_low = plan.get("range_low", 0)
            range_high = plan.get("range_high", 0)
            if px < range_low or px > range_high:
                return False, f"价格({px:.0f})不在区间[{range_low:.0f},{range_high:.0f}]"
            if side == "long":
                entry_low = plan.get("long_entry_low", 0)
                entry_high = plan.get("long_entry_high", 0)
            else:
                entry_low = plan.get("short_entry_low", 0)
                entry_high = plan.get("short_entry_high", 0)
            # 双向计划的 long/short entry zone 只作为倾向参考；开仓速度优先，区间门禁已足够。
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
