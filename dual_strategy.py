"""Shark 2.0 双轨策略 — 主流币波段 + 山寨币短线

单线模式（避免主流/山寨双线并行抢仓、计划冲突）：
  SHARK_TRADING_TRACK=dual     默认：主流 + 动态山寨池
  SHARK_TRADING_TRACK=stable  仅主流（BTC/ETH/SOL），不刷新山寨池、不写本地山寨进攻计划
  SHARK_TRADING_TRACK=volatile 仅动态高波动山寨池，不订阅主流行情（已有主流仓仍可按持仓逻辑管理）
"""
import os

# ═══════════════════════════════════════════════
# 币种分类
# ═══════════════════════════════════════════════
STABLE_COINS = {"BTC/USDT", "ETH/USDT", "SOL/USDT"}  # 主流币：日线波段

_RAW_TRACK = os.environ.get("SHARK_TRADING_TRACK", "dual").strip().lower()
TRADING_TRACK = _RAW_TRACK if _RAW_TRACK in ("dual", "stable", "volatile") else "dual"
# 单线时该桶可用上限与主进程 MAX_TOTAL_EXPOSURE 对齐（避免 dual_strategy 依赖 main）
_CAPITAL_SINGLE = 0.95


def trading_track() -> str:
    return TRADING_TRACK


def trading_track_allows_open(symbol: str) -> bool:
    """当前轨道是否允许对该币对开新仓（持仓管理不受影响）。"""
    if TRADING_TRACK == "dual":
        return True
    if TRADING_TRACK == "stable":
        return is_stable(symbol)
    return not is_stable(symbol)

# 高波动山寨池由 Gate.io 热门波动接口动态刷新，不再写死名单。
HIGH_VOL_ALTS = set()
_DYNAMIC_HIGH_VOL_ALTS = set()

def set_dynamic_high_vol_alts(symbols):
    """Refresh runtime alt pool from exchange discovery."""
    global _DYNAMIC_HIGH_VOL_ALTS
    _DYNAMIC_HIGH_VOL_ALTS = {
        str(s) for s in symbols
        if str(s).endswith("/USDT") and str(s) not in STABLE_COINS
    }
    return set(_DYNAMIC_HIGH_VOL_ALTS)

# ═══════════════════════════════════════════════
# 主流币策略（BTC/ETH）：日线趋势 + 金字塔
# ═══════════════════════════════════════════════
STABLE_CONFIG = {
    "style": "swing_heavy",
    "hold_profile": "swing",
    "disable_aggressive_entry": True,
    "margin_pct": 0.28,         # 主流币改为中长线重仓
    "min_plan_margin_pct": 0.10, # RangePlan 保证金地板：重仓不能开成蚊子仓
    "max_plan_margin_pct": 0.24,
    "min_leverage": 20,
    "max_leverage": 35,
    "max_positions": 3,        # BTC + ETH + SOL 三仓都允许，靠 60% 资金桶分配
    "min_volume": 500000,      # 最低成交量 50万（确保流动性）
    # Gate 24h 涨跌幅为百分数；0 表示不因波动过小拒单（避免 BTC/ETH 横盘不入池）
    "min_change": 0.0,
    "max_change": 15,          # 最大涨跌幅 15%
    
    # 止损/止盈全部由行情+ATR实时计算
    "pyramid_levels": 4,
    "pyramid_margin_pct": 0.5,
    
    "trail_trigger": 14.0,     # 中长线：盈利更大才追踪
    "trail_pct": 0.45,         # 回撤更宽，避免趋势中途洗掉
    "tp_atr_mult": 8.0,
    "breakeven_trigger": 12.0,
    
    "cooldown": 10,
}

# ═══════════════════════════════════════════════
# 波动币策略（高波动山寨）：短线爆发
# ═══════════════════════════════════════════════
VOLATILE_CONFIG = {
    "style": "alt_scalp",
    "hold_profile": "scalp",
    "disable_aggressive_entry": False,
    "margin_pct": 0.02,         # 保证金 2% 余额（山寨起步）
    "min_plan_margin_pct": 0.02, # 山寨也保持有效仓位，不做无意义小单
    "min_leverage": 15,
    "max_leverage": 70,
    "leverage": 30,
    "max_positions": 0,        # 不限制山寨数量，完全由资金池控制
    "min_volume": 1000000,     # 最低成交量 100万（放宽，更多机会）
    "min_change": 1.5,         # 涨跌幅至少 1.5%（放宽，不卡太死）
    "max_change": 25,          # 最大涨跌幅 25%
    
    # 止损/止盈全部由行情+ATR实时计算
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
    "stable": 0.60,    # 60% 主攻主流币中长线
    "volatile": 0.40,  # 40% 博高波动山寨
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
    """是否是动态高波动山寨池成员"""
    return symbol in _DYNAMIC_HIGH_VOL_ALTS

def get_capital_limit(balance: float, symbol: str) -> float:
    """该币种可用的最大资金（单线模式下另一轨为 0，避免双桶并行）。"""
    if TRADING_TRACK == "stable":
        return balance * _CAPITAL_SINGLE if is_stable(symbol) else 0.0
    if TRADING_TRACK == "volatile":
        return balance * _CAPITAL_SINGLE if not is_stable(symbol) else 0.0
    if is_stable(symbol):
        return balance * CAPITAL_SPLIT["stable"]
    return balance * CAPITAL_SPLIT["volatile"]
