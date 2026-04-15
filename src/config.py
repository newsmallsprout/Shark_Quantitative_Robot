from enum import Enum
from pydantic import BaseModel
from typing import List, Optional

class SystemState(str, Enum):
    OBSERVE = "OBSERVE"
    NEUTRAL = "NEUTRAL"
    ATTACK = "ATTACK"
    BERSERKER = "BERSERKER"
    COOL_DOWN = "COOL_DOWN"
    LICENSE_LOCKED = "LICENSE_LOCKED"

class RiskConfig(BaseModel):
    max_single_risk: float = 0.02      # 2%
    max_structure_risk: float = 0.05   # 5%
    daily_drawdown_limit: float = 0.08 # 8%
    hard_drawdown_limit: float = 0.20  # 20%
    
class StrategyConfig(BaseModel):
    symbols: List[str] = ["BTC/USDT", "ETH/USDT"] # Default list
    leverage_neutral: int = 20
    leverage_attack: int = 3
    position_neutral: float = 0.02
    position_attack: float = 0.10

class AppConfig:
    LICENSE_FILE = "license/license.key"
    PUBLIC_KEY_FILE = "license/public.pem" # For client verification
    REDIS_URL = "redis://localhost:6379/0"
    
    # Gate.io API (Load from env or encrypted config)
    GATE_API_KEY = "YOUR_API_KEY" 
    GATE_API_SECRET = "YOUR_API_SECRET"
    
    RISK = RiskConfig()
    STRATEGY = StrategyConfig()

config = AppConfig()
