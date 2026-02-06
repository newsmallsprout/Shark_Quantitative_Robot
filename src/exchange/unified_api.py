import ccxt.async_support as ccxt
import asyncio
from src.core.config_manager import config_manager
from src.utils.logger import log
import statistics

class UnifiedExchange:
    def __init__(self):
        self.config = config_manager.get_config().exchange
        self.execution_exchange_id = self.config.execution_exchange
        self.data_source_ids = self.config.data_sources
        
        self.execution_exchange = None
        self.data_exchanges = {}
        self.market_data = {}
        self.auth_failed = False

    async def initialize(self):
        # 1. Initialize Execution Exchange
        await self._init_execution_exchange()

        # 2. Initialize Data Exchanges (Read-only)
        for ex_id in self.data_source_ids:
            try:
                log.info(f"Initializing Data Source: {ex_id}")
                ex_class = getattr(ccxt, ex_id)
                # No keys needed for public data usually, but limits apply
                exchange = ex_class({
                    'enableRateLimit': True,
                    'options': {
                        'defaultType': 'future', 
                    }
                })
                await exchange.load_markets()
                self.data_exchanges[ex_id] = exchange
            except Exception as e:
                log.warning(f"Failed to initialize data source {ex_id}: {e}")

    async def _init_execution_exchange(self):
        # Refresh config
        self.config = config_manager.get_config().exchange
        self.execution_exchange_id = self.config.execution_exchange
        
        log.info(f"Initializing Execution Exchange: {self.execution_exchange_id}")
        exec_class = getattr(ccxt, self.execution_exchange_id)
        
        # Check if keys are present
        api_key = self.config.api_key
        api_secret = self.config.api_secret
        
        if not api_key or not api_secret or api_key == "YOUR_API_KEY":
            log.warning("No valid API Key found. Execution Exchange will be limited.")
            # Still initialize but maybe expect errors if we try to trade
        
        self.execution_exchange = exec_class({
            'apiKey': api_key,
            'secret': api_secret,
            'enableRateLimit': True,
            'options': {
                'defaultType': 'future', 
            }
        })
        if self.config.sandbox_mode:
            self.execution_exchange.set_sandbox_mode(True)
            
        try:
            await self.execution_exchange.load_markets()
            log.info("Execution Exchange Loaded.")
            # Reset auth failure flag on successful re-init attempt
            self.auth_failed = False 
        except Exception as e:
            log.error(f"Failed to load execution exchange: {e}")

    async def reload_config(self):
        """Reload configuration and re-initialize execution exchange"""
        log.info("Reloading Exchange Configuration...")
        if self.execution_exchange:
            await self.execution_exchange.close()
        
        self.auth_failed = False # Reset熔断
        await self._init_execution_exchange()
        
        # Test connection immediately
        try:
            log.info("Testing connection with new configuration...")
            # Call directly on the exchange object to bypass the safety wrapper and get the real exception
            await self.execution_exchange.fetch_balance()
            log.info("Connection test PASSED.")
        except Exception as e:
            log.error(f"Connection test FAILED: {str(e)}")
            self.auth_failed = True # Set fail state so loop doesn't spam
            raise e
            
        log.info("Exchange Configuration Reloaded.")

    async def close(self):
        if self.execution_exchange:
            await self.execution_exchange.close()
        for ex in self.data_exchanges.values():
            await ex.close()

    async def fetch_balance(self):
        # We try to fetch balance regardless of auth_failed state initially to allow recovery
        # But to prevent spam, we can check a separate flag or just rely on try-except
        
        try:
            return await self.execution_exchange.fetch_balance()
        except ccxt.AuthenticationError as e:
            if not self.auth_failed:
                log.warning(f"Authentication failed: {str(e)}. Switching to Read-Only Mode.")
                self.auth_failed = True
            # Re-raise if this is a test connection (no auth_failed set yet or just reset)
            # Actually fetch_balance is called by strategy loop too.
            # To support test connection raising error, we can check if called from test context?
            # Or simpler: The test_connection in reload_config calls fetch_balance. 
            # If we swallow exception here, reload_config won't see it.
            # BUT reload_config reset auth_failed to False.
            # So if we hit here, we log warning and set auth_failed=True.
            # We should probably re-raise if it's the FIRST failure so upper layers know?
            # No, strategy loop crashes if we raise.
            
            # Correction: reload_config should call a raw method or we modify this to re-raise if needed.
            # Let's add a 'test' parameter? No, API signature change.
            # Let's just return empty dict here as before for safety.
            # The reload_config will rely on the fact that if this returns empty dict (or log warning), it might not trigger exception catch.
            # WAIT: reload_config calls fetch_balance(). If fetch_balance swallows exception, reload_config sees success (empty dict).
            # This is BAD for the user request "tell me what is wrong".
            
            # Fix: We need to allow exception propagation specifically when testing.
            # Since we cannot easily change signature, let's use a flag or check if we are in 'test' mode?
            # Or better: reload_config should call execution_exchange.fetch_balance() DIRECTLY, bypassing this safe wrapper.
            return {'total': {'USDT': 0.0}, 'free': {'USDT': 0.0}}
        except Exception as e:
            if not self.auth_failed:
                log.error(f"Error fetching balance: {e}")
            return {'total': {'USDT': 0.0}, 'free': {'USDT': 0.0}}

    async def fetch_ticker(self, symbol):
        # Fetch from all data sources
        tickers = []
        tasks = []

        for ex_id, exchange in self.data_exchanges.items():
            # Map symbol if necessary (simplified here, assuming standard symbols like BTC/USDT)
            # Some exchanges might have different formats (BTC-USDT, etc), CCXT handles most but not all unified.
            tasks.append(self._safe_fetch_ticker(exchange, ex_id, symbol))
        
        results = await asyncio.gather(*tasks)
        valid_tickers = [t for t in results if t is not None]
        
        if not valid_tickers:
            log.warning(f"No ticker data found for {symbol}")
            return None
        
        # Aggregate Logic: Average Price
        prices = [t['last'] for t in valid_tickers]
        avg_price = statistics.mean(prices)
        
        # Construct a synthetic ticker
        # We use volume from the primary execution exchange if available, or average
        base_vol = statistics.mean([t['baseVolume'] for t in valid_tickers if t.get('baseVolume')])
        
        aggregated_ticker = {
            'symbol': symbol,
            'last': avg_price,
            'baseVolume': base_vol,
            'timestamp': valid_tickers[0]['timestamp'],
            'info': {'sources': len(valid_tickers)}
        }
        
        self.market_data[symbol] = aggregated_ticker
        return aggregated_ticker

    async def _safe_fetch_ticker(self, exchange, ex_id, symbol):
        try:
            # Simple symbol mapping fix for common issues if needed
            # For now rely on CCXT unified symbols
            return await exchange.fetch_ticker(symbol)
        except Exception as e:
            # log.debug(f"Failed to fetch {symbol} from {ex_id}: {e}")
            return None

    async def fetch_tickers(self, symbols):
        """
        Fetch multiple tickers from all data sources and aggregate them.
        """
        if not self.data_exchanges:
            log.warning("No data exchanges available for fetch_tickers")
            return {}

        tasks = []

        for ex_id, exchange in self.data_exchanges.items():
            tasks.append(self._safe_fetch_tickers(exchange, ex_id, symbols))
        
        results = await asyncio.gather(*tasks)
        # results is a list of dicts: [{symbol: ticker, ...}, {symbol: ticker, ...}]
        
        # Aggregate
        aggregated_tickers = {}
        
        for symbol in symbols:
            valid_tickers = []
            for source_result in results:
                if source_result and symbol in source_result:
                    valid_tickers.append(source_result[symbol])
            
            if not valid_tickers:
                # log.debug(f"No tickers found for {symbol} from any source")
                continue
                
            # Aggregate Logic: Average Price
            prices = [t['last'] for t in valid_tickers if t.get('last')]
            if not prices:
                continue
                
            avg_price = statistics.mean(prices)
            
            # Base Volume Average
            vols = [t['baseVolume'] for t in valid_tickers if t.get('baseVolume')]
            base_vol = statistics.mean(vols) if vols else 0
            
            # Quote Volume Average
            quote_vols = [t['quoteVolume'] for t in valid_tickers if t.get('quoteVolume')]
            quote_vol = statistics.mean(quote_vols) if quote_vols else 0
            
            # Change Percentage Average
            changes = [t['percentage'] for t in valid_tickers if t.get('percentage') is not None]
            avg_change = statistics.mean(changes) if changes else 0

            aggregated_tickers[symbol] = {
                'symbol': symbol,
                'last': avg_price,
                'baseVolume': base_vol,
                'quoteVolume': quote_vol,
                'percentage': avg_change,
                'timestamp': valid_tickers[0]['timestamp'],
                'info': {'sources': len(valid_tickers)}
            }
            
        return aggregated_tickers

    async def _safe_fetch_tickers(self, exchange, ex_id, symbols):
        try:
            # log.debug(f"Fetching tickers from {ex_id}...")
            if exchange.has['fetchTickers']:
                return await exchange.fetch_tickers(symbols)
            else:
                # Fallback to fetching one by one if fetchTickers not supported
                # This is slower but compatible
                results = {}
                for symbol in symbols:
                    ticker = await exchange.fetch_ticker(symbol)
                    results[symbol] = ticker
                return results
        except Exception as e:
            log.warning(f"Failed to fetch tickers from {ex_id}: {e}")
            return None

    async def create_order(self, symbol, type, side, amount, price=None, params={}):
        if self.auth_failed:
            return None
            
        try:
            return await self.execution_exchange.create_order(symbol, type, side, amount, price, params)
        except ccxt.AuthenticationError:
            if not self.auth_failed:
                log.error("Cannot create order: Invalid API Key.")
                self.auth_failed = True
            return None
        except Exception as e:
            log.error(f"Error creating order: {e}")
            return None

    async def fetch_positions(self):
        if self.auth_failed:
             return []

        try:
            return await self.execution_exchange.fetch_positions()
        except ccxt.AuthenticationError:
            if not self.auth_failed:
                log.warning("Authentication failed during fetch_positions. Switching to Read-Only Mode.")
                self.auth_failed = True
            return []
        except Exception as e:
            log.error(f"Error fetching positions: {e}")
            return []

    async def fetch_open_orders(self):
        if self.auth_failed:
             return []

        try:
            return await self.execution_exchange.fetch_open_orders()
        except ccxt.AuthenticationError:
            if not self.auth_failed:
                log.warning("Authentication failed during fetch_open_orders. Switching to Read-Only Mode.")
                self.auth_failed = True
            return []
        except Exception as e:
            log.error(f"Error fetching open orders: {e}")
            return []

    async def close_all_positions(self):
        # Implementation depends on exchange specifics. 
        # Typically fetch positions then market close them.
        try:
            # Gate.io specific or unified way
            # CCXT unified fetchPositions is not supported by all, but gate supports it
            positions = await self.execution_exchange.fetch_positions()
            for pos in positions:
                if pos['contracts'] > 0: # Long
                    await self.create_order(pos['symbol'], 'market', 'sell', pos['contracts'])
                elif pos['contracts'] < 0: # Short
                    await self.create_order(pos['symbol'], 'market', 'buy', abs(pos['contracts']))
        except ccxt.AuthenticationError:
            log.warning("Cannot close positions: Invalid API Key.")
        except Exception as e:
            log.error(f"Error closing positions: {e}")
