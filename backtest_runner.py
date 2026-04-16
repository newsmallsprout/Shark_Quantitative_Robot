#!/usr/bin/env python3
"""
独立回测与数据注入：30 天 1m K 线、逐仓、双向微利 + 强平、费用与 paper_engine 一致。

**成交频率说明**：默认每根 1m 仅沿 **OHLC 折线**（外加可选 H↔L 摆动）推进，没有逐笔/tick；
同一策略在实盘若依赖连续报价，回测平仓次数通常会 **少几个数量级**，属模型差异而非单一 bug。

用法（项目根目录）:
  python backtest_runner.py --symbol DOGE/USDT --days 30
  python backtest_runner.py --symbol DOGE/USDT --days 7 --intra-bar-whipsaw-pairs 20  # 压力近似盘中震荡

若 macOS 报 SSL: CERTIFICATE_VERIFY_FAILED：先 ``pip install certifi``；仍失败可加 ``--insecure``（仅本机）。

输出:
  data/backtest/backtest_history_positions.json  — darwin.trade_autopsy.v2 数组（可拷入 autopsy 目录供面板）
  data/backtest/backtest_summary.txt            — 战报摘要
"""
from __future__ import annotations

import argparse
import json
import math
import os
import ssl
import sys
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple, cast

# 项目根
ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.core.paper_engine import TAKER_FEE_RATE  # 必须与实盘纸面一致
from src.darwin.autopsy import build_trade_autopsy

try:
    from tqdm import tqdm
except ImportError:
    tqdm = None  # type: ignore

GATE_REST = "https://api.gateio.ws/api/v4"
INITIAL_CASH_USDT = 1000.0
DEFAULT_MARGIN_PER_LEG = 5.0
DEFAULT_LEVERAGE = 75
LIQ_MARGIN_FRAC = 0.85
# 微利：圆桌净利 = Gross − OpenFee − CloseFee；开火需净利 > max(地板, 0.5×(Open+Close))
MICRO_DYNAMIC_FLOOR_USDT = 0.15
MICRO_FEE_SURPLUS_FRAC = 0.5
MAX_MICRO_RELOADS_PER_BAR_PER_LEG = 2
MAX_MICRO_ITERS_PER_VISIT = 12
# Gate：单次最多 2000 根；用 limit+to 翻页时，返回的最早一根不得早于 server_now−10000×60s，否则 400
# 即：to − limit×60 ≥ server_now − 10000×60 → limit ≤ (to − floor) / 60，其中 floor = server_now − 600000
MAX_CANDLES_CAP = 2000
GATE_RECENT_1M_POINTS = 10000
BAR_SECONDS = 60

# 沿 (p0→p1) 搜索首个微利触发：采样过稀会漏穿；默认 512。
DEFAULT_MICRO_FIRST_HIT_SAMPLES = 512

# HTTPS 上下文：在 main 里 init_http_ssl()；避免 macOS 自带 Python 未装证书链导致 SSL: CERTIFICATE_VERIFY_FAILED
_SSL_CTX: Optional[ssl.SSLContext] = None


def init_http_ssl(*, insecure: bool) -> None:
    global _SSL_CTX
    if insecure:
        _SSL_CTX = ssl._create_unverified_context()
        return
    try:
        import certifi

        _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        _SSL_CTX = ssl.create_default_context()


def _ssl_for_urlopen() -> ssl.SSLContext:
    if _SSL_CTX is not None:
        return _SSL_CTX
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def _contract_id(symbol: str) -> str:
    return str(symbol).replace("/", "_").replace(":", "_")


def fee_usdt(abs_contracts: float, contract_size: float, price: float) -> float:
    """Fee = |张| * contract_size * price * TAKER（与 paper_engine 一致）。"""
    return abs(float(abs_contracts)) * float(contract_size) * float(price) * float(TAKER_FEE_RATE)


def fetch_json(url: str) -> Any:
    import urllib.error
    import urllib.request

    req = urllib.request.Request(url, headers={"User-Agent": "SharkBacktest/1.0"})
    ctx = _ssl_for_urlopen()
    try:
        with urllib.request.urlopen(req, timeout=120, context=ctx) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except ssl.SSLError as e:
        raise SystemExit(
            "HTTPS 证书校验失败（常见于 macOS 未跑 Install Certificates.command）。\n"
            "  任选其一：\n"
            "  1) pip install certifi 后重试（脚本会优先用 certifi 的 CA 包）；\n"
            "  2) python3 backtest_runner.py ... --insecure（仅本机回测，勿用于生产）；\n"
            "  3) 运行 macOS 自带的 Install Certificates.command。\n"
            f"  原始错误: {e}"
        ) from e
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            body = ""
        raise SystemExit(f"HTTP {e.code} {e.reason}: {url}\n{body}") from e
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" in str(e) or "SSL" in str(e).upper():
            raise SystemExit(
                "SSL 握手/证书失败。请 pip install certifi 或使用 --insecure。\n" f"详情: {e}"
            ) from e
        raise


def fetch_quanto(symbol: str) -> float:
    url = f"{GATE_REST}/futures/usdt/contracts/{_contract_id(symbol)}"
    try:
        data = fetch_json(url)
        qm = float(data.get("quanto_multiplier") or 0.0)
        return qm if qm > 0 else 1.0
    except Exception:
        return 1.0


def fetch_gate_server_time_sec() -> int:
    """与 Gate 对齐的当前时间（秒），避免本机时钟漂移导致 from/to 被拒。"""
    data = fetch_json(f"{GATE_REST}/spot/time")
    ms = int(data.get("server_time", 0) or 0)
    if ms > 10_000_000_000:
        return ms // 1000
    return ms


def _parse_candle_chunk(chunk: Any) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    if not isinstance(chunk, list):
        return rows
    for row in chunk:
        if isinstance(row, dict):
            t = int(row.get("t", 0) or 0)
            o = float(row.get("o", 0) or 0)
            h = float(row.get("h", 0) or 0)
            l = float(row.get("l", 0) or 0)
            c = float(row.get("c", 0) or 0)
        elif isinstance(row, (list, tuple)) and len(row) >= 5:
            t = int(row[0])
            o, h, l, c = float(row[1]), float(row[2]), float(row[3]), float(row[4])
        else:
            continue
        if t <= 0 or h <= 0 or l <= 0:
            continue
        rows.append({"t": t, "o": o, "h": h, "l": l, "c": c})
    rows.sort(key=lambda x: x["t"])
    return rows


def _dedupe_consecutive_prices(pts: List[float], *, eps: float = 1e-14) -> List[float]:
    out: List[float] = []
    for x in pts:
        if not out or abs(float(x) - float(out[-1])) > eps:
            out.append(float(x))
    return out


def price_path_for_bar(o: float, h: float, l: float, c: float, whipsaw_hl_pairs: int) -> List[float]:
    """
    默认与历史行为一致：单根 1m 仅走 OHLC 折线（3 段，4 个顶点）。

    whipsaw_hl_pairs>0 时，在到达收盘价前于本根 high/low 之间追加若干次完整往返，
    用于近似「盘中在区间内抖动」对微利触发的次数压力（非真实逐笔还原）。
    """
    o, h, l, c = float(o), float(h), float(l), float(c)
    hi, lo = max(h, l), min(h, l)
    w = max(0, int(whipsaw_hl_pairs))
    if h - o >= o - l:
        seq: List[float] = [o, h, l]
        cur = seq[-1]
        for _ in range(w):
            if abs(hi - cur) > 1e-18:
                seq.append(hi)
                cur = hi
            if abs(lo - cur) > 1e-18:
                seq.append(lo)
                cur = lo
        seq.append(c)
        return _dedupe_consecutive_prices(seq)
    seq = [o, l, h]
    cur = seq[-1]
    for _ in range(w):
        if abs(lo - cur) > 1e-18:
            seq.append(lo)
            cur = lo
        if abs(hi - cur) > 1e-18:
            seq.append(hi)
            cur = hi
    seq.append(c)
    return _dedupe_consecutive_prices(seq)


def _max_limit_for_to(server_now: int, to_ts: int) -> int:
    """满足 Gate「最近 10000 根 1m」窗口：最早一根 >= server_now − 10000×60。"""
    floor_ts = int(server_now) - GATE_RECENT_1M_POINTS * BAR_SECONDS
    span = int(to_ts) - floor_ts
    if span < BAR_SECONDS:
        return 0
    return min(MAX_CANDLES_CAP, max(1, span // BAR_SECONDS))


def fetch_candles_1m(symbol: str, days: int) -> List[Dict[str, Any]]:
    """
    Gate v4 公开 1m：用交易所 server_time；每页 limit+to，limit 随 to 变深自动缩小，
    避免「Candlestick too long ago. Maximum 10000 points recently」。
    """
    contract = _contract_id(symbol)
    server_now = fetch_gate_server_time_sec()
    start_target = int(server_now) - int(days * 86400)
    floor_ts = int(server_now) - GATE_RECENT_1M_POINTS * BAR_SECONDS
    to_ts = int(server_now)
    by_t: Dict[int, Dict[str, Any]] = {}
    prev_oldest: Optional[int] = None

    for _ in range(2000):
        lim = _max_limit_for_to(server_now, to_ts)
        if lim <= 0:
            break
        url = (
            f"{GATE_REST}/futures/usdt/candlesticks?"
            f"contract={contract}&interval=1m&limit={lim}&to={to_ts}"
        )
        chunk = fetch_json(url)
        rows = _parse_candle_chunk(chunk)
        if not rows:
            break
        oldest = int(rows[0]["t"])
        if prev_oldest is not None and oldest >= prev_oldest:
            break
        prev_oldest = oldest
        for r in rows:
            if r["t"] >= start_target:
                by_t[r["t"]] = r
        if oldest <= start_target:
            break
        to_ts = oldest - BAR_SECONDS
        if to_ts <= floor_ts:
            break

    merged = [by_t[k] for k in sorted(by_t.keys())]
    return [r for r in merged if r["t"] < server_now]


@dataclass
class Leg:
    side: str  # "long" | "short"
    contracts: float  # >0 张数
    entry_price: float
    margin_usdt: float
    opened_bar: int
    opened_ts: float
    max_fav: float = 0.0
    max_adv: float = 0.0

    def base_qty(self, cs: float) -> float:
        return float(self.contracts) * float(cs)

    def unrealized(self, cs: float, px: float) -> float:
        b = self.base_qty(cs)
        if self.side == "long":
            return b * (px - self.entry_price)
        return b * (self.entry_price - px)

    def gross_close_at(self, cs: float, px: float) -> float:
        """平仓毛盈亏（不含本次平仓费）。"""
        b = self.base_qty(cs)
        if self.side == "long":
            return b * (px - self.entry_price)
        return b * (self.entry_price - px)


@dataclass
class BacktestState:
    symbol: str
    contract_size: float
    margin_per_leg: float
    leverage: float
    micro_first_hit_samples: int = DEFAULT_MICRO_FIRST_HIT_SAMPLES
    intra_bar_whipsaw_hl_pairs: int = 0
    cash: float = INITIAL_CASH_USDT
    long_leg: Optional[Leg] = None
    short_leg: Optional[Leg] = None
    autopsies: List[Dict[str, Any]] = field(default_factory=list)
    total_fees: float = 0.0
    close_count: int = 0
    liq_count: int = 0
    micro_count: int = 0
    equity_peak: float = INITIAL_CASH_USDT
    max_drawdown: float = 0.0
    seq: int = 0
    _micro_long_bar_cycles: int = 0
    _micro_short_bar_cycles: int = 0
    _mic_wall_minute: int = -1
    autopsy_strategy_name: str = "backtest_runner"

    def sync_micro_minute(self, ts: float) -> None:
        """与实盘 BetaNeutralHF 一致：每腿微利+续杯次数按墙钟分钟重置。"""
        m = int(float(ts) // 60)
        if m != self._mic_wall_minute:
            self._mic_wall_minute = m
            self._micro_long_bar_cycles = 0
            self._micro_short_bar_cycles = 0

    def notional_per_leg(self) -> float:
        return float(self.margin_per_leg) * float(self.leverage)

    def contracts_for_open(self, px: float) -> float:
        n = self.notional_per_leg()
        denom = float(self.contract_size) * float(px)
        if denom <= 0:
            return 0.0
        return n / denom

    def equity(self, mark: float) -> float:
        e = float(self.cash)
        for leg in (self.long_leg, self.short_leg):
            if leg is None:
                continue
            e += float(leg.margin_usdt) + leg.unrealized(self.contract_size, mark)
        return e

    def update_dd(self, mark: float) -> None:
        eq = self.equity(mark)
        if eq > self.equity_peak:
            self.equity_peak = eq
        dd = (self.equity_peak - eq) / self.equity_peak if self.equity_peak > 0 else 0.0
        self.max_drawdown = max(self.max_drawdown, dd)

    def liq_price_long(self, leg: Leg) -> float:
        """亏损达 margin*LIQ_FRAC 时的标记价（多仓价格下跌）。"""
        a = leg.base_qty(self.contract_size)
        if a <= 0:
            return -1e9
        return float(leg.entry_price) - (LIQ_MARGIN_FRAC * float(leg.margin_usdt)) / a

    def liq_price_short(self, leg: Leg) -> float:
        """空仓亏损达阈值时空头标记价上移。"""
        a = leg.base_qty(self.contract_size)
        if a <= 0:
            return 1e9
        return float(leg.entry_price) + (LIQ_MARGIN_FRAC * float(leg.margin_usdt)) / a

    def _micro_roundtrip_net(self, leg: Leg, px: float) -> float:
        cs = self.contract_size
        g = leg.gross_close_at(cs, px)
        fo = fee_usdt(leg.contracts, cs, leg.entry_price)
        fc = fee_usdt(leg.contracts, cs, px)
        return float(g - fo - fc)

    def _micro_roundtrip_target(self, leg: Leg, px: float) -> float:
        cs = self.contract_size
        fo = fee_usdt(leg.contracts, cs, leg.entry_price)
        fc = fee_usdt(leg.contracts, cs, px)
        return max(MICRO_DYNAMIC_FLOOR_USDT, MICRO_FEE_SURPLUS_FRAC * (fo + fc))

    def _micro_fire_ok(self, leg: Leg, px: float, side: str) -> bool:
        if side == "long" and self._micro_long_bar_cycles >= MAX_MICRO_RELOADS_PER_BAR_PER_LEG:
            return False
        if side == "short" and self._micro_short_bar_cycles >= MAX_MICRO_RELOADS_PER_BAR_PER_LEG:
            return False
        return self._micro_roundtrip_net(leg, px) > self._micro_roundtrip_target(leg, px) + 1e-12

    def _cross_on_segment(self, p0: float, p1: float, level: float, eps: float = 1e-12) -> bool:
        lo, hi = (p0, p1) if p0 <= p1 else (p1, p0)
        return lo - eps <= level <= hi + eps

    def first_liq_hit(
        self, p0: float, p1: float
    ) -> Tuple[Optional[float], Optional[str]]:
        """沿 p0→p1 方向第一个触发的强平价 (price, 'long'|'short')。"""
        best_t: Optional[float] = None
        best_side: Optional[str] = None
        dp = p1 - p0
        if abs(dp) < 1e-18:
            return None, None

        def param_on_segment(px: float) -> float:
            return (px - p0) / dp

        if self.long_leg:
            lx = self.liq_price_long(self.long_leg)
            if self._cross_on_segment(p0, p1, lx):
                t = param_on_segment(lx)
                if 0.0 <= t <= 1.0 and (best_t is None or t < best_t - 1e-15):
                    best_t, best_side = t, "long"
        if self.short_leg:
            sx = self.liq_price_short(self.short_leg)
            if self._cross_on_segment(p0, p1, sx):
                t = param_on_segment(sx)
                if 0.0 <= t <= 1.0 and (best_t is None or t < best_t - 1e-15):
                    best_t, best_side = t, "short"
        if best_t is None:
            return None, None
        return p0 + best_t * dp, best_side

    def first_micro_hit(self, p0: float, p1: float) -> Tuple[Optional[float], Optional[str]]:
        """沿 p0→p1 扫描首个圆桌微利满足点（动态阈值 + 每腿每墙钟分钟次数上限）。"""
        dp = p1 - p0
        if abs(dp) < 1e-18:
            return None, None
        best_t: Optional[float] = None
        best_side: Optional[str] = None
        n = max(8, int(self.micro_first_hit_samples))
        for i in range(1, n + 1):
            t = i / float(n)
            px = p0 + t * dp
            if dp > 0 and self.long_leg and self._micro_fire_ok(self.long_leg, px, "long"):
                if best_t is None or t < best_t - 1e-15:
                    best_t, best_side = t, "long"
            if dp < 0 and self.short_leg and self._micro_fire_ok(self.short_leg, px, "short"):
                if best_t is None or t < best_t - 1e-15:
                    best_t, best_side = t, "short"
        if best_t is None or best_side is None:
            return None, None
        return p0 + best_t * dp, best_side

    def record_autopsy(
        self,
        *,
        leg: Leg,
        exit_px: float,
        exit_reason: str,
        realized_gross: float,
        fees_on_trade: float,
        closed_at: float,
    ) -> None:
        ec = {
            "strategy": self.autopsy_strategy_name,
            "strategy_name": self.autopsy_strategy_name,
            "backtest": True,
            "opened_bar": leg.opened_bar,
        }
        doc = build_trade_autopsy(
            symbol=self.symbol,
            side="buy" if leg.side == "long" else "sell",
            entry_price=float(leg.entry_price),
            exit_price=float(exit_px),
            closed_size=float(leg.contracts),
            contract_size=float(self.contract_size),
            leverage=float(self.leverage),
            margin_mode="isolated",
            realized_pnl_gross=float(realized_gross),
            fees_on_trade=float(fees_on_trade),
            entry_context=ec,
            max_favorable_unrealized=float(leg.max_fav),
            max_adverse_unrealized=float(leg.max_adv),
            opened_at=float(leg.opened_ts),
            exit_reason=str(exit_reason),
            trading_mode_at_exit="BACKTEST",
        )
        doc["closed_at"] = float(closed_at)
        doc["duration_sec"] = round(max(0.0, float(closed_at) - float(leg.opened_ts)), 3)
        self.autopsies.append(doc)

    def close_leg(
        self,
        leg: Leg,
        exit_px: float,
        exit_reason: str,
        closed_at: float,
    ) -> None:
        cs = self.contract_size
        gross = leg.gross_close_at(cs, exit_px)
        fee_open = fee_usdt(leg.contracts, cs, leg.entry_price)
        fee_close = fee_usdt(leg.contracts, cs, exit_px)
        fees_total = fee_open + fee_close
        net = gross - fees_total
        # 开仓时已扣 margin + fee_open；平仓退回保证金 + 毛盈亏 - 仅平仓费
        self.cash += float(leg.margin_usdt) + gross - fee_close
        self.total_fees += fee_close
        self.close_count += 1
        if "强平" in exit_reason or "liquidation" in exit_reason.lower():
            self.liq_count += 1
        if "微利" in exit_reason or "micro" in exit_reason.lower():
            self.micro_count += 1
        self.record_autopsy(
            leg=leg,
            exit_px=exit_px,
            exit_reason=exit_reason,
            realized_gross=gross,
            fees_on_trade=fees_total,
            closed_at=closed_at,
        )

    def liquidate_at(self, leg: Leg, px: float, closed_at: float) -> None:
        self.close_leg(leg, px, "逐仓强平(isolated_liquidation)", closed_at)

    def micro_take_at(self, leg: Leg, px: float, closed_at: float) -> None:
        self.close_leg(leg, px, "微利收割(micro_take_reload)", closed_at)

    def open_leg(self, side: str, px: float, bar_idx: int, ts: float) -> Optional[Leg]:
        q = self.contracts_for_open(px)
        if q <= 0:
            return None
        fee = fee_usdt(q, self.contract_size, px)
        need = float(self.margin_per_leg) + fee
        if self.cash < need - 1e-9:
            return None
        self.cash -= need
        self.total_fees += fee
        return Leg(
            side=side,
            contracts=q,
            entry_price=float(px),
            margin_usdt=float(self.margin_per_leg),
            opened_bar=bar_idx,
            opened_ts=float(ts),
        )

    def try_micro_at_price(self, px: float, ts: float, bar_idx: int) -> bool:
        """圆桌微利 + 每腿每墙钟分钟最多 MAX_MICRO_RELOADS_PER_BAR_PER_LEG 次收割续杯。"""
        changed = False
        for _ in range(MAX_MICRO_ITERS_PER_VISIT):
            acted = False
            if self.long_leg and self._micro_long_bar_cycles < MAX_MICRO_RELOADS_PER_BAR_PER_LEG:
                if self._micro_fire_ok(self.long_leg, px, "long"):
                    leg = self.long_leg
                    self.long_leg = None
                    self.micro_take_at(leg, px, ts)
                    self._micro_long_bar_cycles += 1
                    nl = self.open_leg("long", px, bar_idx, ts + 1e-6)
                    self.long_leg = nl
                    changed = True
                    acted = True
                    if nl is None:
                        break
            if self.short_leg and self._micro_short_bar_cycles < MAX_MICRO_RELOADS_PER_BAR_PER_LEG:
                if self._micro_fire_ok(self.short_leg, px, "short"):
                    leg = self.short_leg
                    self.short_leg = None
                    self.micro_take_at(leg, px, ts + 2e-6)
                    self._micro_short_bar_cycles += 1
                    ns = self.open_leg("short", px, bar_idx, ts + 3e-6)
                    self.short_leg = ns
                    changed = True
                    acted = True
                    if ns is None:
                        break
            if not acted:
                break
        return changed

    def try_liquidation_at_price(self, px: float, ts: float) -> bool:
        acted = False
        if self.long_leg:
            u = self.long_leg.unrealized(self.contract_size, px)
            if u <= -LIQ_MARGIN_FRAC * self.long_leg.margin_usdt + 1e-12:
                leg = self.long_leg
                self.long_leg = None
                self.liquidate_at(leg, px, ts)
                acted = True
        if self.short_leg:
            u = self.short_leg.unrealized(self.contract_size, px)
            if u <= -LIQ_MARGIN_FRAC * self.short_leg.margin_usdt + 1e-12:
                leg = self.short_leg
                self.short_leg = None
                self.liquidate_at(leg, px, ts)
                acted = True
        return acted

    def update_path_stats(self, px: float) -> None:
        for leg in (self.long_leg, self.short_leg):
            if leg is None:
                continue
            u = leg.unrealized(self.contract_size, px)
            leg.max_fav = max(leg.max_fav, u)
            leg.max_adv = min(leg.max_adv, u)

    def sweep_segment(self, p0: float, p1: float, bar_idx: int, base_ts: float) -> None:
        """沿 p0→p1 依次处理：路径上最近的强平 > 微利 > 再到终点。"""
        cur = float(p0)
        target = float(p1)
        for _ in range(4096):
            if abs(cur - target) < 1e-14:
                break
            self.update_path_stats(cur)
            self.update_dd(cur)

            lp, ls = self.first_liq_hit(cur, target)
            mp, ms = self.first_micro_hit(cur, target)

            use_micro = mp is not None and ms is not None
            use_liq = lp is not None and ls is not None
            if use_liq and use_micro:
                dl = abs(lp - cur)
                dm = abs(mp - cur)
                if dl < dm - 1e-18:
                    use_micro = False
                elif dm < dl - 1e-18:
                    use_liq = False
                else:
                    use_micro = False

            if use_liq and lp is not None and ls is not None:
                frac = abs(lp - cur) / (abs(target - cur) + 1e-18)
                ts_evt = base_ts + min(0.99, frac * 0.95)
                if ls == "long" and self.long_leg:
                    leg = self.long_leg
                    self.long_leg = None
                    self.liquidate_at(leg, lp, ts_evt)
                elif ls == "short" and self.short_leg:
                    leg = self.short_leg
                    self.short_leg = None
                    self.liquidate_at(leg, lp, ts_evt)
                cur = lp
                continue

            if use_micro and mp is not None and ms is not None:
                frac = abs(mp - cur) / (abs(target - cur) + 1e-18)
                ts_evt = base_ts + min(0.99, frac * 0.85)
                cur = mp
                self.try_micro_at_price(mp, ts_evt + 0.02, bar_idx)
                continue

            self.update_path_stats(target)
            self.update_dd(target)
            self.try_liquidation_at_price(target, base_ts + 0.99)
            self.try_micro_at_price(target, base_ts + 1.0, bar_idx)
            cur = target
            break

    def run_bar(self, bar: Dict[str, Any], bar_idx: int) -> None:
        base_ts = float(bar["t"])
        self.sync_micro_minute(base_ts)
        o, h, l, c = float(bar["o"]), float(bar["h"]), float(bar["l"]), float(bar["c"])
        path = price_path_for_bar(o, h, l, c, self.intra_bar_whipsaw_hl_pairs)
        for i in range(len(path) - 1):
            self.sweep_segment(path[i], path[i + 1], bar_idx, base_ts + i * 0.01)

    def initial_dual_open(self, open_px: float, bar_idx: int, ts: float) -> None:
        self.long_leg = self.open_leg("long", open_px, bar_idx, ts)
        self.short_leg = self.open_leg("short", open_px, bar_idx, ts + 1e-6)

    def force_flat(self, px: float, ts: float) -> None:
        if self.long_leg:
            leg = self.long_leg
            self.long_leg = None
            self.close_leg(leg, px, "回测结束强平仓位(eod_flat)", ts)
        if self.short_leg:
            leg = self.short_leg
            self.short_leg = None
            self.close_leg(leg, px, "回测结束强平仓位(eod_flat)", ts)


def single_backtest_job(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    多进程批量回测子进程入口；定义在本模块便于 spawn 模式下可 pickle、且不会重复执行 __main__。
    """
    sym = str(payload.get("symbol", ""))
    kwargs: Dict[str, Any] = dict(payload.get("kwargs") or {})
    t0 = time.time()
    try:
        result = run_backtest(**kwargs)
        result = dict(result)
        result["elapsed_sec"] = round(time.time() - t0, 3)
        result["error"] = None
        return result
    except (SystemExit, KeyboardInterrupt):
        raise
    except BaseException as e:
        err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
        return {
            "symbol": sym,
            "error": err,
            "elapsed_sec": round(time.time() - t0, 3),
        }


def run_backtest(
    *,
    symbol: str,
    days: int,
    margin_per_leg: float,
    leverage: float,
    out_dir: str,
    insecure_ssl: bool,
    micro_first_hit_samples: int = DEFAULT_MICRO_FIRST_HIT_SAMPLES,
    intra_bar_whipsaw_hl_pairs: int = 0,
) -> Dict[str, Any]:
    init_http_ssl(insecure=bool(insecure_ssl))
    os.makedirs(out_dir, exist_ok=True)
    print(f"[BT] 拉取合约乘数 {symbol} …")
    cs = fetch_quanto(symbol)
    print(f"[BT] quanto_multiplier={cs}")
    print(f"[BT] 拉取近 {days} 天 1m K（Gate server_time 对齐）…")
    bars = fetch_candles_1m(symbol, days)
    need = int(days) * 1440
    if len(bars) < need:
        approx_days = GATE_RECENT_1M_POINTS / 1440.0
        print(
            f"[BT] 说明: Gate 公开接口单合约约仅保留最近 {GATE_RECENT_1M_POINTS} 根 1m（≈{approx_days:.2f} 天）。"
            f"本次实际 {len(bars)} 根；若需满 {days} 天请改用历史行情下载或自建库。"
        )
    if len(bars) < 10:
        raise SystemExit(f"K 线过少: {len(bars)}，请检查网络或 contract 名称")
    print(f"[BT] 共 {len(bars)} 根 1m K")
    mfs = max(8, int(micro_first_hit_samples))
    wh = max(0, int(intra_bar_whipsaw_hl_pairs))
    print(
        f"[BT] 路径模型: 每根 K 为 OHLC 折线 + 可选 H↔L 摆动(当前 whipsaw_hl_pairs={wh})；"
        f"first_micro_hit 采样={mfs}。"
        " 默认无逐笔/tick，微利触发次数通常远小于实盘连续报价；若要压力近似可提高 "
        "`--intra-bar-whipsaw-pairs` 或接入成交明细。"
    )

    st = BacktestState(
        symbol=symbol,
        contract_size=cs,
        margin_per_leg=float(margin_per_leg),
        leverage=float(leverage),
        micro_first_hit_samples=mfs,
        intra_bar_whipsaw_hl_pairs=wh,
    )
    first = bars[0]
    st.initial_dual_open(float(first["o"]), 0, float(first["t"]))
    if st.long_leg is None or st.short_leg is None:
        raise SystemExit("初始双开失败：现金不足或合约张数为 0")

    iterator: Any = bars
    if tqdm is not None:
        iterator = tqdm(bars, desc="Backtest", unit="1m")

    last_print = time.time()
    bars_per_day = 1440
    for i, bar in enumerate(iterator):
        st.run_bar(bar, i)
        if tqdm is None and (i % bars_per_day == 0 or i == len(bars) - 1):
            now = time.time()
            if now - last_print >= 2.0 or i == len(bars) - 1:
                last_print = now
                pct = 100.0 * (i + 1) / max(len(bars), 1)
                dt = time.strftime("%Y-%m-%d", time.gmtime(int(bar["t"])))
                eq = st.equity(float(bar["c"]))
                print(
                    f"[进度 {pct:.1f}% | 日期: {dt} | 余额: {st.cash:.2f} U | "
                    f"累计成交: {st.close_count} 笔 | 总手续费: {-st.total_fees:.2f} U | equity≈{eq:.2f}]"
                )

    last_px = float(bars[-1]["c"])
    st.force_flat(last_px, float(bars[-1]["t"]) + 120.0)

    final_eq = st.equity(last_px)
    wins_net = sum(
        1 for a in st.autopsies if float((a.get("pnl") or {}).get("realized_net", 0) or 0) > 0
    )
    wins_gross = sum(
        1 for a in st.autopsies if float((a.get("pnl") or {}).get("realized_gross", 0) or 0) > 0
    )
    net_profit = final_eq - INITIAL_CASH_USDT

    summary_lines = [
        f"symbol={symbol}",
        f"days={days}",
        f"bars={len(bars)}",
        f"contract_size={cs}",
        f"margin_per_leg={margin_per_leg} leverage={leverage}",
        f"initial_cash_usdt={INITIAL_CASH_USDT:.4f}",
        f"final_equity_usdt={final_eq:.4f}",
        f"net_profit_usdt={net_profit:.4f}",
        f"wallet_cash_after_flat_usdt={st.cash:.4f}",
        f"total_fees_paid_usdt={st.total_fees:.6f}",
        f"total_closes={st.close_count}",
        f"wins_net_pnl_gt0={wins_net} win_rate_net={wins_net / max(st.close_count, 1):.4f}",
        f"wins_gross_pnl_gt0={wins_gross} win_rate_gross={wins_gross / max(st.close_count, 1):.4f}",
        f"liquidations={st.liq_count}",
        f"micro_takes={st.micro_count}",
        f"max_drawdown={st.max_drawdown:.6f}",
        f"taker_fee_rate={TAKER_FEE_RATE}",
        f"micro_first_hit_samples={mfs}",
        f"intra_bar_whipsaw_hl_pairs={wh}",
    ]
    summary_text = "\n".join(summary_lines) + "\n"
    summary_path = os.path.join(out_dir, "backtest_summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(summary_text)

    hist_path = os.path.join(out_dir, "backtest_history_positions.json")
    envelope = {
        "schema": "shark.backtest_history.v1",
        "generated_at": time.time(),
        "symbol": symbol,
        "trades": st.autopsies,
    }
    with open(hist_path, "w", encoding="utf-8") as f:
        json.dump(envelope, f, ensure_ascii=False, indent=2)

    print(summary_text)
    print(f"[BT] 已写入 {hist_path}")
    print(f"[BT] 已写入 {summary_path}")
    print(
        "[BT] 将 trades[] 中单条 JSON 拷入 settings.yaml 中 darwin.autopsy_dir 可在面板 trade_history 展示。"
    )
    return cast(
        Dict[str, Any],
        {
            "symbol": symbol,
            "days": int(days),
            "bars": len(bars),
            "contract_size": float(cs),
            "margin_per_leg": float(margin_per_leg),
            "leverage": float(leverage),
            "initial_cash_usdt": float(INITIAL_CASH_USDT),
            "final_equity_usdt": float(final_eq),
            "net_profit_usdt": float(net_profit),
            "wallet_cash_after_flat_usdt": float(st.cash),
            "total_fees_paid_usdt": float(st.total_fees),
            "total_closes": int(st.close_count),
            "wins_net_pnl_gt0": int(wins_net),
            "win_rate_net": float(wins_net / max(st.close_count, 1)),
            "liquidations": int(st.liq_count),
            "micro_takes": int(st.micro_count),
            "max_drawdown": float(st.max_drawdown),
            "taker_fee_rate": float(TAKER_FEE_RATE),
            "micro_first_hit_samples": int(mfs),
            "intra_bar_whipsaw_hl_pairs": int(wh),
            "summary_path": summary_path,
            "history_path": hist_path,
        },
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Shark 30d 1m 回测 + Darwin v2 注入文件")
    ap.add_argument("--symbol", default="DOGE/USDT", help="合约符号，如 DOGE/USDT")
    ap.add_argument("--days", type=int, default=30, help="回溯天数")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN_PER_LEG, help="逐仓每腿初始保证金 USDT")
    ap.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE, help="杠杆倍数")
    ap.add_argument(
        "--out-dir",
        default=os.path.join(ROOT, "data", "backtest"),
        help="输出目录（json + summary）",
    )
    ap.add_argument(
        "--insecure",
        action="store_true",
        help="跳过 TLS 证书校验（仅本机回测）；也可用环境变量 BACKTEST_INSECURE_SSL=1",
    )
    ap.add_argument(
        "--micro-hit-samples",
        type=int,
        default=DEFAULT_MICRO_FIRST_HIT_SAMPLES,
        help=f"沿每段价格路径搜索首个微利触发的均匀采样数（默认 {DEFAULT_MICRO_FIRST_HIT_SAMPLES}，越大越不易漏触发）",
    )
    ap.add_argument(
        "--intra-bar-whipsaw-pairs",
        type=int,
        default=0,
        help="每根 1m K 内于 high/low 间追加的完整往返次数(压力近似，非真实盘口)；"
        "默认 0 仅 OHLC 折线，故成交笔数通常远小于实盘",
    )
    args = ap.parse_args()
    insecure = bool(args.insecure) or os.environ.get("BACKTEST_INSECURE_SSL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if insecure:
        print("[BT] 警告: 已启用 --insecure / BACKTEST_INSECURE_SSL，HTTPS 不校验证书。")
    run_backtest(
        symbol=str(args.symbol),
        days=int(args.days),
        margin_per_leg=float(args.margin),
        leverage=float(args.leverage),
        out_dir=str(args.out_dir),
        insecure_ssl=insecure,
        micro_first_hit_samples=int(args.micro_hit_samples),
        intra_bar_whipsaw_hl_pairs=int(args.intra_bar_whipsaw_pairs),
    )


if __name__ == "__main__":
    main()
