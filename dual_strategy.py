"""Shark 2.0 双轨策略 — 主流币波段 + 山寨币短线"""

# ═══════════════════════════════════════════════
# 币种分类
# ═══════════════════════════════════════════════
STABLE_COINS = {"BTC/USDT", "ETH/USDT"}  # 主流币：日线波段

# 高波动山寨精选（只做这些，小刀赚微利）
HIGH_VOL_ALTS = {
    "DOGE/USDT", "SOL/USDT", "PEPE/USDT", "XRP/USDT",
    "SUI/USDT", "APT/USDT", "NEAR/USDT", "ARB/USDT",
    "OP/USDT", "WIF/USDT", "BONK/USDT", "TON/USDT",
}

# ═══════════════════════════════════════════════
# 主流币策略（BTC/ETH）：日线趋势 + 金字塔
# ═══════════════════════════════════════════════
STABLE_CONFIG = {
    "margin_pct": 0.03,        # 保证金 3% 余额（动态比例+波动衰减）
    "max_positions": 2,        # 只做 BTC 和 ETH
    "min_volume": 500000,      # 最低成交量 50万（确保流动性）
    # Gate 24h 涨跌幅为百分数；0 表示不因波动过小拒单（避免 BTC/ETH 横盘不入池）
    "min_change": 0.0,
    "max_change": 15,          # 最大涨跌幅 15%
    
    "stop_loss_base": -15.0,   # 宽止损 -15%
    "stop_loss_max": -80.0,    # 最大 -80%
    
    "pyramid_levels": 4,
    "pyramid_margin_pct": 0.5,
    
    "trail_trigger": 8.0,      # 盈利 8% 才追踪
    "trail_pct": 0.25,         # 回撤 25%
    
    "cooldown": 10,
}

# ═══════════════════════════════════════════════
# 波动币策略（高波动山寨）：短线爆发
# ═══════════════════════════════════════════════
VOLATILE_CONFIG = {
    "margin_pct": 0.002,       # 保证金 0.2% 余额（动态比例+波动衰减）
    "max_positions": 3,        # 最多 3 个山寨仓位
    "min_volume": 3000000,     # 最低成交量 300万（只要活跃币）
    "min_change": 3.0,         # 涨跌幅至少 3%（高波动物件）
    "max_change": 25,          # 最大涨跌幅 25%
    
    # 止损：极紧（山寨不扛单）
    "stop_loss_base": -2.5,    # 硬止损 -2.5%
    "stop_loss_max": -6.0,     # 最大 -6%
    
    # 禁止补仓
    "pyramid_levels": 0,       # 波动币绝对不加仓
    
    # 移动止盈：紧贴
    "trail_trigger": 3.0,      # 盈利 3% 启动追踪
    "trail_pct": 0.2,          # 回撤 20% 平
    
    # 第一目标止盈 50%
    "tp1_target": 5.0,         # +5% 平 50%
    
    # 冷却
    "cooldown": 15,            # 冷却 15s
}

# ═══════════════════════════════════════════════
# 资金分配
# ═══════════════════════════════════════════════
CAPITAL_SPLIT = {
    "stable": 0.70,    # 70% 主攻主流币
    "volatile": 0.30,  # 30% 博山寨
}

# ═══════════════════════════════════════════════
# 获取币种对应的策略配置
# ═══════════════════════════════════════════════
def get_config(symbol: str) -> dict:
    if symbol in STABLE_COINS:
        return STABLE_CONFIG
    return VOLATILE_CONFIG

def is_stable(symbol: str) -> bool:
    return symbol in STABLE_COINS

def is_high_vol_alt(symbol: str) -> bool:
    """是否是高波动山寨精选"""
    return symbol in HIGH_VOL_ALTS

def get_capital_limit(balance: float, symbol: str) -> float:
    """该币种可用的最大资金"""
    if is_stable(symbol):
        return balance * CAPITAL_SPLIT["stable"]
    return balance * CAPITAL_SPLIT["volatile"]
