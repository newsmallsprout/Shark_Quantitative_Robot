import asyncio
import sys
import os
import uvicorn
from threading import Thread

from src.utils.logger import setup_logger, log
from src.core.config_manager import config_manager
from src.license_manager.validator import LicenseValidator
from src.exchange.gate_api import GateExchange, MockGateExchange
from src.core.state_machine import StateMachine
from src.strategy.engine import StrategyEngine
from src.api.server import app as api_app

def run_api_server():
    """Runs the FastAPI server in a separate thread (for now, or use asyncio later)"""
    # Note: uvicorn.run is blocking, so usually run in thread or separate process
    # But for asyncio app, we can run uvicorn as a task if we configure it right,
    # or simpler: just run in thread.
    uvicorn.run(api_app, host="0.0.0.0", port=8000, log_level="warning")

async def run_bot():
    """Runs the trading bot loop"""
    setup_logger()
    log.info("Starting Gate Attack Quant Bot V2.2...")
    
    # 0. Load Config
    config = config_manager.get_config()

    # 1. License Check
    if not os.path.exists(config.license_path):
        log.critical(f"License file not found at {config.license_path}")
        # In V2.2, maybe we wait for upload via API?
        # For now, just warn and maybe continue in RESTRICTED mode or exit.
        # Let's keep strict check for now.
        # return

    if not os.path.exists("license/public.pem"):
        log.critical(f"Public key not found at license/public.pem")
        return

    validator = LicenseValidator("license/public.pem", config.license_path)
    # Validate if file exists
    if os.path.exists(config.license_path):
        if not validator.validate():
            log.critical("License validation failed.")
            return
    else:
        log.warning("License missing. Please upload via API.")

    # 2. Initialize Components
    # Use MockExchange if no API keys
    if not config.exchange.api_key:
        log.warning("No API Key found. Using MOCK Exchange.")
        exchange = MockGateExchange()
    else:
        exchange = GateExchange()

    await exchange.initialize()
    
    state_machine = StateMachine(validator)
    strategy_engine = StrategyEngine(exchange, state_machine)

    # 3. Run
    try:
        await strategy_engine.start()
    except asyncio.CancelledError:
        log.info("Bot task cancelled.")
    finally:
        await strategy_engine.stop()
        await exchange.close()

async def main():
    # Run API server in a separate thread to avoid blocking asyncio loop of bot
    # (Uvicorn can run in asyncio, but 'Server.serve' is complex to setup manually)
    # Simpler approach for this script: Thread for API
    api_thread = Thread(target=run_api_server, daemon=True)
    api_thread.start()
    
    log.info("API Server started at http://localhost:8000/docs")
    
    # Run Bot
    await run_bot()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
