import csv
import os
import time
from typing import Dict

class DataCollector:
    def __init__(self, data_dir="data"):
        self.data_dir = data_dir
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        self.file_path = os.path.join(data_dir, "training_data.csv")
        self._ensure_header()

    def _ensure_header(self):
        if not os.path.exists(self.file_path):
            with open(self.file_path, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow(["timestamp", "symbol", "price", "volume", "regime", "signal_score", "action", "result_pnl"])

    def log_execution(self, symbol: str, ticker: Dict, regime: str, score: float, action: str, pnl: float = 0.0):
        with open(self.file_path, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                int(time.time()),
                symbol,
                ticker.get('last'),
                ticker.get('baseVolume'),
                regime,
                score,
                action,
                pnl
            ])

data_collector = DataCollector()
