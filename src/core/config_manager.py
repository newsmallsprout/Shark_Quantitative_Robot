import json
import os
from pydantic import BaseModel, Field
from typing import List, Optional, Dict
from src.utils.logger import log

class RiskConfig(BaseModel):
    max_single_risk: float = 0.02
    max_structure_risk: float = 0.05
    daily_drawdown_limit: float = 0.08
    hard_drawdown_limit: float = 0.15
    max_leverage: int = 10

class ExchangeConfig(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    sandbox_mode: bool = False

class StrategyParams(BaseModel):
    neutral_rsi_buy: int = 30
    neutral_rsi_sell: int = 70
    neutral_ai_threshold: int = 40
    attack_ai_threshold: int = 60
    
class StrategyConfig(BaseModel):
    active_strategies: List[str] = ["core_neutral", "core_attack"]
    symbols: List[str] = ["BTC/USDT", "ETH/USDT"]
    allocations: Dict[str, float] = {"core_neutral": 0.5, "core_attack": 0.5}
    params: StrategyParams = StrategyParams()

class GlobalConfig(BaseModel):
    exchange: ExchangeConfig = ExchangeConfig()
    risk: RiskConfig = RiskConfig()
    strategy: StrategyConfig = StrategyConfig()
    license_path: str = "license/license.key"

class ConfigManager:
    _instance = None
    _config_path = "user_data/settings.json"
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance.config = GlobalConfig()
            cls._instance.load_config()
        return cls._instance

    def load_config(self):
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, 'r') as f:
                    data = json.load(f)
                    self.config = GlobalConfig(**data)
                log.info(f"Configuration loaded from {self._config_path}")
            except Exception as e:
                log.error(f"Failed to load config: {e}. Using defaults.")
        else:
            log.warning("Config file not found. Using defaults.")
            self.save_config()

    def save_config(self):
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, 'w') as f:
                f.write(self.config.model_dump_json(indent=2))
            log.info("Configuration saved.")
        except Exception as e:
            log.error(f"Failed to save config: {e}")

    def update_exchange_config(self, api_key: str, api_secret: str):
        self.config.exchange.api_key = api_key
        self.config.exchange.api_secret = api_secret
        self.save_config()

    def update_risk_config(self, **kwargs):
        updated = False
        for k, v in kwargs.items():
            if hasattr(self.config.risk, k):
                setattr(self.config.risk, k, v)
                updated = True
        if updated:
            self.save_config()

    def update_strategy_config(self, **kwargs):
        updated = False
        # Handle top-level strategy config
        for k, v in kwargs.items():
            if k == 'params':
                 # Handle nested params
                 for pk, pv in v.items():
                     if hasattr(self.config.strategy.params, pk):
                         setattr(self.config.strategy.params, pk, pv)
                         updated = True
            elif hasattr(self.config.strategy, k):
                setattr(self.config.strategy, k, v)
                updated = True
        if updated:
            self.save_config()

    def get_config(self) -> GlobalConfig:
        return self.config

# Global instance
config_manager = ConfigManager()
