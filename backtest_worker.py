#!/usr/bin/env python3
"""
独立进程：按配置批量跑 1m OHLC 回测（与 backtest_runner 同一引擎），
默认使用 settings.yaml 里 beta_neutral_hf.symbols（当前约 20 个 ALT 对 BTC 锚的币对）。

每个币对单独子目录，并写总 manifest 便于面板或后续任务消费。

用法（项目根）:
  python3 backtest_worker.py --days 7 --insecure
  python3 backtest_worker.py --symbol-source strategy --days 30
  # 双线并行（两路同时拉 K / 跑回测；注意 Gate 公网限频，若 429/超时请降 workers 或拉长间隔）
  python3 backtest_worker.py --days 7 --insecure --workers 2

Docker（可选 profile）:
  docker compose --profile backtest run --rm backtest-worker
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import traceback
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backtest_runner import (  # noqa: E402
    DEFAULT_LEVERAGE,
    DEFAULT_MARGIN_PER_LEG,
    run_backtest,
    single_backtest_job,
)


def _resolve_symbols(source: str) -> List[str]:
    from src.core.config_manager import config_manager

    cfg = config_manager.get_config()
    if source == "strategy":
        raw = list(getattr(cfg.strategy, "symbols", None) or [])
    else:
        raw = list(getattr(cfg.beta_neutral_hf, "symbols", None) or [])
    if not raw:
        raw = list(getattr(cfg.strategy, "symbols", None) or [])
    out: List[str] = []
    seen = set()
    for s in raw:
        t = str(s).strip()
        if not t or t in seen:
            continue
        seen.add(t)
        out.append(t)
    return out


def _safe_dir(sym: str) -> str:
    return sym.replace("/", "_").replace(":", "_")


def main() -> None:
    ap = argparse.ArgumentParser(description="批量回测：默认 beta_neutral_hf 币对列表")
    ap.add_argument(
        "--symbol-source",
        choices=("beta_neutral_hf", "strategy"),
        default="beta_neutral_hf",
        help="从配置哪一段读取币对列表（默认与 BetaNeutralHF 20 腿一致）",
    )
    ap.add_argument(
        "--symbols",
        default="",
        help="逗号分隔覆盖列表；非空时忽略 --symbol-source",
    )
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--margin", type=float, default=DEFAULT_MARGIN_PER_LEG)
    ap.add_argument("--leverage", type=float, default=DEFAULT_LEVERAGE)
    ap.add_argument(
        "--out-dir",
        default=os.path.join(ROOT, "data", "backtest_batch"),
        help="总输出根目录；每个币对写入子目录",
    )
    ap.add_argument("--insecure", action="store_true")
    ap.add_argument("--sleep-sec", type=float, default=0.35, help="顺序模式 (--workers 1) 下每币对之间的休眠，减轻 Gate 公网限频")
    ap.add_argument(
        "--workers",
        type=int,
        default=1,
        help="并行进程数；2 即双线并行。>1 时不再使用 --sleep-sec（并发已加速，请注意限频）",
    )
    ap.add_argument("--continue-on-error", action="store_true", default=True)
    ap.add_argument("--no-continue-on-error", action="store_false", dest="continue_on_error")
    ap.add_argument(
        "--micro-hit-samples",
        type=int,
        default=0,
        help="0 表示使用 backtest_runner 默认值",
    )
    ap.add_argument("--intra-bar-whipsaw-pairs", type=int, default=0)
    args = ap.parse_args()

    insecure = bool(args.insecure) or os.environ.get("BACKTEST_INSECURE_SSL", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    if insecure:
        print("[BW] 警告: HTTPS 不校验证书。")

    if str(args.symbols).strip():
        symbols = [s.strip() for s in str(args.symbols).split(",") if s.strip()]
    else:
        symbols = _resolve_symbols(str(args.symbol_source))

    if not symbols:
        raise SystemExit("[BW] 币对列表为空：请检查 config/settings.yaml 或传入 --symbols")

    os.makedirs(str(args.out_dir), exist_ok=True)
    batch_id = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
    manifest_path = os.path.join(str(args.out_dir), f"manifest_{batch_id}.json")
    rows: List[Dict[str, Any]] = []

    workers = max(1, int(args.workers))
    print(
        f"[BW] batch_id={batch_id} symbols={len(symbols)} workers={workers} "
        f"source={args.symbol_source!r} days={args.days}"
    )
    mfs = int(args.micro_hit_samples) if int(args.micro_hit_samples) > 0 else None

    if workers == 1:
        for i, sym in enumerate(symbols):
            sub = os.path.join(str(args.out_dir), _safe_dir(sym))
            print(f"[BW] --- [{i + 1}/{len(symbols)}] {sym} -> {sub} ---")
            t0 = time.time()
            err: Optional[str] = None
            result: Optional[Dict[str, Any]] = None
            try:
                kwargs: Dict[str, Any] = dict(
                    symbol=sym,
                    days=int(args.days),
                    margin_per_leg=float(args.margin),
                    leverage=float(args.leverage),
                    out_dir=sub,
                    insecure_ssl=insecure,
                    intra_bar_whipsaw_hl_pairs=int(args.intra_bar_whipsaw_pairs),
                )
                if mfs is not None:
                    kwargs["micro_first_hit_samples"] = mfs
                result = run_backtest(**kwargs)
                result = dict(result)
                result["elapsed_sec"] = round(time.time() - t0, 3)
                result["error"] = None
            except (SystemExit, KeyboardInterrupt):
                raise
            except BaseException as e:
                err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                print(f"[BW] ERROR {sym}: {err}")
                result = {
                    "symbol": sym,
                    "error": err,
                    "elapsed_sec": round(time.time() - t0, 3),
                }
                if not bool(args.continue_on_error):
                    raise
            row = dict(result or {"symbol": sym, "error": err})
            row["batch_id"] = batch_id
            rows.append(row)
            if float(args.sleep_sec) > 0 and i + 1 < len(symbols):
                time.sleep(float(args.sleep_sec))
    else:
        payloads: List[Tuple[int, Dict[str, Any]]] = []
        for i, sym in enumerate(symbols):
            sub = os.path.join(str(args.out_dir), _safe_dir(sym))
            kwargs: Dict[str, Any] = dict(
                symbol=sym,
                days=int(args.days),
                margin_per_leg=float(args.margin),
                leverage=float(args.leverage),
                out_dir=sub,
                insecure_ssl=insecure,
                intra_bar_whipsaw_hl_pairs=int(args.intra_bar_whipsaw_pairs),
            )
            if mfs is not None:
                kwargs["micro_first_hit_samples"] = mfs
            payloads.append((i, {"symbol": sym, "kwargs": kwargs}))

        ordered: List[Optional[Dict[str, Any]]] = [None] * len(symbols)
        with ProcessPoolExecutor(max_workers=workers) as ex:
            futs = {ex.submit(single_backtest_job, pl[1]): pl[0] for pl in payloads}
            for fut in as_completed(futs):
                idx = futs[fut]
                sym = symbols[idx]
                try:
                    result = fut.result()
                except (SystemExit, KeyboardInterrupt):
                    raise
                except BaseException as e:
                    err = f"{type(e).__name__}: {e}\n{traceback.format_exc()}"
                    print(f"[BW] ERROR {sym}: {err}")
                    result = {
                        "symbol": sym,
                        "error": err,
                        "elapsed_sec": None,
                    }
                    if not bool(args.continue_on_error):
                        raise
                err = result.get("error") if isinstance(result, dict) else None
                if err:
                    print(f"[BW] ERROR {sym}: {err}")
                    if not bool(args.continue_on_error):
                        raise SystemExit(f"[BW] 失败: {sym}")
                else:
                    print(f"[BW] OK {sym} elapsed={result.get('elapsed_sec')}")
                ordered[idx] = result

        for i, sym in enumerate(symbols):
            r = ordered[i]
            if r is None:
                r = {"symbol": sym, "error": "missing result", "elapsed_sec": None}
            row = dict(r)
            row["batch_id"] = batch_id
            rows.append(row)

    manifest = {
        "schema": "shark.backtest_batch.v1",
        "batch_id": batch_id,
        "generated_at": time.time(),
        "symbol_source": str(args.symbol_source),
        "workers": int(workers),
        "days": int(args.days),
        "margin_per_leg": float(args.margin),
        "leverage": float(args.leverage),
        "runs": rows,
    }
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2, default=str)

    ok = sum(1 for r in rows if not r.get("error"))
    print(f"[BW] 完成: ok={ok}/{len(rows)} manifest={manifest_path}")


if __name__ == "__main__":
    main()
