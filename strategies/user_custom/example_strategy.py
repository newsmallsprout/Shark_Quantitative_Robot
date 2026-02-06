from src.strategy.base import BaseStrategy

class MyCustomStrategy(BaseStrategy):
    def __init__(self):
        super().__init__("MyCustomStrategy")

    async def on_tick(self, exchange, symbol: str, ticker: dict, balance: float):
        # Example logic
        # if ticker['last'] > 50000:
        #     await self.log("Price is high!")
        pass
