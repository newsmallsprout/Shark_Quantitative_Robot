#!/usr/bin/env python3
"""
Shark 战报生成器 — 每10分钟自动生成多维度战报
在容器内执行: docker exec shark2 python3 /app/battle_report.py
"""

import json, sys, os, math
from datetime import datetime

API = os.environ.get("SHARK_API", "http://localhost:80/api")
TOKEN = os.environ.get("SHARK_API_TOKEN", "")
HEADERS = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}


def fetch(endpoint: str) -> dict:
    import urllib.request
    url = f"{API}/{endpoint}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read())
    except Exception as e:
        return {"error": str(e)}


def format_pct(v: float) -> str:
    return f"{v:+.1f}%" if v else "0%"


def format_usd(v: float) -> str:
    return f"${v:+,.2f}" if v < 0 else f"${v:,.2f}"


def main():
    status = fetch("status")
    history = fetch("history?offset=0&limit=30")
    
    if "error" in status:
        print(f"❌ API不可达: {status['error']}")
        return

    now = datetime.now().strftime("%H:%M")
    balance = status.get("balance", 0)
    equity = status.get("equity", 0)
    static_eq = status.get("static_equity", 0)
    initial = status.get("initial_capital", 200)
    gross = status.get("gross_realized", 0)
    total_fees = status.get("total_fees", 0)
    free_cash = status.get("free_cash", balance)
    pnl_total = balance - initial
    drawdown = status.get("drawdown_pct", (balance - initial) / initial * 100)

    # 仓位
    positions = status.get("position_list", [])
    pos_count = len(positions)
    total_margin = sum(p.get("margin", 0) for p in positions)
    
    # 交易历史
    trades = history.get("trades", [])
    recent = trades[:20]
    wins = [t for t in recent if t.get("realized_pnl", 0) > 0]
    losses = [t for t in recent if t.get("realized_pnl", 0) <= 0]
    win_rate = len(wins) / len(recent) * 100 if recent else 0
    total_pnl = sum(t.get("realized_pnl", 0) for t in recent)
    best_trade = max(recent, key=lambda t: t.get("realized_pnl", 0), default=None)
    worst_trade = min(recent, key=lambda t: t.get("realized_pnl", 0), default=None)

    # 方向分布
    longs = [t for t in recent if t.get("side") == "long"]
    shorts = [t for t in recent if t.get("side") == "short"]
    long_wr = sum(1 for t in longs if t.get("realized_pnl", 0) > 0) / len(longs) * 100 if longs else 0
    short_wr = sum(1 for t in shorts if t.get("realized_pnl", 0) > 0) / len(shorts) * 100 if shorts else 0

    # 止损vs止盈
    stops = [t for t in recent if "止损" in t.get("reason", "")]
    takes = [t for t in recent if "止盈" in t.get("reason", "")]
    stop_rate = len(stops) / len(recent) * 100 if recent else 0

    # 主流vs山寨
    stable_syms = {"BTC/USDT", "ETH/USDT"}
    stable_trades = [t for t in recent if t.get("symbol") in stable_syms]
    alt_trades = [t for t in recent if t.get("symbol") not in stable_syms]
    stable_pnl = sum(t.get("realized_pnl", 0) for t in stable_trades)
    alt_pnl = sum(t.get("realized_pnl", 0) for t in alt_trades)

    # 反思统计
    reflect = status.get("reflect", {}) or {}
    reflect_summary = reflect.get("summary", "")
    ai_boost = reflect.get("ai_boost", 0)
    stop_boost = reflect.get("stop_boost", 0)

    # ── 战报正文 ──
    lines = []
    lines.append(f"🦈 Shark 战报 {now}")
    lines.append("")
    
    # 资金
    lines.append("💰 资金")
    lines.append(f"   余额: {format_usd(balance)}  权益: {format_usd(equity)}  静态: {format_usd(static_eq)}")
    lines.append(f"   累计毛利: {format_usd(gross)}  手续费: {format_usd(total_fees)}")
    pnl_icon = "📈" if pnl_total > 0 else "📉" if pnl_total < 0 else "➡"
    lines.append(f"   总盈亏: {format_usd(pnl_total)} ({format_pct(drawdown)})  回撤: {drawdown:.1f}% {pnl_icon}")
    lines.append("")

    # 持仓
    lines.append(f"📊 持仓 ({pos_count}个)  占用保证金: {format_usd(total_margin)}")
    for p in positions:
        sym = p.get("symbol", "?")
        side = p.get("side", "?")
        side_icon = "🟢" if side == "long" else "🔴"
        entry = p.get("entry_price", 0)
        margin = p.get("margin", 0)
        pnl = p.get("unrealized_pnl", 0)
        pnl_pct = p.get("pnl_pct", 0)
        pnl_s = f"{format_usd(pnl)}({format_pct(pnl_pct)})" if pnl else ""
        lines.append(f"   {side_icon} {sym} {side.upper()} @{entry:.4f} 保证金{margin:.2f} {pnl_s}")
    if not positions:
        lines.append("   (空仓)")
    lines.append("")

    # 近期战绩
    lines.append(f"⚔ 近{len(recent)}笔战绩")
    lines.append(f"   胜率: {win_rate:.0f}% ({len(wins)}W/{len(losses)}L)")
    lines.append(f"   净利: {format_usd(total_pnl)}")
    if best_trade:
        lines.append(f"   最佳: {best_trade['symbol']} {format_usd(best_trade['realized_pnl'])}")
    if worst_trade:
        lines.append(f"   最差: {worst_trade['symbol']} {format_usd(worst_trade['realized_pnl'])}")
    lines.append(f"   止损率: {stop_rate:.0f}% ({len(stops)}/{len(recent)})")
    lines.append("")

    # 维度分析
    lines.append("🔬 多维度分析")
    lines.append(f"   做多胜率: {long_wr:.0f}% ({len(longs)}笔)")
    lines.append(f"   做空胜率: {short_wr:.0f}% ({len(shorts)}笔)")
    lines.append(f"   主流盈亏: {format_usd(stable_pnl)} ({len(stable_trades)}笔)")
    lines.append(f"   山寨盈亏: {format_usd(alt_pnl)} ({len(alt_trades)}笔)")
    lines.append("")

    # 进化调整
    if reflect_summary or ai_boost or stop_boost:
        lines.append("🧬 进化引擎")
        if reflect_summary:
            lines.append(f"   反思: {reflect_summary}")
        adjusts = []
        if ai_boost:
            adjusts.append(f"AI阈值+{ai_boost}")
        if stop_boost:
            adjusts.append(f"止损放宽+{stop_boost}%")
        if adjusts:
            lines.append(f"   调整: {', '.join(adjusts)}")
        lines.append("")

    # 信号源分布
    ai_count = sum(1 for t in recent if "AI多维" in str(t.get("signal_src", "")))
    fb_count = sum(1 for t in recent if "兜底" in str(t.get("signal_src", "")))
    lines.append(f"📡 信号源: AI {ai_count} | 兜底 {fb_count}")
    lines.append("")

    report = "\n".join(lines)
    print(report)
    return report


if __name__ == "__main__":
    main()
