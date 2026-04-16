#!/usr/bin/env python3
"""
逐笔（成交）回放：用 Gate 公开 /futures/usdt/trades 的 from/to（秒）拉成交，
按与 backtest_runner 相同的圆桌微利 + 强平规则推进，成交密度接近实盘盘口量级。

写入 Darwin 仓库（与实盘一致）：
  - 默认读取 config/settings.yaml → darwin.autopsy_dir，逐笔写入 darwin.trade_autopsy.v2 JSON
  - 可选追加 data/darwin/experience.jsonl（与 schedule_trade_autopsy 行为对齐的轻量版）

注意：
  - 历史窗口越长，REST 请求越多；单窗返回满 1000 条时会自动二分时间窗。
  - 首次建议：--seconds 600 试跑，再加 --write-darwin。

用法（项目根）:
  python3 tick_replay_runner.py --symbol DOGE/USDT --seconds 3600 --insecure
  python3 tick_replay_runner.py --symbol DOGE/USDT --seconds 86400 --write-darwin
  python3 tick_replay_runner.py --symbol DOGE/USDT --seconds 604800 --write-darwin --autopsy-dir data/darwin/autopsies
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backtest_runner import (  # noqa: E402
    DEFAULT_LEVERAGE,
    DEFAULT_MARGIN_PER_LEG,
    GATE_REST,
    INITIAL_CASH_USDT,
    BacktestState,
    fetch_gate_server_time_sec,
    fetch_json,
    fetch_quanto,
    init_http_ssl,
)
def _contract_id(symbol: str) -> str:
    return str(symbol).replace("/", "_").replace(":", "_")


def _parse_trade_row(row: Any) -> Optional[Tuple[float, float, int]]:
    if not isinstance(row, dict):
        return None
    try:
        tid = int(row.get("id", 0) or 0)
        ct = float(row.get("create_time_ms") or row.get("create_time") or 0.0)
        if ct > 1e12:
            ct /= 1000.0
        px = float(row.get("price") or 0.0)
        if px <= 0 or tid <= 0 or ct <= 0:
            return None
        return (ct, px, tid)
    except (TypeError, ValueError):
        return None


def fetch_trades_window(
    contract: str,
    t_from: int,
    t_to: int,
) -> List[Dict[str, Any]]:
    if t_to <= t_from:
        return []
    url = (
        f"{GATE_REST}/futures/usdt/trades?"
        f"contract={contract}&limit=1000&from={int(t_from)}&to={int(t_to)}"
    )
    data = fetch_json(url)
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict)]


def fetch_trades_recursive(
    contract: str,
    t_from: int,
    t_to: int,
) -> List[Dict[str, Any]]:
    """按时间窗拉取；满 1000 则二分时间（避免丢成交）。"""
    rows = fetch_trades_window(contract, t_from, t_to)
    if len(rows) < 1000:
        return rows
    if t_to - t_from <= 1:
        return rows
    mid = t_from + (t_to - t_from) // 2
    a = fetch_trades_recursive(contract, t_from, mid)
    b = fetch_trades_recursive(contract, mid, t_to)
    return a + b


def iter_ticks_chronological(
    contract: str,
    t_start: int,
    t_end: int,
    *,
    step_sec: int,
) -> Iterable[Tuple[float, float, int]]:
    seen: Set[int] = set()
    t = int(t_start)
    step = max(5, int(step_sec))
    while t < t_end:
        te = min(t + step, t_end)
        chunk = fetch_trades_recursive(contract, t, te)
        parsed: List[Tuple[float, float, int]] = []
        for row in chunk:
            p = _parse_trade_row(row)
            if p is None:
                continue
            ct, px, tid = p
            if tid in seen:
                continue
            seen.add(tid)
            parsed.append((ct, px, tid))
        parsed.sort(key=lambda x: (x[0], x[2]))
        for item in parsed:
            yield item
        t = te


def _write_autopsy_file(autopsy_dir: str, doc: Dict[str, Any], seq: int) -> str:
    os.makedirs(autopsy_dir, exist_ok=True)
    sym = str(doc.get("symbol", "x")).replace("/", "_").replace(":", "_")
    ms = int(float(doc.get("closed_at", 0) or 0) * 1000)
    path = os.path.join(autopsy_dir, f"{ms}_{sym}_{seq:06d}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2, default=str)
    return path


def run_tick_replay(
    *,
    symbol: str,
    seconds: int,
    margin_per_leg: float,
    leverage: float,
    insecure_ssl: bool,
    write_darwin: bool,
    autopsy_dir: Optional[str],
    append_experience: bool,
    step_sec: int,
    max_ticks: int,
) -> None:
    init_http_ssl(insecure=bool(insecure_ssl))
    contract = _contract_id(symbol)
    cs = fetch_quanto(symbol)
    server_now = fetch_gate_server_time_sec()
    t_end = int(server_now)
    t_start = max(0, t_end - max(1, int(seconds)))

    from src.core.config_manager import config_manager  # noqa: WPS433

    cfg_dir = (
        str(autopsy_dir).strip()
        if autopsy_dir
        else str(config_manager.get_config().darwin.autopsy_dir or "data/darwin/autopsies")
    )

    print(
        f"[TR] symbol={symbol} contract={contract} quanto={cs} "
        f"window=[{t_start},{t_end}] span={t_end - t_start}s step={step_sec}s "
        f"write_darwin={write_darwin} autopsy_dir={cfg_dir}"
    )

    st = BacktestState(
        symbol=symbol,
        contract_size=cs,
        margin_per_leg=float(margin_per_leg),
        leverage=float(leverage),
    )
    st.autopsy_strategy_name = "tick_replay_runner"

    ticks = 0
    written = 0
    first_px: Optional[float] = None
    seq = 0
    last_px = float("nan")

    for ts, px, _tid in iter_ticks_chronological(contract, t_start, t_end, step_sec=step_sec):
        if first_px is None:
            first_px = px
            st.initial_dual_open(first_px, 0, ts)
            if st.long_leg is None or st.short_leg is None:
                raise SystemExit("[TR] 初始双开失败（现金或张数）")
            print(f"[TR] 双开 entry_px≈{first_px:.8f}")
        ticks += 1
        if ticks > max_ticks:
            print(f"[TR] 已达 max_ticks={max_ticks}，停止回放。")
            break

        st.sync_micro_minute(ts)
        bar_idx = int(ts // 60)
        st.update_path_stats(px)
        st.update_dd(px)
        st.try_liquidation_at_price(px, ts)
        st.try_micro_at_price(px, ts, bar_idx)
        last_px = px
    if first_px is not None and last_px == last_px:  # not NaN
        st.force_flat(last_px, float(t_end) + 1.0)

    if write_darwin:
        try:
            from src.darwin.experience_store import append_from_autopsy
        except Exception:
            append_from_autopsy = None  # type: ignore
        for doc in st.autopsies:
            seq += 1
            p = _write_autopsy_file(cfg_dir, doc, seq)
            written += 1
            if append_experience and append_from_autopsy is not None:
                try:
                    append_from_autopsy(doc)
                except Exception:
                    pass
            if written <= 3 or written % 500 == 0:
                print(f"[TR] wrote {p}")

    final_eq = st.equity(last_px) if first_px is not None else float(INITIAL_CASH_USDT)
    print(
        f"[TR] ticks={ticks} closes={st.close_count} micro={st.micro_count} liq={st.liq_count} "
        f"fees_paid={st.total_fees:.6f} equity≈{final_eq:.4f} autopsy_files_written={written}"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="Gate 成交逐笔回放 → 可选写入 Darwin autopsy 目录")
    ap.add_argument("--symbol", default="DOGE/USDT")
    ap.add_argument("--seconds", type=int, default=600, help="回溯最近多少秒的成交（从 Gate server_time 起算）")
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN_PER_LEG)
    ap.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE)
    ap.add_argument(
        "--step-sec",
        type=int,
        default=90,
        help="REST 按块拉取成交的时间窗（秒）；过大易触顶 1000 条/请求",
    )
    ap.add_argument("--max-ticks", type=int, default=2_000_000, help="最多处理多少笔成交（防爆）")
    ap.add_argument("--write-darwin", action="store_true", help="将每笔 autopsy 写入 autopsy_dir（实盘同款目录）")
    ap.add_argument("--autopsy-dir", default="", help="覆盖 Darwin autopsy 目录；默认读 settings.yaml")
    ap.add_argument(
        "--experience-append",
        action="store_true",
        help="写入 autopsy 后尝试追加 experience.jsonl（需 Darwin enabled）",
    )
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()
    insecure = bool(args.insecure) or os.environ.get("BACKTEST_INSECURE_SSL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if insecure:
        print("[TR] 警告: HTTPS 不校验证书。")
    ad = str(args.autopsy_dir or "").strip() or None
    run_tick_replay(
        symbol=str(args.symbol),
        seconds=int(args.seconds),
        margin_per_leg=float(args.margin),
        leverage=float(args.leverage),
        insecure_ssl=insecure,
        write_darwin=bool(args.write_darwin),
        autopsy_dir=ad,
        append_experience=bool(args.experience_append),
        step_sec=int(args.step_sec),
        max_ticks=int(args.max_ticks),
    )


if __name__ == "__main__":
    main()
