#!/usr/bin/env python3
"""Shark 2.0 自我进化引擎 — 定期评估表现并自动调整策略参数"""
import json, sys, os, subprocess, time

def main():
    try:
        import urllib.request
        resp = urllib.request.urlopen("http://localhost:80/api/status", timeout=10)
        status = json.loads(resp.read())
    except Exception as e:
        print(f"[进化] 无法获取状态: {e}")
        sys.exit(1)

    equity = status.get("equity", 0)
    balance = status.get("balance", 0)
    win_rate = status.get("win_rate", 0.5)
    realized = status.get("realized_pnl", 0)
    trades = status.get("trades", 0)
    wins = status.get("wins", 0)
    history = status.get("trade_history", [])

    changes = []
    initial = 10000.0
    pnl_pct = (equity - initial) / initial * 100 if initial > 0 else 0

    print(f"[进化] 当前: equity={equity:.2f} pnl={pnl_pct:+.1f}% 胜率={win_rate:.1%} 已交易={trades}笔")

    # 分析最近10笔
    recent = history[-10:]
    recent_pnls = [t.get("realized_pnl", 0) for t in recent]
    recent_losers = [p for p in recent_pnls if p < 0]
    recent_winners = [p for p in recent_pnls if p > 0]
    consecutive_losses = 0
    for p in reversed(recent_pnls):
        if p < 0:
            consecutive_losses += 1
        else:
            break

    # ── 规则1：连亏3笔 → 防御模式 ──
    if consecutive_losses >= 3:
        print(f"[进化] ⚠️ 连亏{consecutive_losses}笔，启动防御模式")
        changes.append("defense")

    # ── 规则2：回撤>8% → 缩仓 ──
    if pnl_pct < -8:
        print(f"[进化] ⚠️ 回撤{pnl_pct:.1f}%，缩仓50%")
        changes.append("shrink")

    # ── 规则3：胜率过低 → 收紧开仓 ──
    if win_rate < 0.3 and trades > 10:
        print(f"[进化] ⚠️ 胜率{win_rate:.1%}过低，建议提高AI阈值")
        changes.append("tighten_ai")

    # ── 规则4：稳健盈利 → 适当扩张 ──
    if win_rate > 0.7 and trades > 20 and pnl_pct > 5:
        print(f"[进化] ✅ 表现优异，适当放宽")
        changes.append("expand")

    if not changes:
        print("[进化] 无需调整，系统运行正常")
        return

    # 应用调整 — 修改此脚本对应的配置文件
    strategy_file = "/Users/chenshun/Desktop/Shark_Quantitative_Robot/dual_strategy.py"
    # 简化版：仅输出建议，实际调整由AI agent执行
    for c in changes:
        if c == "defense":
            print("  → 建议: 主流margin_pct降低到0.02, AI置信阈值提高到40")
        elif c == "shrink":
            print("  → 建议: 全部margin_pct减半")
        elif c == "tighten_ai":
            print("  → 建议: AI置信阈值提高到45")
        elif c == "expand":
            print("  → 建议: 主流margin_pct提高到0.04, AI置信阈值降到30")


if __name__ == "__main__":
    main()
