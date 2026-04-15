import asyncio
import json
import os
import threading
from typing import Any, Dict, List

from src.utils.logger import log
from src.core.config_manager import config_manager
from src.darwin.experience_store import append_from_autopsy

_BATCH_LOCK = threading.Lock()
_BATCH_BUFFER: List[Dict[str, Any]] = []


def _persist_autopsy(snapshot: Dict[str, Any]) -> None:
    cfg = config_manager.get_config().darwin
    if not cfg.log_autopsies:
        return
    d = cfg.autopsy_dir
    os.makedirs(d, exist_ok=True)
    path = os.path.join(
        d,
        f"{int(snapshot.get('closed_at', 0) * 1000)}_{snapshot.get('symbol', 'x').replace('/', '_')}.json",
    )
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, default=str)
        log.info(f"[Darwin] Autopsy written {path}")
    except OSError as e:
        log.error(f"[Darwin] Failed to write autopsy: {e}")


async def _post_hook(url: str, payload: Dict[str, Any]) -> None:
    if not url:
        return
    try:
        import aiohttp

        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=aiohttp.ClientTimeout(total=30)
            ) as resp:
                if resp.status >= 400:
                    log.warning(f"[Darwin] Researcher hook HTTP {resp.status}")
    except Exception as e:
        log.warning(f"[Darwin] Researcher hook failed: {e}")


def schedule_trade_autopsy(snapshot: Dict[str, Any]) -> None:
    """
    Fire-and-forget: persist JSON, optional webhook, reflection (per-trade or batch L3).
    Safe to call from synchronous paper_engine when a asyncio loop is running.
    """
    cfg = config_manager.get_config().darwin
    if not cfg.enabled:
        return

    _persist_autopsy(snapshot)
    try:
        append_from_autopsy(snapshot)
    except Exception:
        pass

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.warning("[Darwin] No running event loop; autopsy persisted only (no async researcher).")
        return

    mode = (cfg.reflection_mode or "batch").strip().lower()
    if mode == "persist_only":
        return

    if mode == "per_trade":
        loop.create_task(_run_researcher_pipeline(snapshot))
        return

    if mode == "batch":
        bs = max(1, int(cfg.batch_size))
        with _BATCH_LOCK:
            _BATCH_BUFFER.append(snapshot)
            if len(_BATCH_BUFFER) >= bs:
                chunk = _BATCH_BUFFER[:bs]
                del _BATCH_BUFFER[:bs]
            else:
                chunk = None
        if chunk:
            loop.create_task(_run_batch_evolution_pipeline(chunk))
        return

    log.warning(f"[Darwin] Unknown reflection_mode={mode!r}; use persist_only|per_trade|batch")


async def _run_researcher_pipeline(snapshot: Dict[str, Any]) -> None:
    cfg = config_manager.get_config().darwin
    await _post_hook(cfg.researcher_hook_url, snapshot)
    try:
        from src.darwin.researcher import DarwinResearcher

        await DarwinResearcher().run(snapshot)
    except Exception as e:
        log.error(f"[Darwin] Researcher pipeline error: {e}")


async def _run_batch_evolution_pipeline(chunk: List[Dict[str, Any]]) -> None:
    cfg = config_manager.get_config().darwin
    await _post_hook(
        cfg.researcher_hook_url,
        {
            "schema": "darwin.evolution_hook.v1",
            "count": len(chunk),
            "autopsies": chunk,
        },
    )
    try:
        from src.darwin.evolution import DarwinBatchEvolution

        await DarwinBatchEvolution().run(chunk)
    except Exception as e:
        log.error(f"[Darwin/L3] Batch evolution error: {e}")
