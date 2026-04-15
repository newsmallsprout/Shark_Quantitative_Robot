import asyncio
import sys
import os
import multiprocessing
from pathlib import Path
from typing import Optional
import uvicorn
from uvicorn import Config, Server

from src.utils.logger import setup_logger, log
from src.core.config_manager import config_manager
from src.license_manager.validator import LicenseValidator
from src.exchange.gate_gateway import GateFuturesGateway
from src.core.state_machine import StateMachine
from src.strategy.engine import StrategyEngine
from src.api.server import app as api_app
from src.ai.analyzer import MarketAnalyzer, ai_context
from src.core.globals import bot_context
from src.core.risk_engine import risk_engine
from src.core.ipc import ZMQSubscriber
from src.runtime_obf import (
    IPC_SUBSCRIBE_DEFAULT_TOPICS,
    IPC_TOPIC_AI_SCORE,
    IPC_TOPIC_L1_TUNING,
    IPC_TOPIC_L2_SYMBOLS,
)
from src.ai.regime import MarketRegime

async def start_api_server():
    """Runs the FastAPI server as an async task in the same loop"""
    config = Config(app=api_app, host="0.0.0.0", port=8002, log_level="warning")
    server = Server(config)
    log.info("API Server starting at http://localhost:8002")
    await server.serve()

def run_ai_worker():
    """Entry point for the isolated AI worker process"""
    setup_logger()
    log.info("Starting AI Worker Process...")
    # 子进程 spawn 时 cwd 可能不是项目根，曾导致误读 cwd 下其它 config/settings.yaml → darwin.llm_api_key 空 → 全程 MockLLM
    try:
        root = Path(__file__).resolve().parent
        os.chdir(str(root))
        os.environ.setdefault("SHARK_CONFIG_PATH", str(root / "config" / "settings.yaml"))
    except Exception as e:
        log.warning(f"[AI Worker] chdir/bootstrap SHARK_CONFIG_PATH skipped: {e}")
    from src.core.config_manager import config_manager as _cm

    _cm.load_config()
    _dc = _cm.get_config().darwin
    _k = (_dc.llm_api_key or "").strip()
    log.warning(
        f"[AI Worker] config_path={_cm._config_path!r} "
        f"darwin.llm_provider={(_dc.llm_provider or '').strip()!r} "
        f"api_key_len={len(_k)} (expect >0 for real LLM)"
    )

    # Exchange data fetching in AI worker can use a new instance or REST
    analyzer = MarketAnalyzer(exchange=None, zmq_port=5555)
    try:
        asyncio.run(analyzer.start())
    except KeyboardInterrupt:
        pass

async def handle_ai_message(topic: str, data: dict):
    """Callback for ZMQ Subscriber to update local AIContext"""
    if topic == IPC_TOPIC_AI_SCORE:
        symbol = data.get("symbol")
        try:
            regime = MarketRegime(data.get("regime"))
        except ValueError:
            regime = MarketRegime.OSCILLATING
            
        ai_context.update(
            symbol,
            {
                "regime": regime,
                "matrix_regime": str(data.get("matrix_regime") or "STABLE").upper(),
                "score": float(data.get("score", 50.0)),
                "reason": data.get("reason", ""),
                "obi_5": float(data.get("obi_5", 0.0) or 0.0),
            },
        )
    elif topic == IPC_TOPIC_L1_TUNING:
        from src.core.l1_fast_loop import apply_l1_tuning

        apply_l1_tuning(data if isinstance(data, dict) else {})
    elif topic == IPC_TOPIC_L2_SYMBOLS:
        syms = (data or {}).get("symbols")
        if not isinstance(syms, list) or not syms:
            return
        clean = [str(s).strip() for s in syms if str(s).strip()]
        if not clean:
            return
        config_manager.config.strategy.symbols = clean
        log.info(f"[L2] Hot-updated strategy.symbols ({len(clean)} symbols)")
        ex = bot_context.get_exchange()
        if ex and hasattr(ex, "subscribe_market_data"):
            await ex.subscribe_market_data(clean)

async def run_bot(trading_ready: Optional[asyncio.Event] = None):
    """Runs the trading bot loop. Signals trading_ready after bot_context is registered (before WS connect)."""
    setup_logger()
    log.info("Starting Gate Attack Quant Bot V2.4 (Isolated AI Process)...")
    
    # 0. Load Config
    config = config_manager.get_config()
    from src.core.paper_engine import paper_engine as paper_engine_singleton

    paper_engine_singleton.apply_config_fees()

    # 1. License Check
    if not os.path.exists(config.license_path):
        log.critical(f"License file not found at {config.license_path}")

    if not os.path.exists("license/public.pem"):
        log.critical(f"Public key not found at license/public.pem")

    validator = LicenseValidator("license/public.pem", config.license_path)
    if os.path.exists(config.license_path):
        # Temporarily bypass license check for Sandbox Testing as requested
        # if not validator.validate():
        #     log.critical("License validation failed.")
        pass
    else:
        log.warning("License missing. Please upload via API.")

    # 2. Initialize Risk Control Engine
    log.info("Risk Control Engine Initialized.")

    # 3. Initialize Gateway and Strategy Engine
    api_key = config.exchange.api_key
    api_secret = config.exchange.api_secret
    testnet = config.exchange.sandbox_mode
    
    if not api_key or api_key == "YOUR_API_KEY":
        log.warning("No valid API Key found. Operating with limited capabilities.")

    state_machine = StateMachine(validator)
    
    exchange = GateFuturesGateway(
        api_key=api_key,
        api_secret=api_secret,
        on_tick=None,
        on_orderbook=None,
        testnet=testnet,
        use_paper_trading=True,
    )
    
    strategy_engine = StrategyEngine(exchange, state_machine)
    
    exchange.on_tick = strategy_engine.process_ws_tick
    exchange.on_orderbook = strategy_engine.process_ws_orderbook
    exchange.on_trade = strategy_engine.process_ws_trade
    
    bot_context.set_components(exchange, strategy_engine, state_machine)
    if trading_ready is not None:
        trading_ready.set()
        log.info("bot_context ready — safe to accept dashboard API/WebSocket traffic.")

    # 4. Start ZMQ Subscriber for AI Scores
    subscriber = ZMQSubscriber(port=5555, topics=list(IPC_SUBSCRIBE_DEFAULT_TOPICS))
    subscriber_task = asyncio.create_task(subscriber.start(handle_ai_message))

    # 5. Start Gateway WebSocket and Strategy Engine
    symbols = config.strategy.symbols
    if not symbols:
        symbols = ["BTC/USDT", "ETH/USDT"]

    await exchange.start_rest_session()
    await exchange.sync_usdt_futures_physics_matrix()
    await exchange.subscribe_market_data(symbols)

    # 避免前 10s periodic 更新前 risk_engine.current_balance=0 导致下单数量为 0
    try:
        bal_data = await exchange.fetch_balance()
        usdt = float(bal_data.get("total", {}).get("USDT", 0) or 0)
        if usdt > 0:
            risk_engine.update_balance(usdt)
            log.info(f"Risk engine seeded balance from exchange/paper: {usdt:.2f} USDT")
    except Exception as e:
        log.warning(f"Could not seed risk balance at startup: {e}")

    gateway_task = asyncio.create_task(exchange.start_ws())

    from src.core.volume_radar import run_volume_radar_loop
    from src.core.binance_leadlag import run_binance_leadlag_loop
    from src.core.gate_hot_universe import run_gate_hot_universe_loop

    radar_task = asyncio.create_task(run_volume_radar_loop(exchange))
    leadlag_task = asyncio.create_task(run_binance_leadlag_loop(exchange, strategy_engine))
    hot_universe_task = asyncio.create_task(run_gate_hot_universe_loop(exchange))

    try:
        await strategy_engine.start()
    except asyncio.CancelledError:
        log.info("Bot task cancelled.")
    finally:
        await strategy_engine.stop()
        await exchange.stop_ws()
        subscriber.stop()
        await subscriber_task
        radar_task.cancel()
        leadlag_task.cancel()
        hot_universe_task.cancel()
        try:
            await radar_task
        except asyncio.CancelledError:
            pass
        try:
            await leadlag_task
        except asyncio.CancelledError:
            pass
        try:
            await hot_universe_task
        except asyncio.CancelledError:
            pass
        await gateway_task

async def main():
    # Start AI Worker in a separate process
    ai_process = multiprocessing.Process(target=run_ai_worker, daemon=True)
    ai_process.start()

    # IMPORTANT: uvicorn must not bind until bot_context has the exchange, otherwise
    # /api/candles returns [] and ws_push_loop skips broadcast (dead dashboard in Docker).
    trading_ready = asyncio.Event()
    bot_task = asyncio.create_task(run_bot(trading_ready))
    try:
        await asyncio.wait_for(trading_ready.wait(), timeout=120.0)
    except asyncio.TimeoutError:
        log.critical("Trading stack did not become ready within 120s — check logs and Gate connectivity.")
        bot_task.cancel()
        try:
            await bot_task
        except asyncio.CancelledError:
            pass
        ai_process.terminate()
        ai_process.join()
        return

    log.info("Starting HTTP server on :8002 (trading stack already registered).")
    api_task = asyncio.create_task(start_api_server())

    try:
        await asyncio.gather(api_task, bot_task)
    except asyncio.CancelledError:
        log.info("Main tasks cancelled")
    finally:
        ai_process.terminate()
        ai_process.join()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
