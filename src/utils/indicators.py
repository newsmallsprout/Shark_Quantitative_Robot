import collections

class RollingWindow:
    def __init__(self, window_size=14):
        self.window_size = window_size
        self.prices = collections.deque(maxlen=window_size + 1)

    def add(self, price):
        self.prices.append(price)

    def rsi(self):
        if len(self.prices) < self.window_size + 1:
            return 50.0 # Default neutral
        
        gains = 0.0
        losses = 0.0
        
        for i in range(1, len(self.prices)):
            change = self.prices[i] - self.prices[i-1]
            if change > 0:
                gains += change
            else:
                losses -= change
                
        if losses == 0:
            return 100.0
        
        rs = gains / losses
        return 100.0 - (100.0 / (1.0 + rs))

# Global store for indicators (Symbol -> Indicator)
# In a real app, this should be managed per strategy instance
rsi_store = {}

def get_rsi(symbol: str, price: float, period: int = 14) -> float:
    if symbol not in rsi_store:
        rsi_store[symbol] = RollingWindow(period)
    
    rsi_store[symbol].add(price)
    return rsi_store[symbol].rsi()
