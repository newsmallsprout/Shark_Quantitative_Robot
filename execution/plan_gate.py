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
        self._fuse_paused_until = 0.0        # 全局熔断截止时间
        self._fuse_reason = ""                # 熔断原因
        self._price_history: Dict[str, List[Tuple[float, float]]] = {}  # sym → [(ts, px), ...]
        self._last_all_fetch = 0.0

    # ── 计划读取 ──

    def get_plan(self, symbol: str) -> Optional[dict]:
        """读取计划，缓存5秒避免频繁 Redis 调用"""
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
        """获取所有计划（从 Redis 扫描），用于前端看板"""
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

    # ── 熔断 ──

    def check_fuse(self, prices: Dict[str, float]) -> Optional[str]:
        """
        检查所有币对的1分钟价格变化，触发熔断则返回原因。
        调用位置：tick() 开头。
        """
        now = time.time()

        # 熔断恢复检查
        if self._fuse_paused_until > 0 and now > self._fuse_paused_until:
            self._fuse_paused_until = 0
            self._fuse_reason = ""

        # 已在熔断中 → 跳过检查
        if self._fuse_paused_until > now:
            return self._fuse_reason

        # 收集价格历史
        for sym, px in prices.items():
            if px <= 0:
                continue
            if sym not in self._price_history:
                self._price_history[sym] = []
            self._price_history[sym].append((now, px))
            # 只保留最近90秒
            cutoff = now - 90
            self._price_history[sym] = [(t, p) for t, p in self._price_history[sym] if t > cutoff]

        # 检查1分钟波动
        for sym, px in prices.items():
            hist = self._price_history.get(sym, [])
            if len(hist) < 2:
                continue

            # 找约60秒前的价格
            px_1m_ago = None
            for t, p in reversed(hist):
                if now - t >= 55:  # ~1分钟
                    px_1m_ago = p
                    break
            if px_1m_ago is None or px_1m_ago <= 0:
                continue

            chg_pct = abs((px - px_1m_ago) / px_1m_ago) * 100
            if chg_pct > 3.0:
                self._fuse_paused_until = now + 300  # 5分钟熔断
                self._fuse_reason = f"{sym} 1分钟波动{chg_pct:.1f}% > 3%阈值"
                return self._fuse_reason

        return None

    @property
    def is_fused(self) -> bool:
        """当前是否熔断中"""
        return self._fuse_paused_until > time.time()

    @property
    def fuse_remaining(self) -> float:
        """熔断剩余秒数"""
        if not self.is_fused:
            return 0
        return max(0, self._fuse_paused_until - time.time())

    @property
    def fuse_reason(self) -> str:
        return self._fuse_reason if self.is_fused else ""

    # ── 开仓门禁 ──

    def can_open(self, symbol: str, side: str, px: float) -> tuple:
        """判断是否允许开仓。返回 (允许:bool, 原因:str)"""
        # 1. 全局熔断
        if self.is_fused:
            return False, f"熔断保护中({self._fuse_reason})"

        # 2. 读取计划
        plan = self.get_plan(symbol)
        if plan is None:
            return False, f"无计划({symbol})"

        # 3. 过期检查
        if plan.get("valid_until", 0) < time.time():
            return False, "计划过期"

        # 4. 暂停/风险检查
        if plan.get("state") == "PAUSED" or plan.get("news_risk_level", 0) >= 2:
            return False, f"暂停/风险={plan.get('news_risk_level')}"

        # 5. 方向匹配
        plan_bias = plan.get("bias", "")
        if plan_bias != side and plan_bias != "neutral":
            return False, f"方向不匹配(计划={plan_bias} 信号={side})"

        # 6. 价格走廊
        entry_low = plan.get("entry_zone_low", 0)
        entry_high = plan.get("entry_zone_high", 0)
        if px < entry_low or px > entry_high:
            return False, f"价格({px:.0f})不在入场带[{entry_low:.0f}, {entry_high:.0f}]"

        # 7. Beta过滤：BTC下行趋势 → 山寨禁多
        if symbol not in ("BTC/USDT", "ETH/USDT"):
            btc_plan = self.get_plan("BTC/USDT")
            if btc_plan and btc_plan.get("macro_regime") == "trend_down" and side == "long":
                return False, "BTC下跌趋势，山寨禁多"

        return True, "OK"

    def get_stop_loss(self, symbol: str, px: float) -> float:
        """获取计划止损价"""
        plan = self.get_plan(symbol)
        if plan is None:
            return 0
        return plan.get("stop_loss", 0)

    def get_plan_range(self, symbol: str) -> tuple:
        """获取计划区间 (low, high)"""
        plan = self.get_plan(symbol)
        if plan is None:
            return 0, 0
        return plan.get("range_low", 0), plan.get("range_high", 0)
