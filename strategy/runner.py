
import os
from core.config import settings
SHARK_SIGNAL_SOURCE = settings.SHARK_SIGNAL_SOURCE
AI_ENABLED = settings.AI_ENABLED
from character.dialogue import pop_line, trade_category_for_open
import random

import os, time, json, math, logging, uuid
_log = logging.getLogger(__name__)
from api.routes import get_state
from typing import Dict, List, Optional, Any
from strategy.session import SessionMixin
from strategy.plans import PlanMixin
from strategy.risk import RiskMixin
from strategy.close import CloseMixin
from strategy.state import StateMixin
from persistence.bridge import PersistenceBridge
from execution.plan_gate import PlanGate
from execution.order_command import build_rl_order_command, build_order_command
from market.data import ContractSpec, TAKER_FEE, MAKER_FEE, MAX_TOTAL_EXPOSURE
from strategy.dual import get_config, is_stable, is_high_vol_alt, get_capital_limit, trading_track_allows_open

try:
    from market.kline import KlineCache, init_kline_cache, get_kline_cache
    from market.regime import RegimeDetector, REGIME_CONFIG, init_detector, get_detector
    from learning.reflector import Reflector, LossReason
    from learning.online import OnlineLearner
    from core.live import LiveEngine, create_live_engine
    KLINE_ENABLED = True
except ImportError:
    KLINE_ENABLED = False


def _plan_authority_enabled() -> bool:
    v = os.environ.get("SHARK_PLAN_AUTHORITY", "").strip().lower()
    return v in ("1", "true", "yes", "on", "strict")

# 看板娘事件序号
# character sequence state removed

class StrategyRunner(SessionMixin, PlanMixin, RiskMixin, CloseMixin, StateMixin):
    def __init__(
        self, initial_balance=10000.0, persistence: Optional[PersistenceBridge] = None
    ):
        self._initial_capital = float(initial_balance)
        self.balance = initial_balance
        self.equity = initial_balance
        self.static_equity = initial_balance  # 已实现权益（不含浮盈）
        self.peak_static_equity = initial_balance  # static_equity 历史峰值
        self.positions: Dict[str, dict] = {}
        self.realized_pnl = 0.0
        self.gross_realized = 0.0  # 毛利累计（不含手续费）
        self.trades = 0  # 总开仓次数
        self.closed_trades = 0  # 总平仓次数
        self.wins = 0  # 盈利平仓次数
        self.total_fees = 0.0
        self.total_slippage = 0.0
        self._fuse_sl_streak: Dict[str, int] = {}  # 单币对连续止损笔数
        self._log: List[str] = []
        self._trade_history: List[dict] = []
        self._contract_specs: Dict[str, ContractSpec] = {}
        self._ai_signal_cache: Dict[str, dict] = {}  # sym -> {plan, timestamp}
        self._open_timestamps: list = []  # 开仓时间戳
        self._regime_cache: Dict[str, dict] = {}  # sym → {regime, diag, cfg} 行情上下文
        self._reflector = Reflector() if KLINE_ENABLED else None  # 止损反思器
        self._learner = OnlineLearner() if KLINE_ENABLED else None  # 在线学习器
        self._live = create_live_engine()  # 实盘引擎（paper模式返回None）
        self._live_trading_enabled = False  # 默认不开实盘，需前端手动开启
        self._paper_trading_enabled = False  # 默认不开模拟盘，需前端手动开启
        self._warmup_ticks = 0  # 启动预热计数器
        self._warmup_done = False  # 预热完成标志
        self._pending_evo_changes = []  # 待审批的进化修改
        self._evo_cooldown_types: Dict[str, float] = {}  # type → cooldown_until
        self._evo_change_id = 0  # 修改ID计数器
        self._evo_margin_mult = 1.0  # 进化保证金倍率
        self._evo_skip_alts = False  # 进化暂停山寨
        self._evo_cooldown_bonus = 0  # 进化额外冷却
        self._persistence = persistence
        self._plan_gate = None  # FastLoop 门禁，由 main() 注入
        self._loss_replay_guard: Dict[str, dict] = {}
        self._price_replan_last: Dict[str, float] = {}
        # 山寨币独立进化状态（动态币对，首次见到自动初始化）
        self._alt_evo: Dict[
            str, dict
        ] = {}  # sym → {gen, plans, wins, stops, atr_mult, stop_mult, tp_mult}
        self._last_tick_block: Optional[dict] = None
        self._block_log_ts: Dict[str, float] = {}
        if _plan_authority_enabled():
            _log.info(
                "📌 SHARK_PLAN_AUTHORITY 已启用：RangePlan 开仓后 Python 不覆盖 SL/TP/仓位/杠杆（不含 alt_dynamic）"
            )

    def _get_maker_fee(self, sym: str) -> float:
        """从合约API获取实时maker费率"""
        spec = self._contract_specs.get(sym)
        if spec and spec.maker_fee < 0:
            return abs(spec.maker_fee)  # 负费率=返佣
        if spec:
            return spec.maker_fee
        return MAKER_FEE

    async def _ai_reflect(self, sym, pos, realized, pnl_pct, reason, px, local_tags):
        """AI深度诊断亏损原因 → 多维度调整策略"""
        try:
            import aiohttp

            prompt = self._reflector.build_ai_prompt(
                sym, pos, realized, pnl_pct, reason, px, self._regime_cache, local_tags
            )
            # 优先用 DeepSeek（便宜快速）
            api_key = (
                os.environ.get("DEEPSEEK_API_KEY")
                or os.environ.get("QWEN_KEY")
                or os.environ.get("VOLC_KEY")
            )
            if not api_key:
                return
            endpoint = "https://api.deepseek.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 400,
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as resp:
                    if resp.status != 200:
                        return
                    data = await resp.json()
                    content = data["choices"][0]["message"]["content"]
            # 提取JSON
            start = content.find("{")
            end = content.rfind("}") + 1
            if start >= 0 and end > start:
                result = json.loads(content[start:end])
                adj = result.get("adjustments", {})
                if adj:
                    msg = self._reflector.apply_ai_adjustments(adj)
                    if msg:
                        _log.info(f"[AI调整] {msg}")
                    self._reflector.ai_insights.append(
                        {
                            "sym": sym,
                            "ts": time.time(),
                            "cause": result.get("root_cause", ""),
                            "adjustments": adj,
                            "confidence": result.get("confidence", 0),
                        }
                    )
        except Exception as e:
            _log.info(f"[AI反思] 调用失败: {e}")

    async def _fetch_ai_plan(
        self, sym: str, px: float, funding: float, change: float, vol: float
    ):
        """异步获取AI多层仓位计划（无限流；由上游信号与熔断控制开仓）"""
        now = time.time()
        try:
            pack = await get_ai_targets(sym, px, change, vol, funding)
            plan = pack[0] if isinstance(pack, (list, tuple)) and pack else None
            if isinstance(plan, dict) and plan.get("targets"):
                # 存入信号缓存（开仓前用）
                self._ai_signal_cache[sym] = {"plan": plan, "ts": now}
                if sym in self.positions:
                    pos = self.positions[sym]
                    if pos.get("plan_stick"):
                        return
                    # 存储完整AI计划（多层仓位管理用）
                    pos["ai_plan"] = plan
                    pos["ai_targets"] = plan["targets"]
                    pos["ai_stop"] = plan.get("stop_loss")
                    pos["ai_entry"] = plan.get("entry_price", px)
                    conf = plan.get("confidence", 0)
                    rr = plan.get("risk_reward", 0)
                    _log.info(
                        f"[AI] {sym} 置信{conf} 盈亏比{rr:.1f} "
                        f"支撑{plan.get('supports', [])} 阻力{plan.get('resistances', [])}"
                    )
            else:
                # 否决/HOLD/无计划：清缓存，避免 120s 内沿用过期 LONG/SHORT
                self._ai_signal_cache.pop(sym, None)
        except Exception:
            pass

    def update_contracts(self, specs: Dict[str, ContractSpec]):
        self._contract_specs = specs

    def _persist_margin_delta(
        self,
        prices: Dict[str, float],
        sym: str,
        pos: dict,
        delta_free_cash: float,
        event_type: str,
        note: str,
    ) -> None:
        if not self._persistence or not self._persistence.enabled_db():
            return
        oid = pos.get("order_id")
        if isinstance(oid, str):
            try:
                oid = uuid.UUID(oid)
            except ValueError:
                oid = None
        self._persistence.on_balance_adjustment(
            self,
            prices,
            event_type=event_type,
            delta_free_cash=delta_free_cash,
            sym=sym,
            note=note,
            order_id=oid,
        )

    def _quanto_for(self, sym: str) -> float:
        sp = self._contract_specs.get(sym)
        return float(sp.quanto_multiplier) if sp else 1.0

    def merge_evo_suggestion(self, change: dict) -> None:
        """合并 Go evolution 建议：同 type 仅保留一条，保留 id/params 与 Redis 一致便于 approve。
        审批/拒绝后的冷却期内（5min）同类型建议直接丢弃。"""
        ctype = str(change.get("type") or "unknown")

        # ── 冷却期检查（内存 + get_state() 双检，消除 approve/reject 竞态）──
        now = time.time()
        cooldown_until = self._evo_cooldown_types.get(ctype, 0)
        state_cd = (get_state().get("evo_cooldowns") or {}).get(ctype, 0)
        if state_cd > cooldown_until:
            cooldown_until = state_cd
        if now < cooldown_until:
            return  # 冷却中，丢弃
        raw_id = change.get("id")
        cid: Optional[int] = None
        if raw_id is not None:
            try:
                cid = int(raw_id)
            except (TypeError, ValueError):
                cid = None
        if cid is None:
            self._evo_change_id += 1
            cid = self._evo_change_id
        else:
            self._evo_change_id = max(self._evo_change_id, cid)
        # 操作 get_state()["evo_pending"]（唯一真相源），同时同步 runner 用于前端展示
        pending = _state.setdefault("evo_pending", [])
        get_state()["evo_pending"] = [c for c in pending if c.get("type") != ctype]
        params = change.get("params")
        if not isinstance(params, dict):
            params = {}
        created = change.get("created_at")
        try:
            created_f = float(created) if created is not None else time.time()
        except (TypeError, ValueError):
            created_f = time.time()
        get_state()["evo_pending"].append(
            {
                "id": cid,
                "type": ctype,
                "description": str(change.get("description") or ""),
                "params": params,
                "created_at": created_f,
            }
        )

    def _apply_evo_change(self, change: dict):
        """应用审批通过的进化修改"""
        ct = change.get("type", "")
        params = change.get("params", {})
        if ct == "margin_mult":
            self._evo_margin_mult = params.get("value", self._evo_margin_mult)
            _log.info(f"[进化] 保证金倍率 → {self._evo_margin_mult}")
        elif ct == "skip_alts":
            self._evo_skip_alts = params.get("value", self._evo_skip_alts)
            _log.info(f"[进化] 暂停山寨 → {self._evo_skip_alts}")
        elif ct == "cooldown_bonus":
            self._evo_cooldown_bonus = params.get("value", self._evo_cooldown_bonus)
            _log.info(f"[进化] 额外冷却 → {self._evo_cooldown_bonus}")
        elif ct == "ai_threshold":
            # 更新 Reflector 的 AI 阈值
            if self._reflector:
                self._reflector.ai_boost = params.get("value", self._reflector.ai_boost)
            _log.info(f"[进化] AI阈值 → {params.get('value')}")
        elif ct == "ga_best_params":
            # Go RL 引擎 GA 最优参数
            if "margin_pct" in params:
                self._evo_margin_mult = params["margin_pct"] / 0.02  # 转换为倍率
            if "stop_atr_mult" in params and self._reflector:
                self._reflector.stop_boost = params["stop_atr_mult"]
            if "max_drawdown_limit" in params:
                pass  # 记录但不自动应用（需人工确认）
            _log.info(
                f"[进化] GA最优参数已应用 (fitness={params.get('fitness', '?')})"
            )
        else:
            _log.info(f"[进化] 未知类型 {ct}，跳过")

    def _strategic_entry(
        self, sym: str, side: str, px: float, regime_value: str
    ) -> float:
        """根据行情类型计算策略性入场价：趋势市回调入场，震荡市边界入场，突破市追入"""
        try:
            kc = get_kline_cache() if KLINE_ENABLED else None
            if not kc:
                return px

            highs, lows = kc.get_high_low(sym, "5m")
            closes = kc.get_close(sym, "5m")
            if len(closes) < 10:
                return px

            hh = max(highs[-20:])
            ll = min(lows[-20:])
            ema9 = kc.ema(sym, 9, "5m")
            rng = hh - ll

            if "strong_trend" in regime_value:
                if "up" in regime_value and side == "long":
                    target = ema9 if ema9 < px else px * 0.995
                    return max(target, px * 0.99)
                elif "down" in regime_value and side == "short":
                    target = ema9 if ema9 > px else px * 1.005
                    return min(target, px * 1.01)

            elif "weak_trend" in regime_value:
                if "up" in regime_value and side == "long":
                    return px * 0.997
                elif "down" in regime_value and side == "short":
                    return px * 1.003

            elif "ranging" in regime_value:
                if side == "long":
                    target = ll + rng * 0.3
                    return max(target, px * 0.985)
                else:
                    target = hh - rng * 0.3
                    return min(target, px * 1.015)

            elif "breakout" in regime_value:
                if "up" in regime_value and side == "long":
                    return min(hh * 1.002, px * 1.01)
                elif "down" in regime_value and side == "short":
                    return max(ll * 0.998, px * 0.99)

            return px
        except Exception:
            return px

    async def _ai_build_alt_plan(
        self, sym: str, px: float, change_abs: float, volume: float, funding: float
    ) -> Optional[dict]:
        """山寨AI计划：单次DeepSeek调用，失败返回None回退数学"""
        try:
            import aiohttp
            import os

            key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
            if not key:
                return None
            prompt = (
                f"你是超短线山寨币交易员。{sym} 现价{px} 24h波动{change_abs:+.1f}% "
                f"24h成交量{volume:,.0f} 资金费率{funding * 100:+.4f}%。"
                f"输出JSON: bias(both/long/short), long_entry_low, long_entry_high, "
                f"long_sl, long_tp1, long_tp2, short_entry_low, short_entry_high, "
                f"short_sl, short_tp1, short_tp2, leverage(20-50), rationale(≤25字)。"
                f"默认bias=both双向区间，仅强单边(波动>12%+费率>0.020%)才用long/short。"
                f"入场带≈现价±0.5%-1.2%，止盈≈止损的2倍(风险收益比≥1:2)。"
            )
            async with aiohttp.ClientSession() as s:
                async with s.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": "deepseek-chat",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 400,
                        "temperature": 0.3,
                        "response_format": {"type": "json_object"},
                    },
                    timeout=aiohttp.ClientTimeout(total=12),
                ) as resp:
                    if resp.status != 200:
                        return None
                    data = await resp.json()
                    text = data["choices"][0]["message"]["content"]
                    import json as _json

                    ai = _json.loads(text) if isinstance(text, str) else text
            return ai if isinstance(ai, dict) else None
        except Exception:
            return None

    def _est_fee_usd(
        self, sym: str, pos: dict, px: float, fee_rounds: float = 3.0
    ) -> float:
        """按当前名义估算平仓侧手续费倍数（与止盈里原 *3 口径一致）。"""
        q = self._quanto_for(sym)
        fee_r = self._get_maker_fee(sym)
        return pos["size"] * q * px * fee_r * fee_rounds

    def _take_profit_net_ok(
        self, sym: str, pos: dict, px: float, fee_rounds: float = 3.0
    ) -> bool:
        """毛利扣估算手续费后仍有意义，避免 net > est_fee 的翻倍门槛锁死大单止盈。"""
        gross = self._gross_pnl_usd(sym, pos, px)
        est = self._est_fee_usd(sym, pos, px, fee_rounds)
        net = gross - est
        return net >= max(0.05, 0.25 * est)

    def _planned_stop_pnl_pct(self, pos: dict, stop_price: float) -> float:
        """Convert a planned stop price into leveraged PnL%, matching pnl_pct comparisons."""
        entry = float(pos.get("entry") or 0)
        lev = max(float(pos.get("leverage") or 1), 1.0)
        if entry <= 0 or stop_price <= 0:
            return 0.0
        if pos.get("side") == "long":
            return -((entry - stop_price) / entry) * lev * 100
        return -((stop_price - entry) / entry) * lev * 100

    def _planned_take_profit_pnl_pct(
        self, pos: dict, take_profit_price: float
    ) -> float:
        """Convert a planned TP price into leveraged PnL%, matching pnl_pct comparisons."""
        entry = float(pos.get("entry") or 0)
        lev = max(float(pos.get("leverage") or 1), 1.0)
        if entry <= 0 or take_profit_price <= 0:
            return 0.0
        if pos.get("side") == "long":
            return ((take_profit_price - entry) / entry) * lev * 100
        return ((entry - take_profit_price) / entry) * lev * 100

    def _apply_stop_loss_fuse(
        self, sym: str, reason: str, pos: Optional[dict] = None
    ) -> None:
        """单币对连续止损只告警，不阻止下一单立即开。"""
        cfg = get_config(sym)
        fuse_limit = cfg.get("fuse_sl_streak_limit", 3)
        r = str(reason)
        is_sl = "止损" in r and "止盈" not in r
        if is_sl:
            st = self._fuse_sl_streak.get(sym, 0) + 1
            self._fuse_sl_streak[sym] = st
            if st >= fuse_limit:
                signature = (pos or {}).get("plan_signature")
                if signature:
                    self._loss_replay_guard[sym] = {
                        "signature": signature,
                        "ts": time.time(),
                    }
                self._fuse_sl_streak[sym] = 0
                self._request_symbol_replan(sym, f"连续止损{fuse_limit}次")
        else:
            self._fuse_sl_streak[sym] = 0
            self._loss_replay_guard.pop(sym, None)

    def _note_tick_block(
        self, code: str, detail: str, *, log_every_sec: float = 25.0
    ) -> None:
        """记录本轮暂停新开仓的原因，并节流打日志（避免刷屏）。"""
        now = time.time()
        self._last_tick_block = {"code": code, "detail": detail, "ts": now}
        last = self._block_log_ts.get(code, 0.0)
        if now - last >= log_every_sec:
            self._block_log_ts[code] = now
            line = f"[交易暂停] {code}: {detail}"
            _log.info(line)
            _log.warning("%s", line)

    async def tick(
        self,
        prices: Dict[str, float],
        volumes: Dict[str, float],
        changes: Dict[str, float],
        funding_rates: Dict[str, float],
        mark_prices: Dict[str, float] = None,
    ):
        now = time.time()

        from api.routes import state_lock
        async with state_lock:
            # 同步实盘/模拟盘开关
            self._live_trading_enabled = get_state().get("live_trading", False)
            self._paper_trading_enabled = get_state().get("paper_trading", False)
            self._last_tick_block = None
    
            # 处理审批通过的进化修改
            evo_apply = get_state().pop("evo_apply", None)
            
            # 处理进化冷却队列（审批/拒绝后5分钟不重复推送同类型）
            cooldowns = list(get_state().pop("evo_cooldown_queue", []))
            
            # 处理模式切换请求（前端点击切换 paper/live）
            switch_req = get_state().pop("switch_mode_request", None)
            
            # 处理模拟盘重置请求
            reset_req = get_state().pop("paper_reset_request", None)
            
            # 停止交易 → 平掉所有持仓
            live_close_all = get_state().pop("live_close_all", False)
            paper_close_all = get_state().pop("paper_close_all", False)

        if evo_apply:
            self._apply_evo_change(evo_apply)

        for item in cooldowns:
            self._evo_cooldown_types[item["type"]] = item["until"]

        if switch_req:
            result = self.switch_mode(switch_req)
            if "error" in result:
                _log.info(f"[模式切换] 失败: {result['error']}")

        if reset_req:
            self._reset_paper(reset_req["capital"])

        # 发布价格到 Redis（Go matcher 撮合用，必须在开仓前）
        _redis_ok = True
        try:
            import redis as _rp

            _r = _rp.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            for sym, px in prices.items():
                if px > 0:
                    _r.set(f"shark:price:{sym}", px, ex=10)
        except Exception:
            _redis_ok = False
            try:
                from execution.prod_alert import alert_redis_down

                asyncio.create_task(alert_redis_down())
            except Exception:
                pass

        # 停止交易 → 平掉所有持仓
        if live_close_all:
            for sym in list(self.positions):
                px = prices.get(sym, 0)
                if px > 0:
                    self._close_position(sym, px, "手动停止", 0, prices)
            _log.info("[实盘] 已平掉所有持仓")
        if paper_close_all:
            for sym in list(self.positions):
                px = prices.get(sym, 0)
                if px > 0:
                    self._close_position(sym, px, "手动停止(模拟)", 0, prices)
            _log.info("[模拟] 已平掉所有持仓")

        # 偶尔飙句骚话调节气氛（5%概率/tick，不在交易时触发）
        if len(self.positions) == 0 and random.random() < 0.05:
            speech = pop_line("boring")
            if speech:
                from api.routes import state_lock
                async with state_lock:
                    get_state()["character_event"] = {
                        "Event_Type": "闲聊",
                        "Speech_Text": speech,
                        "Facial_Expression": "idle",
                        "Emotion_Index": 5,
                    }

        # ── 实盘：定期同步 + 对账（不因 API 熔断整段跳过；止盈止损仍按行情跑）──
        if self._live and self._live.active:
            if now - self._live._last_sync > 60:
                mismatches = self._live.reconcile(self.positions)
                if mismatches:
                    try:
                        from execution.prod_alert import _send_slack

                        asyncio.create_task(
                            _send_slack(
                                f"🔴 [Shark] 持仓对账不一致: {'; '.join(mismatches[:3])}"
                            )
                        )
                    except Exception:
                        pass

        # 检查持仓：动态止损 / 移动止盈 / 浮盈加仓
        for sym in list(self.positions):
            pos = self.positions[sym]
            px = prices.get(sym, 0)
            if px <= 0:
                continue

            if pos["side"] == "long":
                pnl_pct = (px - pos["entry"]) / pos["entry"] * pos["leverage"] * 100
                price_move = (px - pos["entry"]) / pos["entry"]
            else:
                pnl_pct = (pos["entry"] - px) / pos["entry"] * pos["leverage"] * 100
                price_move = (pos["entry"] - px) / pos["entry"]

            # 获取策略配置
            cfg = get_config(sym)
            is_st = is_stable(sym)

            # ── 计划方向反转检查 (Plan Reversal Check) ──
            if self._plan_gate:
                latest_plan = self._plan_gate.get_plan(sym)
                if latest_plan:
                    new_side, _, _, _ = self._side_from_plan(latest_plan, px)
                    if new_side and new_side != pos["side"]:
                        current_sig = pos.get("plan_signature")
                        latest_gen = latest_plan.get("generated_at")
                        is_new_plan = True
                        if current_sig and isinstance(current_sig, tuple) and len(current_sig) > 0:
                            if current_sig[0] == latest_gen:
                                is_new_plan = False
                        
                        if is_new_plan:
                            self._close_position(
                                sym, px, f"计划方向反转(持{pos['side']}新{new_side})", pnl_pct, prices
                            )
                            continue

            # 行情止损覆盖（开仓时判定的行情类型）
            _rc = self._regime_cache.get(sym, {}).get("cfg", {})
            _stop_mult = _rc.get("stop_atr_mult", 2.0)
            _tp_mult = _rc.get("tp_atr_mult", 3.0)
            if cfg.get("hold_profile") == "swing":
                try:
                    _tp_mult = max(
                        float(_tp_mult), float(cfg.get("tp_atr_mult") or _tp_mult)
                    )
                except Exception:
                    pass

            # ── ATR 实时止损/止盈（5分钟ATR，避免1分钟噪声）──
            vol_chg = abs(pos.get("vol_chg", 3.0))
            atr_pct = 0.0
            try:
                kc = get_kline_cache() if KLINE_ENABLED else None
                if kc:
                    atr_val = kc.atr(sym, period=14, interval="5m")
                    if atr_val > 0 and px > 0:
                        atr_pct = atr_val / px * 100
            except Exception:
                pass
            if atr_pct <= 0:
                atr_pct = vol_chg * 0.3  # 日波动30% ≈ 5m ATR

            # ATR 侧：sl_raw / tp_raw 是「价格波动百分比」(例如 2.0 = 2% 价格)
            # pnl_pct 是「杠杆盈亏百分比」，二者必须先换算再比较，否则会 -2% 杠杆就平仓
            lev_f = max(float(pos.get("leverage") or 1), 1.0)
            sl_raw = atr_pct * _stop_mult
            sl_floor = 2.0  # 最低 2% 价格波动（再乘杠杆得到杠杆侧止损）
            sl_raw = max(sl_raw, sl_floor)
            _sl_boost = self._reflector.stop_boost if self._reflector else 0
            dyn_sl = -((sl_raw + _sl_boost) * lev_f)
            dyn_sl = max(dyn_sl, -95.0)

            tp_raw = max(atr_pct * _tp_mult, 3.0)
            dyn_tp = tp_raw * lev_f

            # ── 计划精确 SL/TP 优先 ──
            _psl = pos.get("plan_sl")
            _ptp = pos.get("plan_tp")
            if _psl and isinstance(_psl, (int, float)) and _psl > 0:
                plan_sl_pct = self._planned_stop_pnl_pct(pos, float(_psl))
                if -95 <= plan_sl_pct <= -0.5:
                    dyn_sl = plan_sl_pct
            if _ptp:
                if isinstance(_ptp, list) and len(_ptp) > 0:
                    _tp_first = _ptp[0]
                elif isinstance(_ptp, (int, float)):
                    _tp_first = _ptp
                else:
                    _tp_first = None
                if _tp_first and _tp_first > 0:
                    plan_tp_pct = self._planned_take_profit_pnl_pct(
                        pos, float(_tp_first)
                    )
                    if 0.5 <= plan_tp_pct <= 500:
                        dyn_tp = plan_tp_pct

            # 移动止盈：主流中长线更慢，山寨短线更贴
            trail_trigger = max(atr_pct * 1.5, 2.0)  # 超短线：更低阈值
            trail_ratio = 0.3
            try:
                trail_trigger = max(trail_trigger, float(cfg.get("trail_trigger") or 0))
                trail_ratio = float(cfg.get("trail_pct") or trail_ratio)
            except Exception:
                pass

            # 更新最高盈利
            if pnl_pct > pos.get("best_pnl", -999):
                pos["best_pnl"] = pnl_pct
                pos["best_price"] = px

            best_pnl = pos.get("best_pnl", pnl_pct)

            # ── 平仓风控集中管理 ──
            from strategy.risk import RiskValidator
            should_close, close_reason = RiskValidator.check_close_conditions(
                sym=sym,
                pos=pos,
                px=px,
                pnl_pct=pnl_pct,
                best_pnl=best_pnl,
                dyn_tp=dyn_tp,
                dyn_sl=dyn_sl,
                cfg=cfg,
                gross_usd=self._gross_pnl_usd(sym, pos, px),
                est_fee=self._est_fee_usd(sym, pos, px, fee_rounds=3.0),
                is_stable=is_stable(sym),
                take_profit_net_ok=self._take_profit_net_ok(sym, pos, px)
            )
            
            if should_close:
                self._close_position(sym, px, close_reason, pnl_pct, prices)
                continue

            # ── AI 多层仓位管理（主逻辑） ──
            ai_plan = pos.get("ai_plan")
            if ai_plan and not pos.get("plan_stick"):
                pside = pos["side"]
                # 1. AI 止损（含方向校验）
                ai_sl = ai_plan.get("stop_loss")
                if ai_sl:
                    # 方向校验：做多止损应在 entry 下方，做空在上方
                    sl_valid = (pside == "long" and ai_sl < pos["entry"]) or (
                        pside == "short" and ai_sl > pos["entry"]
                    )
                    sl_hit = (pside == "long" and px <= ai_sl) or (
                        pside == "short" and px >= ai_sl
                    )
                    if sl_valid and sl_hit:
                        self._close_position(
                            sym, px, f"AI止损{ai_sl:.2f}", pnl_pct, prices
                        )
                        continue
                    elif not sl_valid and sl_hit:
                        # 止损价在盈利方向 → 当作止盈触发
                        self._close_position(
                            sym, px, f"AI目标{ai_sl:.2f}", pnl_pct, prices
                        )
                        continue

                # 2. AI 防守区
                def_zone = ai_plan.get("add_zone", {})
                if def_zone:
                    dz_price = def_zone.get("price", 0)
                    in_defense = (pside == "long" and px <= dz_price) or (
                        pside == "short" and px >= dz_price
                    )
                    if in_defense and pnl_pct < 0 and not pos.get("defense_used"):
                        # 成交量判断：缩量补仓，放量减仓
                        sym_vol = volumes.get(sym, 0)
                        avg_vols = [
                            volumes.get(s, 0) for s in list(volumes.keys())[:20]
                        ]
                        med_vol = (
                            sorted(avg_vols)[len(avg_vols) // 2]
                            if avg_vols
                            else sym_vol
                        )
                        vol_ratio = sym_vol / max(med_vol, 1)
                        if vol_ratio < 1.2:  # 缩量 → 补仓
                            add_m = min(pos["margin"] * 0.3, self.balance * 0.02, 2.0)
                            if add_m >= 0.3 and self.balance > add_m + pos["margin"]:
                                q_df = self._quanto_for(sym)
                                add_s = (add_m * pos["leverage"]) / max(q_df * px, 1e-9)
                                pos["margin"] += add_m
                                self.balance -= add_m
                                pos["size"] += add_s
                                pos["entry"] = (
                                    pos["entry"] * (pos["size"] - add_s) + px * add_s
                                ) / pos["size"]
                                pos["defense_used"] = True
                                self.trades += 1
                                _log.info(f"[AI防守] {sym} 缩量补仓 {add_m:.2f}@ {px:.4f}")
                                self._persist_margin_delta(
                                    prices,
                                    sym,
                                    pos,
                                    -add_m,
                                    "margin_add",
                                    "ai_defense_add",
                                )
                        else:  # 放量 → 减仓
                            reduce_ratio = 0.3
                            reduce_s = pos["size"] * reduce_ratio
                            pos["size"] -= reduce_s
                            pos["margin"] *= 1 - reduce_ratio
                            pos["defense_used"] = True
                            _log.info(f"[AI防守] {sym} 放量减仓 {reduce_ratio * 100:.0f}%")

                # 3. AI 目标层（按价格排序）
                targets = sorted(
                    ai_plan.get("targets", []), key=lambda t: t.get("price", 0)
                )
                for t in targets:
                    tp = t.get("price", 0)
                    act_type = t.get("action", "take_profit")
                    ratio = t.get("ratio", 0.5)
                    hit = (pside == "long" and px >= tp) or (
                        pside == "short" and px <= tp
                    )
                    if not hit:
                        continue
                    layer_key = f"layer_{tp:.0f}"
                    if pos.get(layer_key):
                        continue  # 已执行

                    if act_type == "pyramid_add" and pos.get("pyramid_count", 0) < 4:
                        # ── 利润垫加仓：先收割再博弈 ──
                        # 步骤A：平掉 30% 底仓落袋利润
                        harvest_ratio = 0.3
                        harvest_size = pos["size"] * harvest_ratio
                        qh = self._quanto_for(sym)
                        harvest_pnl = (
                            harvest_size * qh * (px - pos["entry"])
                            if pside == "long"
                            else harvest_size * qh * (pos["entry"] - px)
                        )
                        # 扣 Maker 手续费
                        fee_r = self._get_maker_fee(sym)
                        harvest_fee = harvest_size * qh * px * fee_r
                        net_harvest = harvest_pnl - harvest_fee

                        if net_harvest <= 0:
                            continue  # 不够覆盖手续费，不动作

                        # 执行收割：减仓 + 入账利润
                        pos["size"] -= harvest_size
                        pos["margin"] *= 1 - harvest_ratio
                        self.balance += net_harvest
                        self.realized_pnl += net_harvest
                        self.gross_realized += harvest_pnl  # 毛利（不含手续费）
                        self.total_fees += harvest_fee
                        self.closed_trades += 1
                        if net_harvest > 0:
                            self.wins += 1
                        self._persist_margin_delta(
                            prices,
                            sym,
                            pos,
                            net_harvest,
                            "partial_realize",
                            "ai_harvest_30pct",
                        )

                        _log.info(
                            f"[AI收割] {sym} 平{harvest_ratio * 100:.0f}%落袋 ${net_harvest:+.4f}"
                        )

                        # 步骤B：用利润作最大回撤额度加仓
                        add_margin = min(
                            net_harvest * 2, pos["margin"] * 0.5, total_account_equity * 0.05
                        )
                        
                        # ── 加仓前：检查该加仓操作是否会突破所在币种类型的资金桶上限 ──
                        total_account_equity = self.balance + sum(p["margin"] for p in self.positions.values())
                        bucket_cap = get_capital_limit(total_account_equity, sym)
                        bucket_used = sum(
                            p["margin"] for s, p in self.positions.items() if is_stable(s) == is_stable(sym)
                        )
                        
                        if bucket_used + add_margin > bucket_cap:
                            # 将加仓金额砍到只剩桶里剩余的额度，如果不剩了就设为 0
                            add_margin = max(0.0, bucket_cap - bucket_used)
                            
                        if add_margin >= 0.3 and self.balance > add_margin:
                            add_size = (add_margin * pos["leverage"]) / max(
                                qh * px, 1e-9
                            )
                            pos["margin"] += add_margin
                            self.balance -= add_margin
                            pos["size"] += add_size
                            open_fee_est = add_size * qh * px * fee_r
                            self.balance -= open_fee_est
                            self.total_fees += open_fee_est
                            pos["entry"] = (
                                pos["entry"] * (pos["size"] - add_size) + px * add_size
                            ) / pos["size"]
                            pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                            self.trades += 1

                            # 全局止损移至初始开仓价（保本）
                            pos["trailing_stop"] = pos.get("ai_entry", pos["entry"])
                            pos[layer_key] = True
                            _log.info(
                                f"[AI加仓] {sym} 用利润${net_harvest:.4f} 加仓${add_margin:.2f} @{px:.4f} 止损→保本"
                            )
                            self._persist_margin_delta(
                                prices,
                                sym,
                                pos,
                                -(add_margin + open_fee_est),
                                "margin_add",
                                "ai_pyramid_profit_add",
                            )
                    elif act_type == "take_profit":
                        if ratio >= 0.8:  # 终极止盈 → 全平
                            fee_r = self._get_maker_fee(sym)
                            qp = self._quanto_for(sym)
                            est_fee = pos["size"] * qp * px * fee_r * 2
                            net_pnl = pos["margin"] * pnl_pct / 100 - est_fee
                            if net_pnl > est_fee * 5:  # 微利即走
                                self._close_position(
                                    sym, px, f"AI终极止盈{tp:.2f}", pnl_pct, prices
                                )
                                continue
                        elif ratio > 0 and pnl_pct > 0:  # 部分止盈：有利润就行
                            reduce_s = pos["size"] * ratio
                            pos["size"] -= reduce_s
                            pos["margin"] *= 1 - ratio
                            pos[layer_key] = True
                            pos["trailing_stop"] = (
                                px * 0.99 if pside == "long" else px * 1.01
                            )
                            _log.info(
                                f"[AI止盈] {sym} 部分{ratio * 100:.0f}% @{px:.4f} 余{pos['size']:.4f}"
                            )

            # 现有逻辑兜底 ──
            # 浮盈加仓后检查AI目标价
            ai_targets = pos.get("ai_targets")
            if ai_targets and not pos.get("plan_stick"):
                actions = apply_ai_targets(pos, px, ai_targets, sym, self)
                for act in actions:
                    if act["type"] == "take_profit":
                        # 微利即走：用统一手续费校验
                        if self._take_profit_net_ok(sym, pos, px):
                            self._close_position(
                                sym, px, f"AI目标{act['price']:.2f}", pnl_pct, prices
                            )
                            break
                    elif (
                        act["type"] == "pyramid_add" and pos.get("pyramid_count", 0) < 3
                    ):
                        add_m = pos["margin"] * 0.5
                        if add_m >= 0.5 and self.balance > add_m:
                            q_at = self._quanto_for(sym)
                            add_s = (add_m * pos["leverage"]) / max(q_at * px, 1e-9)
                            pos["margin"] += add_m
                            self.balance -= add_m
                            pos["size"] += add_s
                            pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                            self.trades += 1
                            # 加仓不单独扣费，费用已含在开仓中
                            self._persist_margin_delta(
                                prices,
                                sym,
                                pos,
                                -add_m,
                                "margin_add",
                                "ai_targets_pyramid",
                            )
            # 金字塔加仓（仅主流币）
            pyramid_max = cfg.get("pyramid_levels", 0)
            if (
                not pos.get("plan_stick")
                and pyramid_max > 0
                and pnl_pct > vol_chg
                and pos.get("pyramid_count", 0) < pyramid_max
            ):
                funding = funding_rates.get(sym, 0)
                signal_valid = (
                    (pos["side"] == "short" and funding > 0.0001)
                    or (pos["side"] == "long" and funding < -0.0001)
                    or abs(funding) <= 0.0001  # 中性信号维持原方向
                )
                if signal_valid and self.balance > pos["margin"] * 1.2:
                    add_margin = pos["margin"] * 0.5
                    
                    # ── 加仓前：检查资金费率重叠加仓是否会突破所在币种类型的资金桶上限 ──
                    total_account_equity = self.balance + sum(p["margin"] for p in self.positions.values())
                    bucket_cap = get_capital_limit(total_account_equity, sym)
                    bucket_used = sum(
                        p["margin"] for s, p in self.positions.items() if is_stable(s) == is_stable(sym)
                    )
                    
                    if bucket_used + add_margin > bucket_cap:
                        # 超过上限，强制截断加仓金额，避免吸血
                        add_margin = max(0.0, bucket_cap - bucket_used)
                        
                    if add_margin >= 0.5 and self.balance > add_margin:
                        q_py = self._quanto_for(sym)
                        add_size = (add_margin * pos["leverage"]) / max(q_py * px, 1e-9)
                        pos["margin"] += add_margin
                        self.balance -= add_margin
                        pos["size"] += add_size
                        pos["pyramid_count"] = pos.get("pyramid_count", 0) + 1
                        pos["entry"] = (
                            pos["entry"] * (pos["size"] - add_size) + px * add_size
                        ) / pos["size"]
                        self.trades += 1
                        self._persist_margin_delta(
                            prices,
                            sym,
                            pos,
                            -add_margin,
                            "margin_add",
                            "funding_pyramid",
                        )

            # 保本止损：盈利 ≥ 阈值（杠杆后）→ 止损移至开仓价（会覆盖计划 SL；计划锁定时关闭）
            try:
                breakeven_trigger = float(cfg.get("breakeven_trigger") or 3.0)
            except Exception:
                breakeven_trigger = 3.0
            if (
                not pos.get("plan_stick")
                and pnl_pct >= breakeven_trigger
                and not pos.get("_breakeven_set")
            ):
                pos["_breakeven_set"] = True
                # 把计划止损价提升到 entry（相当于 dyn_sl = -0.01%，留手续费空间）
                if pos["side"] == "long":
                    pos["plan_sl"] = pos["entry"] * 1.0005
                else:
                    pos["plan_sl"] = pos["entry"] * 0.9995

            # 超时平仓已禁用

        # 计算当前总风险敞口
        total_margin = sum(p["margin"] for p in self.positions.values())
        # The total account equity (available balance + margin used by positions)
        total_account_equity = self.balance + total_margin
        
        available = self.balance
        if available <= 5.0:  # Allow a small buffer for dust instead of strict 0
            self._note_tick_block(
                "no_cash", "可用余额极低，暂停新开仓（持仓仍管理）", log_every_sec=45.0
            )
            self._update_state(prices)
            return

        # 开仓：对所有符合条件的币对尽可能开单
        # ── 预生成山寨计划（仅针对可开仓或已持仓的币对，清理无关过期计划）──
        for sym in list(get_state().get("dynamic_high_vol_alts", [])):
            if sym not in prices or prices[sym] <= 0:
                continue
                
            if sym not in self.positions:
                cfg = get_config(sym)
                from strategy.risk import RiskValidator
                can_open, _ = RiskValidator.can_open_position(
                    sym=sym,
                    cfg=cfg,
                    prices=prices,
                    volumes=volumes,
                    changes=changes,
                    total_margin=total_margin,
                    balance=self.balance,
                    positions=self.positions,
                    max_total_exposure=MAX_TOTAL_EXPOSURE,
                    total_account_equity=total_account_equity
                )
                if not can_open:
                    continue

            await self._ensure_alt_attack_plan(
                sym,
                prices[sym],
                abs(changes.get(sym, 0)),
                volumes.get(sym, 0),
                funding_rates.get(sym, 0),
            )

        # ── 开关检查：实盘/模拟盘都需手动开启 ──
        _is_live_mode = self._live and self._live.active
        _trade_enabled = True
        if _is_live_mode and not self._live_trading_enabled:
            _trade_enabled = False  # 实盘模式但开关关闭，不开新仓
        if not _is_live_mode and not self._paper_trading_enabled:
            _trade_enabled = False  # 模拟盘模式但开关关闭，不开新仓

        if not _trade_enabled:
            self._update_state(prices)
            return  # 如果不开新仓，直接跳过后面的开仓逻辑（不影响前面已经执行的持仓管理和平仓逻辑）

        # ── Fuse 熔断检查（单币对独立：触发→请求重规划→30秒冷却，不阻塞其他币对）──
        if self._plan_gate:
            triggered = self._plan_gate.check_fuse(prices)
            if triggered:
                for sym in triggered:
                    self._note_tick_block(
                        "price_fuse",
                        f"{sym}: {self._plan_gate.fuse_reason_for(sym)}",
                        log_every_sec=15.0,
                    )
                # 不 return — 单币对阻塞在 can_open() 中处理，其他币对继续交易

        # ── 启动预热：等K线+行情就绪后再开仓（持仓管理不受影响）──
        _can_open = True
        if not self._warmup_done:
            kc = get_kline_cache() if KLINE_ENABLED else None
            detector = get_detector() if KLINE_ENABLED else None
            _can_open = self._warmup_allows_open(
                has_kline=bool(kc), has_detector=bool(detector)
            )
        
        from strategy.risk import RiskValidator
        scored = []
        for sym in prices:
            if sym in self.positions:
                continue

            cfg = get_config(sym)
            can_open, _ = RiskValidator.can_open_position(
                sym=sym,
                cfg=cfg,
                prices=prices,
                volumes=volumes,
                changes=changes,
                total_margin=total_margin,
                balance=self.balance,
                positions=self.positions,
                max_total_exposure=MAX_TOTAL_EXPOSURE,
                total_account_equity=total_account_equity
            )
            if not can_open:
                continue

            # 评分 = 成交量 * 资金费率极端度（信号越强分越高）
            vol = volumes.get(sym, 0)
            chg_abs = abs(changes.get(sym, 0))
            fr_strength = abs(funding_rates.get(sym, 0)) * 10000
            score = vol * (1 + chg_abs / 100) * (1 + fr_strength)
            scored.append((sym, score, vol, chg_abs))

        scored.sort(key=lambda x: x[1], reverse=True)

        # 预取AI计划：对前N个币对并行拉取AI信号（开仓前缓存就位）
        # 方向判定已内联（读 RangePlan 区间中点判定），无需外部引擎
        if SHARK_SIGNAL_SOURCE == "ai" and AI_ENABLED:
            prefetch_tasks = []
            for psym, _, _, _ in scored[:35]:
                if not trading_track_allows_open(psym):
                    continue
                if len(prefetch_tasks) >= 15:
                    break
                prefetch_tasks.append(
                    self._fetch_ai_plan(
                        psym,
                        prices[psym],
                        funding_rates.get(psym, 0),
                        changes.get(psym, 0),
                        volumes.get(psym, 0),
                    )
                )
            if prefetch_tasks:
                await asyncio.gather(*prefetch_tasks, return_exceptions=True)

        opened = 0
        _rej = {
            "no_plan": 0,
            "no_lev": 0,
            "no_margin": 0,
            "no_side": 0,
            "entry_block": 0,
            "gate_block": 0,
            "no_cash": 0,
            "cap_full": 0,
        }
        for sym, score, vol, chg_abs in scored:
            # 启动预热中 → 不开新仓
            if not _can_open:
                break
            if not trading_track_allows_open(sym):
                continue
            # 进化引擎：连亏时暂停山寨
            if self._evo_skip_alts and not is_stable(sym):
                continue
            # 总敞口限制（总权益 * 95%）
            if total_margin >= total_account_equity * MAX_TOTAL_EXPOSURE:
                break

            px = prices[sym]
            change = changes.get(sym, 0)

            # 杠杆：完全由 AI 计划决定，无固定值
            lev = 0
            spec = self._contract_specs.get(sym)  # 合约规格（quanto/手续费）

            # 保证金：余额动态比例 × 波动衰减（低波大仓，高波小仓）
            cfg = get_config(sym)

            # ── 行情检测：多因子判定行情类型，每币对独立判断 ──
            _regime = None
            _regime_cfg = {}
            if KLINE_ENABLED:
                try:
                    detector = get_detector()
                    if detector:
                        _regime, _diag = detector.detect(sym)
                        _regime_cfg = REGIME_CONFIG.get(_regime, {})
                        # 乱震/死水 → 不开仓
                        if _regime_cfg.get("allowed_dir") is None:
                            continue
                        # 缓存行情上下文
                        self._regime_cache[sym] = {
                            "regime": _regime.value,
                            "diag": _diag,
                            "cfg": _regime_cfg,
                        }
                except Exception:
                    pass

            # ── 读 AI 计划，提取杠杆（完全由 AI 决定） ──
            plan_cache = None
            if (not is_stable(sym)) and is_high_vol_alt(sym):
                await self._ensure_alt_attack_plan(
                    sym, px, change, vol, funding_rates.get(sym, 0)
                )
            if self._plan_gate:
                plan_cache = self._plan_gate.get_plan(sym)
                if plan_cache:
                    if (not is_stable(sym)) and self._is_alt_dynamic_plan(plan_cache):
                        (
                            plan_cache,
                            refreshed,
                            replan_reason,
                        ) = await self._refresh_alt_plan_if_needed(
                            sym,
                            plan_cache,
                            px,
                            change,
                            vol,
                            funding_rates.get(sym, 0),
                            now,
                        )
                        if refreshed:
                            _log.info(
                                f"[山寨计划] {sym} {replan_reason} → 本地进攻计划已刷新"
                            )
                    else:
                        replan_now, replan_reason = self._should_replan_for_price_drift(
                            sym, plan_cache, px, now
                        )
                        if replan_now:
                            self._request_symbol_replan(sym, replan_reason)
                            continue
                    plan_lev = plan_cache.get("leverage", 0)
                    if plan_lev and 1 <= plan_lev <= 125:
                        lev = self._clamp_leverage_for_config(sym, int(plan_lev), cfg)
            if lev <= 0:
                _rej["no_lev"] += 1
                continue  # AI 未产出有效杠杆，不交易

            plan_stick = bool(
                _plan_authority_enabled()
                and plan_cache
                and not self._is_alt_dynamic_plan(plan_cache)
            )

            margin = self._margin_from_plan(
                plan_cache, cfg, _regime_cfg, chg_abs, strict_plan=plan_stick
            )
            
            # Use the correct total equity base for bucket capital limit, 
            # NOT just remaining balance, to prevent buckets from shrinking each other.
            cap = get_capital_limit(total_account_equity, sym)
            st_bucket = is_stable(sym)
            
            # Check bucket-specific capital limits BEFORE calculating final size
            # (Early rejection saves computation and avoids partial allocations)
            bucket_used = sum(
                p["margin"] for s, p in self.positions.items() if is_stable(s) == st_bucket
            )
            if bucket_used + margin > cap:
                # 提前拦截：如果初步计算的 margin 已经突破了该币种类型的资金池上限，拒绝开仓
                _rej["cap_full"] += 1
                continue
                
            if margin <= 0:
                _rej["no_margin"] += 1
                continue

            entry_price = px  # 默认市价，策略入场在方向确定后调整

            # 策略类型持仓限制
            max_pos = cfg.get("max_positions", 0)
            if max_pos > 0:
                same_type = sum(
                    1 for s in self.positions if is_stable(s) == is_stable(sym)
                )
                if same_type >= max_pos:
                    continue

            # ── 纯数学方向判定（从 RangePlan 区间中点） ──
            side = ""
            signal_src = ""
            ai_confidence = 0
            ai_use = False
            _learner_feat = []
            _stop_mult = 2.0
            _tp_mult = 3.0
            _plan_sl = None  # 计划精确止损价
            _plan_tp = None  # 计划精确止盈价

            # 使用缓存计划（已在杠杆阶段读取）
            if plan_cache:
                side, signal_src, _plan_sl, _plan_tp = self._side_from_plan(
                    plan_cache, px
                )
                ai_confidence = int(plan_cache.get("ai_confidence", 0))
                if ai_confidence > 0:
                    ai_use = True
            if not side:
                _rej["no_side"] += 1
                continue

            # 实际下单为市价；计划层 entry zone 只用于门禁，不由 Python 改写。
            entry_price = px
            if plan_cache and not self._main_coin_entry_allowed(
                sym, plan_cache, side, entry_price
            ):
                continue

            # ── FastLoop 计划门禁：无计划/走廊外/熔断/方向不匹配 → 禁止开仓 ──
            if self._plan_gate:
                can, reason = self._plan_gate.can_open(sym, side, entry_price)
                if not can:
                    _rej["gate_block"] += 1
                    continue

            quanto = spec.quanto_multiplier if spec else 1.0
            fee_rate_maker = (
                abs(spec.maker_fee) if spec and spec.maker_fee < 0 else TAKER_FEE * 0.2
            )

            entry_risk_tag = "标准"
            if plan_cache:
                if plan_stick:
                    margin_mult, lev_cap, entry_risk_tag = 1.0, 125, "计划锁定"
                else:
                    margin_mult, lev_cap, entry_risk_tag = self._entry_risk_adjustment(
                        sym, plan_cache, side, entry_price
                    )
                if margin_mult <= 0 or lev_cap <= 0:
                    _rej["entry_block"] += 1
                    continue
                lev = max(1, min(int(lev), int(lev_cap)))
                lev = self._clamp_leverage_for_config(sym, lev, cfg)
                margin *= margin_mult

                size = (margin * lev) / max(quanto * entry_price, 1e-9)
                bumped_for_min = False
                if spec and size < spec.order_size_min:
                    size = spec.order_size_min
                    margin = (size * quanto * entry_price) / lev
                    bumped_for_min = True
                if bumped_for_min and not is_stable(sym):
                    alt_ceiling = max(5.0, total_account_equity * 0.06)
                    if margin > alt_ceiling:
                        continue
                fee = size * quanto * entry_price * fee_rate_maker
                
                # Check absolute available cash across the entire account
                if margin + fee > self.balance:
                    _rej["no_cash"] += 1
                    continue
                    
                # Check bucket-specific capital limits (Stable vs Volatile)
                # Ensure we use total initial capital for the bucket calculation, not just remaining balance
                bucket_cap = get_capital_limit(total_account_equity, sym)
                
                bucket_used = sum(
                    p["margin"]
                    for s, p in self.positions.items()
                    if is_stable(s) == is_stable(sym)
                )
                
                if bucket_used + margin > bucket_cap:
                    # 如果超出了该类别的资金池上限，则拒绝开仓
                    _rej["cap_full"] += 1
                    continue

            # ── 统一下单通道 → Redis → Go 执行器 ──
            _live_oid = None
            _is_live = self._live and self._live.active
            mode = "live" if (_is_live and self._live_trading_enabled) else "paper"
            if (
                mode == "live"
                and self._live
                and self._live.active
                and not self._live.is_healthy
            ):
                self._note_tick_block(
                    "live_api",
                    "实盘引擎已连续报单错误熔断，本轮跳过开仓",
                    log_every_sec=20.0,
                )
                continue

            _ct_size = max(1, int(size))
            cmd = build_order_command(
                symbol=sym,
                side=side,
                size=_ct_size,
                leverage=lev,
                action="open",
                mode=mode,
                stop_loss=_plan_sl,
                take_profit=_plan_tp,
            )
            try:
                import redis as _redis

                _r = _redis.from_url(
                    os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0")
                )
                _r.publish("shark:orders:new", cmd)
                _live_oid = True
            except Exception as e:
                _log.error("Redis publish failed: %s", e)
                if mode == "live":
                    continue  # 实盘失败必须跳过

            self.positions[sym] = {
                "side": side,
                "entry": entry_price,
                "size": size,
                "leverage": lev,
                "margin": margin,
                "opened": now,
                "fee_open": fee,
                "vol_chg": chg_abs,
                "best_pnl": -999,
                "pyramid_count": 0,
                "ai_targets": None,
                "order_id": uuid.uuid4(),
                "signal_src": signal_src,
                "ai_confidence": ai_confidence if ai_use else 0,
                "_learner_feat": _learner_feat,
                "plan_sl": _plan_sl,  # AI计划精确止损价
                "plan_tp": _plan_tp,  # AI计划精确止盈价
                "plan_signature": self._plan_signature(plan_cache, side)
                if plan_cache
                else None,
                "entry_risk_tag": entry_risk_tag,
                "plan_stick": plan_stick,
            }

            # AI分析（异步，不阻塞开仓）
            if AI_ENABLED and SHARK_SIGNAL_SOURCE == "ai" and not plan_stick:
                asyncio.create_task(
                    self._fetch_ai_plan(
                        sym, px, funding_rates.get(sym, 0), changes.get(sym, 0), vol
                    )
                )

            # 实盘记录
            if self._live and self._live.active and _live_oid:
                self._live.positions[sym] = LivePosition(
                    symbol=sym,
                    side=side,
                    size=int(size),
                    entry_price=entry_price,
                    leverage=lev,
                    margin=margin,
                    order_id=str(uuid.uuid4()),
                    opened_at=now,
                )
            self.trades += 1
            total_margin += margin

            fee_str = f" 手续费={fee:.4f}" if fee > 0.0001 else ""
            stype = "主流" if is_stable(sym) else "山寨"
            msg = f"[开仓-{stype}] {sym} {side.upper()} @ {entry_price:.4f} 保证金={margin:.2f} 杠杆={lev}x 信号={signal_src}{' 行情=' + _regime.value if _regime else ''}"
            if entry_risk_tag != "标准":
                msg += f" 风控={entry_risk_tag}"
            if plan_cache:
                msg += " " + self._plan_price_debug(plan_cache, side, entry_price)
            if _plan_sl:
                msg += f" SL=计划{_plan_sl:.1f}"
            if _tp_mult:
                msg += f" TP=ATR×{_tp_mult}"

            # 所有检查通过，扣费开仓
            if (
                self._live
                and self._live.active
                and self._live_trading_enabled
                and _live_oid
            ):
                # 实盘：余额从交易所同步
                try:
                    self.balance = self._live.get_balance()
                except Exception:
                    self.balance -= margin + fee
            else:
                self.balance -= margin + fee
            self.total_fees += fee
            if self._persistence and self._persistence.enabled_db():
                oid = self.positions[sym]["order_id"]
                self._persistence.on_position_open(
                    self,
                    prices,
                    order_id=oid,
                    sym=sym,
                    side=side,
                    entry_price=entry_price,
                    size=size,
                    margin=margin,
                    lev=float(lev),
                    fee=fee,
                    opened_ts=now,
                )

            self._log.append(msg)
            _log.info(msg)

            # Alpha角色事件：开仓（短台词 + 可选 LLM 暴走润色）
            side_cn = "多" if side == "long" else "空"
            from api.routes import state_lock
            async with state_lock:
                seq = get_state().get("character_event_seq", 0) + 1
                get_state()["character_event_seq"] = seq
            speech0 = pop_line(trade_category_for_open())
            ev_open = {
                "Event_Type": f"开仓_{sym}_{side_cn}",
                "Action_Code": "action_sword_draw"
                if side == "long"
                else "action_hammer_down",
                "Facial_Expression": "confident",
                "Emotion_Index": 35,
                "Speech_Text": speech0,
                "symbol": sym,
                "side": side,
                "_seq": seq,
            }
            async with state_lock:
                get_state()["character_event"] = ev_open
            try: _schedule_loli_speech(ev_open)
            except NameError: pass

            opened += 1

        if opened == 0 and len(scored) > 0:
            _rej_str = " ".join(f"{k}={v}" for k, v in _rej.items() if v > 0)
            if _rej_str:
                _log.info(
                    f"[跳过] {len(scored)}币对 开仓=0 持仓={len(self.positions)} | {_rej_str}"
                )

        self._update_state(prices)


# ═══════════════════════════════════════════════════════════════════════
# 价格推送循环
# ═══════════════════════════════════════════════════════════════════════
