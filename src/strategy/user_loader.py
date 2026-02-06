import importlib.util
import os
import sys
from typing import List
from src.strategy.base import BaseStrategy
from src.utils.logger import log

class UserStrategyLoader:
    def __init__(self, strategy_dir: str = "strategies/user_custom"):
        self.strategy_dir = strategy_dir
        if not os.path.exists(self.strategy_dir):
            os.makedirs(self.strategy_dir)

    def load_strategies(self) -> List[BaseStrategy]:
        strategies = []
        for filename in os.listdir(self.strategy_dir):
            if filename.endswith(".py") and not filename.startswith("__"):
                path = os.path.join(self.strategy_dir, filename)
                try:
                    strategy_class = self._load_strategy_from_file(path)
                    if strategy_class:
                        strategy_instance = strategy_class()
                        strategies.append(strategy_instance)
                        log.info(f"Loaded user strategy: {strategy_instance.name}")
                except Exception as e:
                    log.error(f"Failed to load user strategy from {filename}: {e}")
        return strategies

    def _load_strategy_from_file(self, path: str):
        module_name = os.path.basename(path).replace(".py", "")
        spec = importlib.util.spec_from_file_location(module_name, path)
        if spec and spec.loader:
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)
            
            # Find class that inherits from BaseStrategy
            for attr_name in dir(module):
                attr = getattr(module, attr_name)
                if isinstance(attr, type) and issubclass(attr, BaseStrategy) and attr is not BaseStrategy:
                    return attr
        return None
