import ccxt.async_support as ccxt
import asyncio
from src.config import config
from src.utils.logger import log

class GateExchange:
    def __init__(self):
        self.exchange = ccxt.gateio({
            'apiKey': config.GATE_API_KEY,
            'secret': config.GATE_API_SECRET,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future',  # Contract trading
            }
        })
        self.market_data = {}

    async def initialize(self):
        try:
            # Check connection (or just load markets)
            await self.exchange.load_markets()
            log.info("Gate.io markets loaded successfully.")
        except Exception as e:
            log.error(f"Failed to connect to Gate.io: {e}")
            # In a real scenario, might want to raise error or retry
            
    async def close(self):
        await self.exchange.close()

    async def fetch_balance(self):
        try:
            balance = await self.exchange.fetch_balance()
            return balance['total'].get('USDT', 0.0)
        except Exception as e:
            log.error(f"Error fetching balance: {e}")
            return 0.0

    async def fetch_ticker(self, symbol):
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            self.market_data[symbol] = ticker
            return ticker
        except Exception as e:
            log.error(f"Error fetching ticker for {symbol}: {e}")
            return None

    async def create_order(self, symbol, type, side, amount, price=None, params={}):
        try:
            order = await self.exchange.create_order(symbol, type, side, amount, price, params)
            log.info(f"Order created: {side} {amount} {symbol} @ {price}")
            return order
        except Exception as e:
            log.error(f"Order failed: {e}")
            return None

    async def close_all_positions(self):
        log.warning("Closing all positions...")
        # Implementation depends on exchange specifics. 
        # Typically fetch positions then market close them.
        try:
            positions = await self.exchange.fetch_positions()
            for pos in positions:
                if pos['contracts'] > 0: # Long
                    await self.create_order(pos['symbol'], 'market', 'sell', pos['contracts'])
                elif pos['contracts'] < 0: # Short
                    await self.create_order(pos['symbol'], 'market', 'buy', abs(pos['contracts']))
        except Exception as e:
            log.error(f"Error closing positions: {e}")

# Simple Mock for testing without API Keys
class MockGateExchange(GateExchange):
    def __init__(self):
        self.balance = 10000.0
        self.market_data = {}
        
    async def initialize(self):
        log.info("Mock Exchange Initialized")
        
    async def fetch_balance(self):
        return self.balance
        
    async def fetch_ticker(self, symbol):
        try:
            ticker = await self.exchange.fetch_ticker(symbol)
            self.market_data[symbol] = ticker
            return ticker
        except Exception as e:
            log.error(f"Error fetching ticker for {symbol}: {e}")
            return None

    async def create_order(self, symbol, type, side, amount, price=None, params={}):
        log.info(f"MOCK ORDER: {side} {amount} {symbol}")
        return {'id': 'mock_id', 'status': 'closed'}

    async def close_all_positions(self):
        log.info("MOCK: Closing all positions")
