from character.dialogue import pop_line, trade_category_for_close
from execution.order_command import build_order_command
"""Position close logic."""
from api.routes import get_state
from strategy.dual import is_stable, is_high_vol_alt
import time
from api.routes import get_state
import uuid
import json
import os
import logging
from character.voice import _schedule_loli_speech

# character sequence state removed
_log = logging.getLogger(__name__)

class CloseMixin:
    def _close_position(self, sym, px, reason, pnl_pct, prices=None):
        # ── 统一平仓通道：实盘给 executor，纸盘给 matcher，避免撮合流只有 open 没有 close ──
        _live_close_ok = True
        _live_close_px = px
        lp = self.positions.get(sym)
        close_mode = "live" if (self._live and self._live.active and self._live_trading_enabled) else "paper"
        if lp:
            cmd = build_order_command(
                symbol=sym,
                side=lp["side"],
                action="close",
                mode=close_mode,
                size=max(1, int(lp.get("size", 1))),
                leverage=max(1, int(lp.get("leverage", 1))),
            )
            try:
                import redis as _redis
                _r = _redis.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
                _r.publish("shark:orders:new", cmd)
            except Exception as e:
                _log.error("Redis close publish failed: %s", e)
                if close_mode == "live":
                    _live_close_ok = False
                    _log.error("🔥 实盘平仓失败 %s, 人工介入!", sym)

        pos = self.positions.pop(sym)
        oid = pos.get("order_id")
        bal_before = self.balance
        spec = self._contract_specs.get(sym)
        q = self._quanto_for(sym)
        gross = self._gross_pnl_usd(sym, pos, px)
        fee_rate_maker = abs(spec.maker_fee) if spec and spec.maker_fee < 0 else TAKER_FEE * 0.2
        fee_close = pos["size"] * q * px * fee_rate_maker
        fee_open = pos.get("fee_open", 0)
        gross_pnl = gross  # 毛利（不含手续费）
        realized = gross - fee_open - fee_close  # 含全部手续费的净利

        self.total_fees += fee_close
        # 余额更新：实盘从交易所同步，纸盘本地计算
        if _live_close_ok and self._live and self._live.active and self._live_trading_enabled:
            try:
                self.balance = self._live.get_balance()
            except Exception:
                self.balance += pos["margin"] + gross - fee_close
        else:
            self.balance += pos["margin"] + gross - fee_close
        print(
            f"[DEBUG费用] 毛利={gross:.6f} 平仓费={fee_close:.6f} 净利={realized:.6f} "
            f"balance={self.balance:.2f} total_fees={self.total_fees:.4f}"
        )
        self.realized_pnl += realized
        self.gross_realized += gross  # 毛利累计（不含手续费），用于余额展示
        self.closed_trades += 1
        if realized > 0:
            self.wins += 1

        # 更新 static_equity（平仓后 = 已实现的真实权益，剔除浮盈）
        if prices:
            self._recalc_equity(prices)
        self.static_equity = self.equity
        if self.static_equity > self.peak_static_equity:
            self.peak_static_equity = self.static_equity

        # 记录到交易历史
        closed_ts = time.time()
        self._trade_history.append({
            "symbol": sym, "side": pos["side"],
            "entry_price": pos["entry"], "exit_price": px,
            "size": pos["size"], "leverage": pos["leverage"],
            "margin": pos["margin"], "realized_pnl": realized,
            "pnl_pct": pnl_pct, "reason": reason,
            "fee_open": pos.get("fee_open", 0),
            "fee_close": fee_close,
            "gross_pnl": gross,
            "opened_at": pos["opened"], "closed_at": closed_ts,
            "signal_src": pos.get("signal_src", ""),
            "ai_confidence": pos.get("ai_confidence", 0),
            "exit_type": "tp" if realized > 0 else ("sl" if pnl_pct < -0.01 else "timeout"),
        })
        # ── 山寨币独立进化：每笔平仓追踪该币对质量 ──
        if not is_stable(sym) and is_high_vol_alt(sym):
            evo = self._alt_evo.get(sym)
            if evo:
                if realized > 0:
                    evo["wins"] += 1
                elif pnl_pct < -0.01:
                    evo["stops"] += 1
                if evo["plans"] >= 6 and evo["plans"] % 6 == 0:
                    total = max(1, evo["wins"] + evo["stops"])
                    wr = evo["wins"] / max(1, total)
                    sr = evo["stops"] / max(1, total)
                    if sr > 0.5:
                        evo["stop_mult"] = min(1.4, evo["stop_mult"] + 0.15)
                        evo["atr_mult"] = max(0.6, evo["atr_mult"] - 0.1)
                    if wr < 0.35:
                        evo["atr_mult"] = max(0.5, evo["atr_mult"] - 0.1)
                    if wr > 0.6:
                        evo["tp_mult"] = min(1.5, evo["tp_mult"] + 0.1)
                    evo["gen"] += 1
                    print(f"[山寨进化] {sym} gen={evo['gen']} trades={total} wr={wr:.1%} sr={sr:.1%} "
                          f"atr×{evo['atr_mult']:.2f} stop×{evo['stop_mult']:.2f} tp×{evo['tp_mult']:.2f}")
        # 发布到 Redis 供 Go 进化引擎消费
        try:
            import redis as _redis2
            _rr = _redis2.from_url(os.environ.get("SHARK_REDIS_URL", "redis://redis:6379/0"))
            _rr.lpush("shark:trade_history", json.dumps(self._trade_history[-1]))
            _rr.ltrim("shark:trade_history", 0, 199)
        except Exception:
            pass
        if self._persistence and self._persistence.enabled_db() and oid:
            ou = oid if isinstance(oid, uuid.UUID) else uuid.UUID(str(oid))
            self._persistence.on_position_close(
                self,
                prices,
                order_id=ou,
                trade_id=uuid.uuid4(),
                sym=sym,
                side=pos["side"],
                entry_price=float(pos["entry"]),
                exit_price=float(px),
                size=float(pos["size"]),
                leverage=float(pos["leverage"]),
                margin=float(pos["margin"]),
                gross_pnl=gross_pnl,
                fee_open=float(fee_open),
                fee_close=float(fee_close),
                realized=float(realized),
                pnl_pct=float(pnl_pct),
                reason=reason,
                opened_ts=float(pos["opened"]),
                closed_ts=float(closed_ts),
                free_cash_before_release=bal_before,
            )

        msg = (
            f"[平仓] {sym} {reason} 盈亏={realized:+.4f} ({pnl_pct:+.1f}%) "
            f"余额={self.balance:.2f} static_equity={self.static_equity:.2f} 累计手续费={self.total_fees:.4f}"
        )
        self._log.append(msg)
        print(msg)

        # ── 止损反思：多维分析亏损原因 → 立即调整下笔交易参数 ──
        if hasattr(self, "_reflector") and self._reflector:
            try:
                # 兼容旧版 Reflector，如果它没有 record_trade 方法，就调用 analyze
                if hasattr(self._reflector, "record_trade"):
                    self._reflector.record_trade(sym, "profit" if pnl_pct > 0 else "loss", pnl_pct)
                elif realized < 0:
                    local_tags = self._reflector.analyze(sym, pos, realized, pnl_pct, reason, px,
                                            self._regime_cache, None)
                    adj = self._reflector.maybe_adjust()
                    if adj:
                        print(f"[反思统计] {adj}")
                print(f"[反思记录] {sym} {'盈利' if pnl_pct > 0 else '亏损'} pnl={pnl_pct:.2f}%")
            except Exception as e:
                import traceback
                self._log.append(f"[Reflector Error] {e}\n{traceback.format_exc()}")

        # ── 在线学习：Q-Learning + ES更新 ──
        if self._learner:
            feat = pos.get("_learner_feat")
            if feat:
                # 构建下一状态特征（平仓时）
                try:
                    diag_now = self._regime_cache.get(sym, {}).get("diag", {})
                    ai_cache = self._ai_signal_cache.get(sym, {})
                    funding = 0  # 当前tick的费率
                    # 简化：用开仓时的特征变换作为下一状态
                    next_feat = feat[:]  # 简化处理
                    held = pos.get("closed_at", time.time()) - pos.get("opened_at", time.time())
                    was_stop = "止损" in reason
                    rc = self._regime_cache.get(sym, {})
                    regime_val = rc.get("regime", "unknown")
                    self._learner.on_trade_closed(
                        feat, next_feat, realized, pnl_pct,
                        was_stop, held, regime_val
                    )
                except Exception:
                    pass

        # Alpha角色事件：平仓
        is_tp = "止盈" in reason
        is_big_win = realized > 1.0
        pnl_abs = abs(realized)
        seq = get_state().get("character_event_seq", 0) + 1
        get_state()["character_event_seq"] = seq
        if is_tp:
            speech0 = pop_line("profit")
        else:
            speech0 = pop_line(trade_category_for_close(reason, realized))
        ev_close = {
            "Event_Type": f"{'止盈' if is_tp else '止损'}_{sym}",
            "Action_Code": (
                ("action_catch_coin" if is_big_win else "action_fist_pump")
                if is_tp
                else ("action_adjust_glasses" if pnl_abs < 0.5 else "action_shield_up")
            ),
            "Facial_Expression": "excited" if is_tp and is_big_win else ("relaxed" if is_tp else "serious"),
            "Emotion_Index": 20 if is_tp else 65,
            "Speech_Text": speech0,
            "Evolution_Log": (
                f"止盈记录: {sym} +{realized:.4f} ({pnl_pct:+.1f}%)"
                if is_tp
                else f"止损分析: {sym} {realized:.4f} ({pnl_pct:+.1f}%) → 因子权重微调中"
            ),
            "symbol": sym,
            "side": pos["side"],
            "pnl": realized,
            "pnl_pct": pnl_pct,
            "_seq": seq,
        }
        get_state()["character_event"] = ev_close
        _schedule_loli_speech(ev_close)

        self._apply_stop_loss_fuse(sym, reason, pos)
