from src.strategy.base import BaseStrategy
from src.core.events import TickEvent


class MyCustomStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("MyCustomStrategy")

    async def on_tick(self, event: TickEvent) -> None:
        # Example: if event.ticker.get("last", 0) > 50000:
        #     await self.log("Price is high!")
        pass
