class GlobalContext:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(GlobalContext, cls).__new__(cls)
            cls._instance.exchange = None
            cls._instance.strategy_engine = None
            cls._instance.state_machine = None
        return cls._instance

    def set_components(self, exchange, strategy_engine, state_machine):
        self.exchange = exchange
        self.strategy_engine = strategy_engine
        self.state_machine = state_machine

    def get_exchange(self):
        return self.exchange

    def get_strategy_engine(self):
        return self.strategy_engine

    def get_state_machine(self):
        return self.state_machine

bot_context = GlobalContext()
