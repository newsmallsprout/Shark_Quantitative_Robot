import asyncio
import sys
import os
import uvicorn
from uvicorn import Config, Server

from src.utils.logger import setup_logger, log
from src.core.config_manager import config_manager
from src.license_manager.validator import LicenseValidator
from src.exchange.unified_api import UnifiedExchange
from src.exchange.gate_api import MockGateExchange
from src.core.state_machine import StateMachine
from src.strategy.engine import StrategyEngine
from src.api.server import app as api_app
from src.core.globals import bot_context

async def start_api_server():
    """Runs the FastAPI server as an async task in the same loop"""
    config = Config(app=api_app, host="0.0.0.0", port=8002, log_level="warning")
    server = Server(config)
    log.info("API Server starting at http://localhost:8002")
    await server.serve()

async def run_bot():
    """Runs the trading bot loop"""
    setup_logger()
    log.info("Starting Gate Attack Quant Bot V2.2...")
    
    # 0. Load Config
    config = config_manager.get_config()

    # 1. License Check
    if not os.path.exists(config.license_path):
        log.critical(f"License file not found at {config.license_path}")

    if not os.path.exists("license/public.pem"):
        log.critical(f"Public key not found at license/public.pem")
        # return # Allow to continue for now to show UI

    validator = LicenseValidator("license/public.pem", config.license_path)
    # Validate if file exists
    if os.path.exists(config.license_path):
        if not validator.validate():
            log.critical("License validation failed.")
            # return
    else:
        log.warning("License missing. Please upload via API.")

    # 2. Initialize Components
    # Use MockExchange if no API keys
    # Check if key is placeholder or empty
    api_key = config.exchange.api_key
    if not api_key or api_key == "YOUR_API_KEY":
        log.warning("No valid API Key found (detected default or empty). Using UnifiedExchange in Read-Only Mode (Mock Execution).")
        # We still use UnifiedExchange but execution will fail gracefully or we can inject Mock
        # For now, let UnifiedExchange handle auth errors gracefully as implemented
        exchange = UnifiedExchange()
    else:
        log.info("Initializing Unified Exchange System...")
        exchange = UnifiedExchange()

    await exchange.initialize()
    
    state_machine = StateMachine(validator)
    strategy_engine = StrategyEngine(exchange, state_machine)

    # Register components to global context for API access
    bot_context.set_components(exchange, strategy_engine, state_machine)

    # 3. Run Strategy Engine
    try:
        await strategy_engine.start()
    except asyncio.CancelledError:
        log.info("Bot task cancelled.")
    finally:
        await strategy_engine.stop()
        await exchange.close()

async def main():
    # Run both API and Bot in the same event loop
    api_task = asyncio.create_task(start_api_server())
    bot_task = asyncio.create_task(run_bot())
    
    try:
        await asyncio.gather(api_task, bot_task)
    except asyncio.CancelledError:
        log.info("Main tasks cancelled")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
