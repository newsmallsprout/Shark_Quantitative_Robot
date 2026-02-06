from abc import ABC, abstractmethod
from typing import Dict, Any

class BaseStrategy(ABC):
    def __init__(self, name: str, config: Dict[str, Any] = None):
        self.name = name
        self.config = config or {}

    @abstractmethod
    async def on_tick(self, exchange, symbol: str, ticker: dict, balance: float):
        """
        Called on every market tick.
        """
        pass

    async def log(self, message: str):
        from src.utils.logger import log
        log.info(f"[{self.name}] {message}")
