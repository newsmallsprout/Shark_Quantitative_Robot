import asyncio
from src.core.config_manager import config_manager
from src.config import SystemState
from src.utils.logger import log
from src.strategy.core_strategy import CoreNeutralStrategy, CoreAttackStrategy
from src.strategy.user_loader import UserStrategyLoader

class StrategyEngine:
    def __init__(self, exchange, state_machine):
        self.exchange = exchange
        self.state_machine = state_machine
        self.running = False
        self.strategies = []
        self._load_strategies()

    def _load_strategies(self):
        # 1. Load Core Strategies
        self.strategies.append(CoreNeutralStrategy())
        self.strategies.append(CoreAttackStrategy())
        
        # 2. Load User Strategies
        user_loader = UserStrategyLoader()
        user_strategies = user_loader.load_strategies()
        self.strategies.extend(user_strategies)
        
        log.info(f"Strategy Engine loaded {len(self.strategies)} strategies.")

    async def start(self):
        self.running = True
        log.info("Strategy Engine Started")
        while self.running:
            try:
                await self.tick()
                await asyncio.sleep(1) # 1 second loop
            except Exception as e:
                log.error(f"Error in strategy loop: {e}")
                await asyncio.sleep(5)

    async def stop(self):
        self.running = False
        log.info("Strategy Engine Stopping...")

    async def tick(self):
        # Reload config if needed (or assume ConfigManager handles it)
        global_config = config_manager.get_config()
        
        # 1. Update Market Data & Balance
        # Loop through configured symbols
        for symbol in global_config.strategy.symbols:
            ticker = await self.exchange.fetch_ticker(symbol)
            balance = await self.exchange.fetch_balance()
            
            if not ticker:
                continue

            # 2. Update State Machine
            self.state_machine.update(balance, {symbol: ticker})
            current_state = self.state_machine.state
            
            # 3. Execute based on State
            if current_state == SystemState.LICENSE_LOCKED:
                await self.exchange.close_all_positions()
                return

            if current_state == SystemState.COOL_DOWN:
                return

            if current_state == SystemState.OBSERVE:
                pass # Just observe

            # Dispatch to strategies
            # Note: In a real system, StateMachine might dictate which strategy is active.
            # Here we let strategies decide or run all active ones.
            # For V2.2, we filter by 'active_strategies' in config.
            
            active_names = global_config.strategy.active_strategies
            
            for strategy in self.strategies:
                # Basic filter: check if strategy name is in active list
                # (User strategies might need a consistent naming convention or manual activation)
                # For now, we run all loaded user strategies + active core strategies
                is_core = strategy.name in ["CoreNeutral", "CoreAttack"]
                
                # Simplified logic: 
                # If it's a core strategy, check if it's enabled in config (mapped names)
                # If it's a user strategy, we assume it's enabled if loaded (or add config for it)
                
                should_run = True
                if strategy.name == "CoreNeutral" and "core_neutral" not in active_names:
                    should_run = False
                if strategy.name == "CoreAttack" and "core_attack" not in active_names:
                    should_run = False
                    
                if should_run:
                    # Strategies should respect system state internally or we filter here
                    # e.g. Neutral only runs in NEUTRAL state
                    if strategy.name == "CoreNeutral" and current_state != SystemState.NEUTRAL:
                        continue
                    if strategy.name == "CoreAttack" and current_state != SystemState.ATTACK:
                        continue
                        
                    await strategy.on_tick(self.exchange, symbol, ticker, balance)
