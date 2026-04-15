from abc import ABC, abstractmethod
from typing import Dict, Any, List
from src.core.events import TickEvent, SignalEvent

class BaseStrategy(ABC):
    def __init__(self, name: str, config: Dict[str, Any] = None):
        self.name = name
        self.config = config or {}
        self.events_queue: List[SignalEvent] = []

    @abstractmethod
    async def on_tick(self, event: TickEvent):
        """
        Called on every market tick.
        Process the tick event and generate signals.
        """
        pass

    def emit_signal(self, signal: SignalEvent):
        """
        Emit a trading signal to the execution engine.
        """
        self.events_queue.append(signal)

    async def log(self, message: str):
        from src.utils.logger import log
        log.info(f"[{self.name}] {message}")
