"""在线学习器 — Stub（Go 进化引擎负责主循环）。
多维奖励与回撤分解见 evolution.reward_signal.compute_reward_breakdown。
"""

class OnlineLearner:
    def __init__(self):
        self.weights = {}
    
    def predict(self, features):
        return 0.5

class FeatureExtractor:
    @staticmethod
    def extract(trade_history):
        return {}

def compute_reward(pnl, win_rate):
    """旧接口占位；多维奖励请用 compute_reward_breakdown(trade_rows)。"""
    return pnl
