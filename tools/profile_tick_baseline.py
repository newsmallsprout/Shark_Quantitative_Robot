#!/usr/bin/env python3
"""
无网络基线：对 main.StrategyRunner.tick 用合成行情跑固定次数，打印 cProfile 累计时间前若干条。

用法（仓库根目录）:
  python tools/profile_tick_baseline.py
  python -m cProfile -o /tmp/shark.prof tools/profile_tick_baseline.py  # 另存 stats 文件

环境变量:
  PROFILE_TICKS  单次脚本内 tick 次数，默认 400
  PROFILE_TOP    print_stats 行数，默认 35
"""

from __future__ import annotations

import asyncio
import cProfile
import os
import pstats
import sys
from io import StringIO
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from main import ContractSpec, StrategyRunner  # noqa: E402


def _synthetic_universe(n: int = 30) -> tuple[list[str], dict[str, ContractSpec]]:
    symbols = [f"SYN{i}/USDT" for i in range(n)]
    specs = {s: ContractSpec(symbol=s) for s in symbols}
    return symbols, specs


async def _run_ticks(runner: StrategyRunner, n: int) -> None:
    symbols, _ = _synthetic_universe()
    base = 50_000.0
    prices = {s: base + i * 10.0 for i, s in enumerate(symbols)}
    volumes = {s: 1_000_000.0 for s in symbols}
    changes = {s: 0.5 for s in symbols}
    funding = {s: 0.0001 for s in symbols}
    marks = dict(prices)
    for _ in range(n):
        await runner.tick(prices, volumes, changes, funding, marks)


def main() -> None:
    ticks = int(os.environ.get("PROFILE_TICKS", "400"))
    top = int(os.environ.get("PROFILE_TOP", "35"))
    symbols, specs = _synthetic_universe()
    runner = StrategyRunner(initial_balance=200.0)
    runner.update_contracts(specs)

    pr = cProfile.Profile()
    pr.enable()
    asyncio.run(_run_ticks(runner, ticks))
    pr.disable()

    buf = StringIO()
    st = pstats.Stats(pr, stream=buf).sort_stats(pstats.SortKey.CUMULATIVE)
    st.print_stats(top)
    print(f"=== StrategyRunner.tick × {ticks} (symbols={len(symbols)}, no I/O) ===")
    print(buf.getvalue())


if __name__ == "__main__":
    main()
