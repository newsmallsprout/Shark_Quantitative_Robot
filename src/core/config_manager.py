import json
import os
import yaml
from pathlib import Path
from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional, Dict, Any
from src.utils.logger import log


def _resolve_settings_yaml_path() -> str:
    """
    优先顺序：
    1) SHARK_CONFIG_PATH
    2) 与本文件相对的项目根 config/settings.yaml（最可靠，避免 cwd 下另有同名文件误加载成「空 key / mock」）
    3) 当前工作目录 config/settings.yaml
    """
    env = (os.environ.get("SHARK_CONFIG_PATH") or "").strip()
    if env and os.path.isfile(env):
        return env
    try:
        root = Path(__file__).resolve().parents[2]
        cand = root / "config" / "settings.yaml"
        if cand.is_file():
            return str(cand)
        model = root / "config" / "settings.model.yaml"
        if model.is_file():
            log.info(f"Using template config {model} (copy to config/settings.yaml for local secrets)")
            return str(model)
    except Exception:
        pass
    rel = "config/settings.yaml"
    if os.path.isfile(rel):
        return os.path.abspath(rel)
    return rel


def _sanitize_loaded_config_data(data: Any) -> Any:
    """
    Best-effort compatibility cleanup before Pydantic validation.
    Prevent a single malformed field from invalidating the whole YAML.
    """
    if not isinstance(data, dict):
        return data

    risk = data.get("risk")
    if isinstance(risk, dict):
        # Historic/operator typo: users sometimes put a drawdown ratio into
        # `drawdown_halt_trading`, but this field is boolean.
        dht = risk.get("drawdown_halt_trading")
        if isinstance(dht, (int, float)) and not isinstance(dht, bool):
            log.warning(
                f"risk.drawdown_halt_trading expects bool, got numeric {float(dht):.6f}; coercing to False."
            )
            risk["drawdown_halt_trading"] = False
        elif isinstance(dht, str):
            v = dht.strip().lower()
            if v in {"true", "1", "yes", "on"}:
                risk["drawdown_halt_trading"] = True
            elif v in {"false", "0", "no", "off", ""}:
                risk["drawdown_halt_trading"] = False
    return data

class RiskConfig(BaseModel):
    # False：单笔上限仅用 max_single_risk；True：按权益分档（见 equity_sizing.margin_cap_fraction）
    use_equity_tier_margin: bool = True
    max_single_risk: float = 0.05
    max_structure_risk: float = 0.05
    daily_drawdown_limit: float = 0.08
    hard_drawdown_limit: float = 0.15
    # 全账户每秒下单上限（非「每日每币笔数」）；剥头皮可调高，由 Darwin patches.risk 维护
    max_orders_per_second: int = 500
    # Daily Grinder (NEUTRAL / ATTACK): Kelly + ATR smoothed in this band
    grinder_leverage_min: float = 10.0
    grinder_leverage_max: float = 75.0
    # NOTE: `max_leverage` is no longer used as a hard reject for normal orders.
    # It is kept as a "system-wide ceiling" for UI / legacy components.
    max_leverage: int = 200
    # HFT realism: risk should be controlled by exposure (margin/notional), not by leverage alone.
    # If margin <= this absolute USDT cap, allow the order through (after exchange min_notional checks).
    max_margin_per_trade_usdt: float = 5000.0
    # Absolute cap on notional per trade; if exceeded, RiskEngine compresses margin/amount but keeps leverage.
    max_notional_per_trade_usdt: float = 500000.0
    # 10 分钟看似横盘但短窗 ATR 已偏高时，强制上限（避免高倍 + 窄止损连环扫）
    grinder_choppy_atr_cap_leverage: int = 35
    berserker_obi_threshold: float = 0.85
    # 幽灵持仓：SNIPER_LEADLAG silo 单独占用单笔风险与杠杆上限
    sniper_leadlag_max_single_risk: float = 0.02
    sniper_leadlag_max_leverage: int = 15
    # True：触及日/硬回撤上限时进入 COOL_DOWN 并暂停策略引擎；False：仅打日志（订单仍可能被 risk_engine 停机拦截）
    drawdown_cool_down_enabled: bool = True
    # COOL_DOWN 持续秒数；0 表示下一 tick 即尝试回到 OBSERVE（若回撤仍超标会再次触发）
    drawdown_cool_down_sec: float = 3600.0
    # False：触及日/硬回撤时不设置 risk_engine.is_halted（仍打日志；与 drawdown_cool_down_enabled 独立）
    drawdown_halt_trading: bool = True

class ExchangeConfig(BaseModel):
    api_key: str = ""
    api_secret: str = ""
    sandbox_mode: bool = False
    data_sources: List[str] = ["gateio", "binance", "okx"]
    execution_exchange: str = "gateio"


class ExecutionConfig(BaseModel):
    """
    执行层灰度：关闭则 StrategyEngine 仍直连 exchange.create_order（逃生舱）。
    开启则经 OrderManager（意图→网关、TTL 撤单、对账）。
    """

    use_order_manager: bool = False
    # 限价单默认 TTL（毫秒）；0 表示不自动撤单（仍可对账）
    default_order_ttl_ms: int = 0
    # 纸面影子限价：单次价格/盘口更新最多吃掉挂单剩余量的比例（撮合排队吞吐）
    shadow_fill_fraction_per_tick: float = 0.18
    # --- 狙击手过滤 / 双通道（与 StrategyEngine + OrderIntent 对齐）---
    sniper_win_rate_floor: float = 0.6
    high_conviction_win_rate_floor: float = 0.85
    sniper_normal_ttl_ms: int = 15000
    sniper_atr_sl_mult: float = 1.0
    sniper_atr_tp_mult: float = 2.0
    # 高置信度贪婪单：权益 ROE（浮盈/占用保证金）达此比例后，激活按价回撤止损
    high_conviction_trailing_activation_pct: float = 0.02
    high_conviction_trailing_callback_pct: float = 0.005
    # --- Kelly + ATR 动态名义/杠杆（狙击手管线内当 ai_win_rate 存在时生效）---
    max_account_risk_per_trade_pct: float = 0.02
    kelly_fraction: float = 0.25
    max_allowed_leverage: int = 200
    # 爆仓缓冲：要求 1/L > SL_Distance_Pct * liquidation_safety_mult（默认 1.2）
    liquidation_safety_mult: float = 1.2
    # 高置信度通道对凯利乘子的额外放大
    kelly_high_conviction_mult: float = 1.5
    # 凯利乘子上限（防止名义膨胀过度）
    max_kelly_notional_mult: float = 3.0
    # 高置信度追踪：开仓后至少 N 秒内不允许仅因 trailing 市价平仓（排除开盘噪声 / 手数过小导致 ROE 爆炸）
    high_conviction_trailing_min_hold_sec: float = 2.0
    # ROE 达阈值并武装 trailing 后，再过 N 秒才允许按价回撤触发平仓（防止武装当根 K 立刻被洗）
    trailing_callback_grace_after_arm_sec: float = 1.0
    # Predator 净利润 Chandelier / 不败金身：仅当「毛利 − 开+平预估 Taker 费」> 该值才允许止盈类平仓（0=必须严格为正）
    predator_chandelier_min_net_usdt: float = 0.0
    # Chandelier：当前净利须 ≥ 峰值净利 × 该比例才不平（0.8=最多回吐 20% 峰值，「止损线」宽松）
    predator_chandelier_retain_frac: float = 0.8
    # Predator 止盈/地板平仓用盘口限价（reduce_only），避免纯市价吃单
    predator_exit_limit_order: bool = True


class PaperEngineConfig(BaseModel):
    """
    纸面撮合审计可调参数（见 paper_engine 模块头注释）。
    - fallback_slippage_bps：仅当本地无完整 orderbook 时相对 last 的偏移；主流币有 L2 时不走此分支。
    - taker_extra_bps：在真实盘口 VWAP 之后再叠加的冲击，默认 0 表示不人为加悲观滑点。
    - default_contract_size：无 Gate quanto 缓存时，每张合约对应标的数量（默认 1 = 把张数当币数）。
    - initial_balance_usdt：全局 paper_engine 单例起始权益（USDT）；单测里 new PaperTradingEngine(...) 仍可用自定义金额。
    """

    initial_balance_usdt: float = 100.0
    fallback_slippage_bps: float = 2.0
    taker_extra_bps: float = 0.0
    default_contract_size: float = 1.0
    # 交易所口径：手续费 = 名义 × 费率；与杠杆无关。Maker 可为负（返佣）。
    taker_fee_rate: float = 0.0005
    maker_fee_rate: float = 0.0002
    # True：下单前按 Gate 同步的合约张数/杠杆/风控阶梯做预检（须网关已 sync_usdt_futures_physics_matrix）
    enforce_exchange_physics: bool = False
    # True：新开仓须在 entry_context 带止盈/止损限价；成交后挂 OCO（单测 conftest 会关）
    require_entry_tp_sl_limits: bool = False
    # 盈利化债：平仓实现正净利后，用其中一部分去市价减仓当前最亏腿（见 paper_engine）
    pnl_wash_enabled: bool = True
    # 微利多单实现净利 ≥ 该值时触发化债（与 leg 微利阈值配合，默认与 beta_hf 目标一致偏 1.5U 量级）
    pnl_wash_min_credit_usdt: float = 1.5
    # True：仅当 entry_context 带 beta_leg_micro_take 且多为多单赢利时，优先啃空单（按 ROE 最烂优先）
    pnl_wash_prefer_short_bags_on_long_micro: bool = True
    # True：化债候选腿须带 beta_neutral_hf 标记
    pnl_wash_only_beta_hf_legs: bool = True
    # 逐仓纸面：权益（margin_used+unrealized）触及维持保证金附近则强制市价全平，避免「无限浮亏」拖累全账户
    isolated_hard_liquidation_enabled: bool = True
    isolated_liquidation_maintenance_buffer: float = 1.05  # equity <= maintenance × 该系数 → 强平
    # AI 下发 dynamic_max_loss_margin_pct：浮亏超「保证金×比例」先斩；False 则仅走上面维持带强平
    ai_dynamic_margin_loss_cap_enabled: bool = True

class StrategyParams(BaseModel):
    neutral_rsi_buy: int = 30
    neutral_rsi_sell: int = 70
    neutral_ai_threshold: int = 40
    # 震荡均值回归：要求相对近窗极值已小幅反转，减少高波动里「接飞刀 / 摸顶」反向单
    neutral_micro_confirm_enabled: bool = True
    neutral_micro_window_ticks: int = 10
    neutral_micro_bounce_bps: float = 6.0
    neutral_second_confirm_enabled: bool = True
    neutral_second_confirm_bps: float = 10.0
    neutral_max_obi_against: float = 0.10
    # CoreNeutral：近窗单边跑动 ≥ run_bps 视为强趋势，AI 阈值下调 relax_points（不低于 floor）
    neutral_ai_trend_relax_enabled: bool = True
    neutral_ai_trend_run_bps: float = 28.0
    neutral_ai_trend_relax_points: float = 12.0
    neutral_ai_trend_relax_floor: float = 38.0
    # 近窗单边跑动超过此 bps 则禁止均值回归开仓（避免强趋势里摸顶/抄底）；0=关闭
    neutral_block_if_window_run_bps: float = 0.0
    attack_ai_threshold: int = 60
    # Min seconds between CoreAttack (non-berserker) signals per symbol
    attack_signal_cooldown_sec: float = 25.0
    # CoreNeutral 同品种最小发单间隔（秒），减轻震荡市里过度交易
    neutral_signal_cooldown_sec: float = 25.0
    # 强攻：价格须不显著逆势于短期均价；OBI 不过分反向，减少扫单后反手被打
    attack_momentum_confirm_enabled: bool = True
    attack_sma_fast_ticks: int = 12
    attack_sma_align_max_adverse_bps: float = 12.0
    attack_max_obi_against: float = 0.28
    attack_scene_priority_floor: float = 8.0
    attack_scene_escape_ai_score: float = 78.0
    attack_scene_escape_obi_min: float = 0.10
    # CoreAttack：相对 60-tick 慢均线，价在趋势侧超出 bps 则禁止反手；0=关闭
    attack_slow_sma_trend_guard_bps: float = 0.0
    # Scales how much funding / carry factors influence attack scoring (Darwin-tunable)
    funding_signal_weight: float = 1.0
    # CoreNeutral / CoreAttack 括号：True=按权益净利 USDT 反推限价（与狂鲨 100U→1U/0.5U 同比例）；False=仅用 bps
    core_use_equity_net_brackets: bool = True
    core_tp_net_equity_fraction: float = 0.01
    core_sl_net_equity_fraction: float = 0.005
    # CoreNeutral / CoreAttack 信号参考价上推导限价止盈、止损（bps）；仅当 core_use_equity_net_brackets=false 时作主路径
    core_entry_tp_bps: float = 55.0
    core_entry_sl_bps: float = 50.0
    core_entry_limit_enabled: bool = True
    core_entry_limit_offset_bps: float = 1.0
    core_entry_limit_ttl_ms: int = 3000
    core_entry_limit_requote_max: int = 4
    core_margin_floor_usdt: float = 6.0
    core_margin_floor_cap_fraction: float = 0.60
    # 超高波动：按 symbol ATR% 放宽括号止损、抬高止盈 RR，避免 50~60bps 止损被噪音扫光
    core_high_atr_threshold: float = 0.01
    core_atr_sl_widen_mult: float = 1.35
    core_atr_sl_from_atr_frac: float = 1.08
    core_high_atr_sl_cap_bps: float = 240.0
    core_high_atr_tp_min_rr: float = 1.35
    core_high_atr_tp_cap_bps: float = 400.0
    # 高 ATR + 按权益 USDT 括号时：放大净利目标/止损预算（减少插针秒止损）
    core_high_atr_net_tp_mult: float = 1.28
    core_high_atr_net_sl_mult: float = 1.55
    # 高 ATR 品种压低 grinder 杠杆，减少插针爆仓式止损
    core_high_atr_max_leverage: int = 20
    core_breakeven_arm_r: float = 0.40
    core_breakeven_fee_buffer_bps: float = 6.0
    # 强攻：高波动时 Predator 触线阈值抬高，减少无效反手
    attack_high_atr_score_padding: float = 12.0

class StrategyConfig(BaseModel):
    active_strategies: List[str] = ["core_neutral", "core_attack"]
    # 单合约最多一笔净头寸（与纸面会话一致）
    single_open_per_symbol: bool = True
    # 自动 NEUTRAL/ATTACK 切换只观察锚定合约，避免多标的 tick 上 regime 冲突导致反复全平
    regime_switch_anchor_symbol: str = "BTC/USDT"
    # True 时切换模式会先 close_all（极易误伤；默认关）
    regime_switch_close_positions: bool = False
    symbols: List[str] = [
        "BTC/USDT",
        "ETH/USDT",
        "SOL/USDT",
        "DOGE/USDT",
        "PEPE/USDT",
        "XRP/USDT",
        "BNB/USDT",
        "AVAX/USDT",
        "LINK/USDT",
        "ADA/USDT",
        "SUI/USDT",
        "WIF/USDT",
        "ORDI/USDT",
        "APT/USDT",
        "NEAR/USDT",
        "DOT/USDT",
        "LTC/USDT",
        "TRX/USDT",
        "FIL/USDT",
    ]
    allocations: Dict[str, float] = {"core_neutral": 0.5, "core_attack": 0.5}
    params: StrategyParams = StrategyParams()


class BetaNeutralHFConfig(BaseModel):
    """
    高市值 ALT vs 锚（BTC）残差信号 + 双腿同时进场；
    进场后两腿解绑：单腿「毛利 − 预估市价平仓费」超过阈值则微利平仓，
    并可选瞬时同向同量市价重载，维持双向敞口在场。
    """

    model_config = ConfigDict(extra="ignore")
    enabled: bool = True
    anchor_symbol: str = "BTC/USDT"
    symbols: List[str] = Field(
        default_factory=lambda: [
            "ETH/USDT",
            "SOL/USDT",
            "DOGE/USDT",
            "XRP/USDT",
            "BNB/USDT",
            "ADA/USDT",
            "LINK/USDT",
            "AVAX/USDT",
            "LTC/USDT",
            "BCH/USDT",
            "TRX/USDT",
            "DOT/USDT",
            "NEAR/USDT",
            "APT/USDT",
            "FIL/USDT",
            "ETC/USDT",
            "UNI/USDT",
            "ATOM/USDT",
            "ARB/USDT",
            "OP/USDT",
        ]
    )
    lookback_ticks: int = 120
    min_points: int = 45
    entry_zscore: float = 2.0
    exit_zscore: float = 0.45
    stop_zscore: float = 3.5
    rearm_zscore: float = 0.75
    min_correlation: float = 0.65
    max_beta_abs: float = 3.0
    min_spread_std_bps: float = 0.35
    min_impulse_bps: float = 0.8
    impulse_zscore_mult: float = 0.8
    cross_section_min_edge_bps: float = 0.6
    cross_section_top_k: int = 4
    cross_section_lookback: int = 8
    # 双雷达：山寨波动 ≫ BTC 且资金/单币 Z 极端 → 脱钩；切 OBI 动量、禁 BTC 均值回归与截面锚回退
    dual_radar_enabled: bool = True
    decouple_vol_ratio_vs_btc: float = 3.0
    decouple_funding_rate_abs: float = 0.0005
    decouple_zscore_abs: float = 4.0
    decouple_momentum_obi_abs: float = 0.28
    decoupled_margin_loss_cap: float = 0.4
    pair_leverage: int = 8
    pair_margin_usdt: float = 6.0
    # 单 pair 双腿合计初始保证金 ≤ min(available/max_active_pairs, equity × 本比例)
    max_pair_equity_fraction: float = 0.05
    max_hold_sec: float = 45.0
    cooldown_sec: float = 0.0
    hedge_grace_sec: float = 2.0
    hedge_rebalance_min_interval_sec: float = 0.8
    min_profit_usdt: float = 0.05
    min_profit_bps: float = 3.0
    max_loss_usdt: float = 0.25
    max_loss_bps: float = 12.0
    slippage_bps_per_leg: float = 1.5
    requote_penalty_bps: float = 0.8
    funding_interval_sec: float = 28800.0
    entry_order_type: str = "limit"
    entry_limit_ttl_ms: int = 1500
    entry_limit_requote_max: int = 3
    exit_order_type: str = "limit"
    candidate_limit_ui: int = 6
    closed_history_limit: int = 12
    max_active_pairs: int = 50
    # 按 symbols 顺序每 N 个 ALT 为一组；仅当同组内全部满足开仓条件时，在同一 tick 内连续发单（避免「先开后开」时间差）
    entry_group_size: int = Field(default=2, ge=1, le=32)
    # symbol_adjacent=配置顺序相邻 N 个为一组（严）；score_batch=每 tick 从高分候选里凑 N 个同发（易填满多仓）
    entry_group_mode: str = "score_batch"
    entry_score_batch_max_waves: int = Field(default=10, ge=1, le=64)
    # True=组内 EV 预检须全过才开仓（严）；False=只开通过预检的腿，组内仍共用 lev_g
    entry_group_require_all_feasible: bool = False
    # 放松模式下至少几条通过才触发本组（通常 1）
    entry_group_min_feasible: int = Field(default=1, ge=1, le=32)
    # exchange_group_min_max：组内统一杠杆 = 各 ALT 与锚的交易所 leverage_max 的最小值；单开取 min(该 ALT, 锚)。dynamic=旧版按信号/AI 缩放，组内取各腿有效杠杆的最小值
    entry_leverage_mode: str = "exchange_group_min_max"
    # 开仓 EV：期望 TP(USDT) 需 > 圆桌双边费 × 该倍数（代码层再强制 floor≥1.2）
    entry_ev_round_trip_mult: float = 1.2
    # 独立腿：毛利 − 单次市价平仓预估费 > 该值(USDT) 则微利平仓并可瞬时重载（宜小，避免「有赚不平」）
    leg_micro_take_usdt: float = 0.06
    # 圆桌微利：净利 = 毛 unrealized − 开仓Taker费(按entry) − 平仓Taker费(按现价)；仅当净利 > max(下值, frac*(两费之和)) 才平仓
    leg_micro_dynamic_floor_usdt: float = 0.15
    leg_micro_fee_surplus_fraction: float = 0.5
    # 同一标的在同一 Unix 分钟内最多「收割+续杯」次数（对齐 1m K 内刷单上限）
    leg_micro_max_reload_per_1m_bar: int = Field(default=2, ge=0, le=32)
    # 微利触发后该腿最短冷却（秒），与 reload_cooldown_sec 取大
    leg_micro_live_cooldown_sec: float = 10.0
    # 微利平仓后立刻同品种同向同量市价再进场（永动机续杯）
    instant_reload_enabled: bool = True
    # 同一腿连续重载的最短间隔，避免信号队列堆积重复触发
    reload_cooldown_sec: float = 0.35
    # matrix_regime=TRENDING_* 时顺势腿 ROE 追踪（paper_engine Predator：武装 ROE 阈值，如 0.004=0.4%）
    trend_trailing_activation_roe: float = 0.004
    trend_trailing_callback_roe: float = 0.004
    # 顺势奔跑：同武装 ROE；净利回撤由 execution.predator_chandelier_retain_frac 控制
    ride_trailing_activation_roe: float = 0.004
    ride_trailing_price_callback_frac: float = 0.15  # 已废弃：狼群仅用利润回撤，保留字段兼容
    # 锚（BTC）大级别趋势过滤：STRONG_UP 禁山寨做空；STRONG_DOWN 禁山寨做多；STABLE 双向
    trend_filter_enabled: bool = True
    trend_filter_fast_minutes: int = 15
    trend_filter_slow_minutes: int = 60
    trend_filter_strong_separation_bps: float = 28.0
    trend_filter_min_minutes: int = 65  # 分钟收样本不足则 STABLE
    # 单车种 1m 微观趋势：逆势不加空/不加多；续杯同样尊重
    micro_trend_filter_enabled: bool = True
    micro_trend_ema_minutes: int = 20
    micro_trend_lookback_minutes: int = 15
    micro_trend_min_bars: int = 24
    micro_trend_bps_confirm: float = 8.0
    # 策略 inventory：做空腿数 − 做多腿数 ≥ 该值 → 禁止新开/续空；对称禁止开多
    directional_imbalance_cap: int = 3
    # 激进市价 Taker 前：前 5 档 OBI 必须同向极端（无盘口时不挡单，避免停机）
    obi_taker_gate_enabled: bool = True
    obi_taker_min_abs: float = 0.28
    obi_taker_top_levels: int = 5
    # 资金费「养单」：持仓吃贴够厚时跳过自动减仓化债（仍尊重强平）
    funding_preserve_skip_wash_enabled: bool = True
    funding_preserve_min_abs_rate: float = 0.00025  # 每期费率绝对值（Gate: 正=多付空）
    # 微利平仓：Post-Only 限价挂在动态 TP 附近（Maker）；即时「续杯」默认关闭以免裸露
    leg_micro_maker_exit_enabled: bool = False
    leg_micro_maker_exit_offset_bps: float = 0.0
    leg_micro_maker_exit_skip_instant_reload: bool = True
    # 成交流「毒性」门控（与 OBI 并列）：需 futures.trades WS 已由网关 ingest
    trade_flow_gate_enabled: bool = False
    trade_flow_window_sec: float = 10.0
    trade_flow_min_abs: float = 0.22
    trade_flow_min_abs_contracts: float = 12.0
    # 目标波动率缩放：pair_margin 参考 × vol(ATR) / vol_target
    vol_scaled_sizing_enabled: bool = False
    vol_target_micro_atr_bps: float = 12.0
    vol_scale_min: float = 0.35
    vol_scale_max: float = 1.85

class DarwinSymbolPatch(BaseModel):
    model_config = ConfigDict(extra="ignore")
    berserker_obi_threshold: Optional[float] = None
    max_leverage: Optional[int] = None


class DarwinConfig(BaseModel):
    """Darwin Protocol: trade autopsies → researcher → optional config patches."""
    enabled: bool = True
    autopilot: bool = True
    apply_llm_patches: bool = False
    log_autopsies: bool = True
    autopsy_dir: str = "data/darwin/autopsies"
    macro_context_path: str = "src/darwin/macro_context.default.md"
    researcher_hook_url: str = ""
    llm_provider: str = "mock"
    # 盘面分析 / Darwin 共用；也可不设此项，改用环境变量 DARWIN_LLM_API_KEY（或 openai→OPENAI_API_KEY、deepseek→DEEPSEEK_API_KEY）
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model_name: str = ""
    symbol_patches: Dict[str, DarwinSymbolPatch] = Field(default_factory=dict)
    # 时间序经验库（JSONL）；Researcher / L3 提示词会注入尾部若干行
    experience_log_path: str = "data/darwin/experience.jsonl"
    experience_tail_lines: int = 80
    learn_on_order_open: bool = True
    # persist_only: 只写战报；per_trade: 每平一笔即 LLM（费 token）；batch: 满 batch_size 笔一批 L3
    reflection_mode: str = "batch"
    batch_size: int = 50


class PredatorMatrixWeights(BaseModel):
    ai: float = 0.4
    tech: float = 0.3
    obi: float = 0.3


def _default_predator_regime_weights() -> Dict[str, PredatorMatrixWeights]:
    return {
        "DEFAULT": PredatorMatrixWeights(ai=0.4, tech=0.3, obi=0.3),
        "OSCILLATING": PredatorMatrixWeights(ai=0.35, tech=0.40, obi=0.25),
        "TRENDING_UP": PredatorMatrixWeights(ai=0.45, tech=0.35, obi=0.20),
        "TRENDING_DOWN": PredatorMatrixWeights(ai=0.45, tech=0.35, obi=0.20),
        "CHAOTIC": PredatorMatrixWeights(ai=0.15, tech=0.15, obi=0.70),
    }


class PredatorMatrixConfig(BaseModel):
    """机构级开仓：成交量分布 / 流动性扫 / 费率×OBI / 压缩突破 + 贝叶斯(盘面)动态权重。"""
    enabled: bool = True
    ohlcv_interval: str = "15m"
    ohlcv_limit: int = 200
    ohlcv_refresh_sec: float = 45.0
    vp_bins: int = 32
    vp_value_area_pct: float = 0.70
    vp_tech_weight: float = 0.55
    liquidity_swing_lookback: int = 12
    liquidity_obi_floor_long: float = -0.35
    liquidity_obi_ceiling_short: float = 0.35
    liquidity_boost_points: float = 14.0
    funding_neg_extreme: float = -0.0003
    funding_pos_extreme: float = 0.0003
    funding_obi_min_align: float = 0.12
    funding_boost_points: float = 18.0
    squeeze_bb_period: int = 20
    squeeze_bbw_percentile: float = 0.05
    squeeze_volume_mult: float = 1.2
    squeeze_donchian: int = 15
    squeeze_bbw_history: int = 120
    squeeze_boost_points: float = 16.0
    regime_weights: Dict[str, PredatorMatrixWeights] = Field(default_factory=_default_predator_regime_weights)


class ExitManagementConfig(BaseModel):
    """
    四维出场：ATR 止损 / Chandelier 追踪 / OBI 抢先平仓 / 时间止损。
    当前执行器接在纸面引擎持仓上；实盘需同步仓位状态后可复用同一逻辑。
    """
    enabled: bool = True
    atr_period: int = 14
    atr_sl_multiplier: float = 1.5
    atr_trailing_multiplier: float = 1.5
    # 浮盈（价格向有利方向移动）达到该倍数 × ATR 后，将止损抬/压至保本线
    breakeven_r_multiple: float = 1.0
    breakeven_fee_buffer_bps: float = 10.0
    obi_preemptive_threshold: float = 0.8
    obi_preemptive_hold_sec: float = 3.0
    time_stop_sec: float = 900.0
    # 若现价与开仓价距离 < 该比例 × ATR，视为「横盘在成本附近」
    time_stop_atr_fraction: float = 0.25
    candle_interval: str = "1m"
    # Chandelier / OBI / 时间衰减 等「软出场」：要求预估净利 ≥ 名义×bps（覆盖双边 taker 后仍有余）
    soft_exit_net_floor_enabled: bool = True
    soft_exit_min_net_bps: float = 2.0
    # 保本线上移时额外计入双边 taker 费率（避免「假保本」实亏）
    breakeven_roundtrip_taker: bool = True
    # Chandelier 追踪止盈触发时：限价在 active_sl 成交并按 Maker 费率（须与 paper_engine 同步 Gate 合约费率）
    chandelier_exit_limit_maker: bool = True


class MicroMakerSchemeConfig(BaseModel):
    """方案一：双边 Post-Only 挂单吃价差（震荡市）。"""
    enabled: bool = False
    min_spread_bps: float = 2.0
    quote_notional_usd: float = 45.0
    throttle_sec: float = 0.35
    leverage: int = 5
    require_regime_oscillating: bool = True


class LiquidationSnipeSchemeConfig(BaseModel):
    """方案二：插针/爆仓带后的均值回归埋伏（平时空仓）。"""
    enabled: bool = False
    min_range_pct: float = 0.035
    max_body_to_range_ratio: float = 0.42
    bid_depth_vacuum_ratio: float = 0.3
    limit_price_offset_bps: float = 6.0
    cooldown_sec: float = 120.0
    ohlcv_interval: str = "1m"
    ohlcv_limit: int = 40
    fetch_throttle_sec: float = 18.0


class FundingSqueezeSchemeConfig(BaseModel):
    """方案三：极端负费率 + 轧空博弈（埋伏多）。"""
    enabled: bool = False
    funding_rate_below: float = -0.0008
    min_obi: float = 0.06
    limit_offset_bps: float = 8.0
    cooldown_sec: float = 300.0


class InstitutionalSchemesConfig(BaseModel):
    micro_maker: MicroMakerSchemeConfig = MicroMakerSchemeConfig()
    liquidation_snipe: LiquidationSnipeSchemeConfig = LiquidationSnipeSchemeConfig()
    funding_squeeze: FundingSqueezeSchemeConfig = FundingSqueezeSchemeConfig()


class L1FastLoopConfig(BaseModel):
    """L1 狙击层：trades CVD + 1m ATR + OBI，Taker 进 / Maker 止盈 / CVD 止损。"""
    enabled: bool = False
    min_atr_bps: float = 10.0
    cvd_burst_mult: float = 2.8
    cvd_stop_mult: float = 2.2
    max_obi_opposition_long: float = -0.38
    tp_bps: float = 30.0
    trade_notional_usd: float = 100.0
    leverage: int = 10
    signal_cooldown_sec: float = 1.2
    require_attack_mode: bool = False
    # Bracket Execution Protocol：开仓后立刻挂 Maker 止盈 + 模拟止损市价腿，OCO 互撤；覆盖手续费后微利
    bracket_protocol: bool = True
    bracket_taker_fee_bps: float = 5.0
    bracket_net_target_bps: float = 10.0
    bracket_sl_floor_bps: float = 20.0
    bracket_sl_atr_mult: float = 0.65
    bracket_tp_decay_sec: float = 300.0
    bracket_tp_decay_bps: float = 6.0


class GateHotUniverseConfig(BaseModel):
    """
    Gate USDT 永续全市场 ticker：按成交额×波动×资金费热度排序，定时覆盖 strategy.symbols 并 WS 订阅。
    与 L2 指挥不同：更短周期、更小成交额门槛，专抓 RAVE 类高波动热门合约（仍须满足 min_quote_vol）。
    """

    enabled: bool = False
    refresh_sec: float = 90.0
    top_n: int = 10
    symbols_cap: int = 12
    min_quote_vol_24h: float = 80_000.0
    anchor_symbols: List[str] = Field(default_factory=lambda: ["BTC/USDT"])
    # 评分：score = vq * (1 + min(|chg|/divisor, cap)) * (1 + min(|fr|*fscale, fcap))；divisor 越小越偏波动
    change_pct_divisor: float = 12.0
    change_score_cap: float = 5.0
    funding_score_scale: float = 800.0
    funding_score_cap_mult: float = 1.5
    # 24h 高低振幅(bps)加成：score *= (1 + w * min(hl_bps/div, cap))；0=关闭
    hl_vol_weight: float = 0.85
    hl_vol_bps_divisor: float = 180.0
    hl_vol_score_cap: float = 2.2
    # True 时不再因「beta_neutral 占用 symbols」而拒绝刷新，而是覆盖 beta_neutral_hf.symbols（山寨列表，不含锚）
    apply_to_beta_neutral_hf: bool = False
    beta_neutral_hf_alt_cap: int = 20


class L2CommandConfig(BaseModel):
    """L2 指挥层：定时全市场扫描 + 可选 LLM 下发 L1 运行时参数（ZMQ）。"""
    enabled: bool = False
    interval_sec: float = 900.0
    universe_top_n: int = 48
    symbols_cap: int = 16
    min_quote_volume_24h: float = 2.0e6
    anchor_symbols: List[str] = Field(
        default_factory=lambda: ["BTC/USDT", "ETH/USDT"]
    )
    persist_symbols_to_yaml: bool = False
    publish_symbols_zmq: bool = True
    use_llm_for_l1_tuning: bool = True
    rules_l1_tuning: bool = True


class AssassinMicroConfig(BaseModel):
    """
    微观游击刺客：60s 滚动成交 VWAP 偏离 + 吃单耗竭；Taker 反手；OCO；平仓后冷静期。
    cost-aware：Hurdle=双费+价差；回归空间 > Hurdle×mult；止盈=Entry×(1±(Hurdle+目标净利))。
    """

    enabled: bool = False
    vwap_window_sec: float = 60.0
    deviation_bps: float = 20.0
    deviation_atr_mult: float = 0.0
    exhaustion_burst_sec: float = 3.0
    exhaustion_baseline_sec: float = 10.0
    exhaustion_ratio: float = 0.1
    min_baseline_taker_vol: float = 1e-6
    cooldown_sec: float = 0.0
    tp_path_fraction: float = 0.8
    sl_bps: float = 25.0
    min_tp_bps: float = 5.0
    trade_notional_usd: float = 60.0
    leverage: int = 10
    signal_cooldown_sec: float = 0.0
    require_attack_mode: bool = False
    # --- 成本感知（净利润保底）---
    use_cost_aware: bool = True
    target_net_frac: float = 5e-4
    net_space_hurdle_mult: float = 2.5


class VolumeRadarConfig(BaseModel):
    """
    广域成交量雷达：REST 全市场 ticker + 5m 量速 vs 24h 均档；Hurdle 点差闸；动态追加 WS 深度/逐笔。
    战术 A（CVD+OBI 点火）/B（爆仓网格）与 LLM 叙事核实为后续策略插件，此处只做发现与订阅。
    """

    enabled: bool = False
    poll_interval_sec: float = 45.0
    velocity_ratio_threshold: float = 10.0
    min_quote_vol_24h: float = 200_000.0
    min_history_span_sec: float = 180.0
    min_change_pct_abs: float = 2.0
    max_hurdle_frac: float = 0.0015
    prey_cooldown_sec: float = 120.0
    max_prey_extra_subs: int = 8
    max_prey_list_ui: int = 12
    auto_subscribe_ws: bool = True
    narrative_llm_enabled: bool = False


class BinanceLeadLagConfig(BaseModel):
    """
    币安 !ticker@arr 天眼 → Gate IOC 吃单 + Maker 止盈（纸面/实盘标签经 client_oid/text）。
    symbol_overrides: 币安 BASE（无 USDT 后缀）→ Gate 合约如 OTHER/USDT。
    """

    enabled: bool = False
    ws_url: str = ""
    move_lookback_sec: float = 1.0
    min_move_pct: float = 3.0
    min_quote_vol_24h_bn: float = 2_000_000.0
    max_hurdle_frac: float = 0.002
    signal_cooldown_sec: float = 45.0
    max_signals_per_minute: int = 6
    trade_notional_usd: float = 40.0
    leverage: int = 10
    tp_bps: float = 25.0
    enable_short_on_dump: bool = False
    client_oid_prefix: str = "SNIPER_LL"
    silo_tag: str = "SNIPER_LEADLAG_001"
    auto_subscribe_gate_ob: bool = True
    sl_bps: float = 100.0
    # --- Post-Only 限价止盈括号（净利结界）：P_tp = P_fill * (1 ± (fee_taker + spread + margin_net)) ---
    bracket_target_net_frac: float = 0.0015
    bracket_min_tp_bps: float = 15.0
    initial_sl_bps: float = 120.0
    breakeven_arm_frac: float = 0.0002
    symbol_overrides: Dict[str, str] = Field(default_factory=dict)


class SlingshotConfig(BaseModel):
    """
    引力弹弓：极端均值回归。三维极值（布林 kσ + 短周期 RSI + CVD 瀑布）+
    Post-Only 挂单接针；成交后微反弹括号止盈 + 宽容止损 + 时间止损。
    """

    enabled: bool = False
    bb_period: int = 20
    bb_std_mult: float = 3.0
    rsi_period: int = 3
    rsi_oversold: float = 10.0
    rsi_overbought: float = 90.0
    cvd_waterfall_mult: float = 3.5
    entry_depth_bps: float = 50.0
    trade_notional_usd: float = 80.0
    leverage: int = 10
    tp_bps: float = 20.0
    sl_bps: float = 75.0
    time_stop_sec: float = 180.0
    time_stop_min_bounce_bps: float = 8.0
    signal_cooldown_sec: float = 90.0
    require_attack_mode: bool = False
    net_edge_gate_enabled: bool = True
    min_expected_net_usdt: float = 0.0
    friction_assume_taker_entry: bool = False
    friction_exit_is_maker: bool = True


class SharkScalpConfig(BaseModel):
    """
    狂鲨微秒剥头皮：BBO 量失衡 + 近窗同向主动成交 → 固定名义开仓；
    止盈/止损按绝对 USDT 净利目标反推价格（fiat_tp_sl），并计入手续费。
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    # True：净利目标随权益缩放（默认 100U→+1U / −0.5U）；False 用 target_net_usdt / risk_net_usdt
    scale_tp_sl_to_equity: bool = True
    tp_net_equity_fraction: float = 0.01
    sl_net_equity_fraction: float = 0.005
    target_net_usdt: float = 20.0
    risk_net_usdt: float = 10.0
    fixed_notional_usdt: float = 10000.0
    leverage: int = 10
    book_window_sec: float = 10.0
    bid_ask_size_ratio_min: float = 5.0
    min_consecutive_taker_buys: int = 10
    min_best_ask_contracts: float = 1e-9
    signal_cooldown_sec: float = 0.0
    max_equity_fraction_per_shot: float = 0.35
    max_leverage: int = 20
    require_attack_mode: bool = False
    long_only: bool = True
    # 喂经验库 / Darwin：进场前微观快照窗口（秒）
    micro_snapshot_sec: float = 5.0
    # 高 ATR 时 scale_tp_sl_to_equity 的净利倍数（与 strategy.params.core_high_atr_threshold 同源）
    high_atr_net_tp_mult: float = 1.28
    high_atr_net_sl_mult: float = 1.55


class AutoTunerConfig(BaseModel):
    """绩效窗口 + 侦察模式：对 ai_win_rate 置信度打折；微仓用低杠杆 + 名义对齐 min_notional。"""

    enabled: bool = True
    window_trades: int = 10
    enter_consecutive_losses: int = 3
    enter_win_rate_max: float = 0.3
    min_trades_for_enter_wr: int = 3
    confidence_penalty_multiplier: float = 0.8
    recovery_trades: int = 5
    recovery_win_rate_min: float = 0.5
    probe_notional_floor_usdt: float = 10.0
    probe_leverage: int = 2


class MarketOracleConfig(BaseModel):
    """战术雷达 + 防绞杀阈值（StrategyEngine 在组装 OrderIntent 前核验方向）。"""

    enabled: bool = True
    cache_ttl_sec: float = 8.0
    orderbook_depth_pct: float = 0.01
    crowded_ls_ratio: float = 2.5
    crowded_funding_rate_min: float = 0.0005
    long_obi_veto_max: float = -0.4
    crash_anchor_symbol: str = "BTC/USDT"
    crash_lookback_minutes: int = 5
    crash_max_anchor_return_pct: float = -0.02


class InfiniteMatrixConfig(BaseModel):
    """
    极限刷单 + 逐仓隔离 + AI 趋势切换：entry_context.infinite_matrix_ultra=True 的仓位由 infinite_matrix_runner 管理。
    """

    model_config = ConfigDict(extra="ignore")
    enabled: bool = False
    min_net_close_usdt: float = 0.08
    reload_enabled: bool = True
    reload_debounce_sec: float = 0.05
    trend_trailing_activation_roe: float = 0.5
    trend_trailing_callback_roe: float = 0.004
    inject_dummy_tp_sl_for_paper: bool = True


class PlaybookConfig(BaseModel):
    """
    战术调度枢纽：2×2 矩阵 (权益 × 波动) + 交易所币对约束；象限 A 叠加游击（Maker/时间止损）。
    """

    enabled: bool = True
    matrix_capital_threshold_usdt: float = 5000.0
    matrix_volatility_threshold_pct: float = 0.01
    small_equity_threshold_usdt: float = 2000.0
    low_volatility_atr_pct: float = 0.005
    guerrilla_leverage: int = 15
    guerrilla_margin_fraction: float = 0.02
    position_ttl_minutes: float = 120.0
    guerrilla_order_ttl_ms: int = 15000
    matrix_margin_fraction_a: float = 0.02
    matrix_margin_fraction_b: float = 0.03
    matrix_margin_fraction_c: float = 0.15
    matrix_margin_fraction_d: float = 0.05


class GlobalConfig(BaseModel):
    exchange: ExchangeConfig = ExchangeConfig()
    execution: ExecutionConfig = ExecutionConfig()
    paper_engine: PaperEngineConfig = PaperEngineConfig()
    playbook: PlaybookConfig = PlaybookConfig()
    market_oracle: MarketOracleConfig = MarketOracleConfig()
    auto_tuner: AutoTunerConfig = AutoTunerConfig()
    risk: RiskConfig = RiskConfig()
    strategy: StrategyConfig = StrategyConfig()
    beta_neutral_hf: BetaNeutralHFConfig = BetaNeutralHFConfig()
    darwin: DarwinConfig = DarwinConfig()
    exit_management: ExitManagementConfig = ExitManagementConfig()
    predator_matrix: PredatorMatrixConfig = PredatorMatrixConfig()
    institutional_schemes: InstitutionalSchemesConfig = InstitutionalSchemesConfig()
    l1_fast_loop: L1FastLoopConfig = L1FastLoopConfig()
    l2_command: L2CommandConfig = L2CommandConfig()
    gate_hot_universe: GateHotUniverseConfig = GateHotUniverseConfig()
    slingshot: SlingshotConfig = SlingshotConfig()
    assassin_micro: AssassinMicroConfig = AssassinMicroConfig()
    shark_scalp: SharkScalpConfig = SharkScalpConfig()
    volume_radar: VolumeRadarConfig = VolumeRadarConfig()
    binance_leadlag: BinanceLeadLagConfig = BinanceLeadLagConfig()
    infinite_matrix: InfiniteMatrixConfig = InfiniteMatrixConfig()
    license_path: str = "license/license.key"

class ConfigManager:
    _instance = None
    _config_path: str = "config/settings.yaml"

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(ConfigManager, cls).__new__(cls)
            cls._instance.config = GlobalConfig()
            cls._instance.load_config()
        return cls._instance

    def load_config(self):
        self._config_path = _resolve_settings_yaml_path()
        if os.path.exists(self._config_path):
            try:
                with open(self._config_path, 'r') as f:
                    if self._config_path.endswith('.yaml') or self._config_path.endswith('.yml'):
                        data = yaml.safe_load(f)
                    else:
                        data = json.load(f)
                    # Handle empty file or None
                    if data:
                         self.config = GlobalConfig(**_sanitize_loaded_config_data(data))
                log.info(f"Configuration loaded from {self._config_path}")
            except Exception as e:
                log.error(
                    f"Failed to load config: {e}. Using defaults — "
                    "darwin/LLM and other fields from yaml are IGNORED until valid."
                )
        else:
            log.warning("Config file not found. Using defaults.")
            self.save_config()

    def save_config(self):
        try:
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, 'w') as f:
                if self._config_path.endswith('.yaml') or self._config_path.endswith('.yml'):
                    # Convert model to dict first, then dump to yaml
                    yaml.dump(self.config.model_dump(), f, default_flow_style=False)
                else:
                    f.write(self.config.model_dump_json(indent=2))
            log.info("Configuration saved.")
        except OSError as e:
            log.error(f"Failed to save config: {e}")
            if getattr(e, "errno", None) == 30:  # EROFS: e.g. Docker volume mounted :ro
                log.error(
                    "Hint: config path is read-only (often docker-compose :ro on settings.yaml). "
                    "Remove :ro or set a writable mount."
                )
        except Exception as e:
            log.error(f"Failed to save config: {e}")

    def update_exchange_config(self, api_key: str, api_secret: str, sandbox_mode: bool = False):
        self.config.exchange.api_key = api_key
        self.config.exchange.api_secret = api_secret
        self.config.exchange.sandbox_mode = sandbox_mode
        self.save_config()

    def update_risk_config(self, **kwargs):
        updated = False
        for k, v in kwargs.items():
            if hasattr(self.config.risk, k):
                setattr(self.config.risk, k, v)
                updated = True
        if updated:
            self.save_config()

    def update_strategy_config(self, **kwargs):
        updated = False
        # Handle top-level strategy config
        for k, v in kwargs.items():
            if k == 'params':
                 # Handle nested params
                 for pk, pv in v.items():
                     if hasattr(self.config.strategy.params, pk):
                         setattr(self.config.strategy.params, pk, pv)
                         updated = True
            elif hasattr(self.config.strategy, k):
                setattr(self.config.strategy, k, v)
                updated = True
        if updated:
            self.save_config()

    def _normalize_symbol_key(self, key: str) -> str:
        k = (key or "").strip()
        if "_" in k and "/" not in k:
            if k.endswith("_USDT") and "_USDT" in k:
                base = k.replace("_USDT", "")
                return f"{base}/USDT"
        return k

    def apply_darwin_llm_result(self, result: Dict[str, Any]) -> bool:
        """
        Merge a researcher JSON payload into live config and persist settings.yaml.
        Expected shape: { "reflection": str, "patches": { "risk": {...}, "strategy": {...}, "strategy_params": {...},
        "shark_scalp": {...}, "symbols": { "PEPE/USDT": {...} } } }
        """
        patches = result.get("patches") if isinstance(result.get("patches"), dict) else result
        if not isinstance(patches, dict):
            return False
        changed = False

        # Top-level strategy fields Darwin may tune (whitelist; avoid LLM overwriting symbols / active_strategies)
        _darwin_strategy_top_keys = frozenset(
            {
                "single_open_per_symbol",
                "regime_switch_anchor_symbol",
                "regime_switch_close_positions",
                "allocations",
            }
        )

        risk_keys = set(RiskConfig.model_fields.keys())
        risk_in = patches.get("risk") if isinstance(patches.get("risk"), dict) else {}
        risk_updates = {k: v for k, v in risk_in.items() if k in risk_keys and v is not None}
        if risk_updates:
            self.update_risk_config(**risk_updates)
            changed = True

        strat_in = patches.get("strategy") if isinstance(patches.get("strategy"), dict) else {}
        if strat_in:
            sk = set(StrategyConfig.model_fields.keys())
            clean_st = {
                k: v
                for k, v in strat_in.items()
                if k in sk and k in _darwin_strategy_top_keys and v is not None
            }
            if clean_st:
                self.update_strategy_config(**clean_st)
                changed = True

        shark_in = patches.get("shark_scalp") if isinstance(patches.get("shark_scalp"), dict) else {}
        if shark_in:
            fk = set(SharkScalpConfig.model_fields.keys())
            for k, v in shark_in.items():
                if k not in fk or v is None:
                    continue
                if k == "enabled":
                    log.warning("[Darwin] Ignoring shark_scalp.enabled from LLM (safety).")
                    continue
                try:
                    setattr(self.config.shark_scalp, k, v)
                    changed = True
                except Exception as e:
                    log.warning(f"[Darwin] Ignoring invalid shark_scalp.{k}: {e}")

        sp_in = patches.get("strategy_params") if isinstance(patches.get("strategy_params"), dict) else {}
        if sp_in:
            param_keys = set(StrategyParams.model_fields.keys())
            clean = {k: v for k, v in sp_in.items() if k in param_keys and v is not None}
            if clean:
                self.update_strategy_config(params=clean)
                changed = True

        sym_in = patches.get("symbols") if isinstance(patches.get("symbols"), dict) else {}
        for raw_sym, raw_patch in sym_in.items():
            if not isinstance(raw_patch, dict):
                continue
            raw_patch = dict(raw_patch)
            if "leverage_cap" in raw_patch and "max_leverage" not in raw_patch:
                raw_patch["max_leverage"] = raw_patch["leverage_cap"]
            sym = self._normalize_symbol_key(str(raw_sym))
            prev = self.config.darwin.symbol_patches.get(sym, DarwinSymbolPatch())
            merged = {**prev.model_dump(exclude_none=True)}
            for pk, pv in raw_patch.items():
                if pk in DarwinSymbolPatch.model_fields and pv is not None:
                    merged[pk] = pv
            try:
                self.config.darwin.symbol_patches[sym] = DarwinSymbolPatch(**merged)
                changed = True
            except Exception as e:
                log.warning(f"[Darwin] Ignoring invalid symbol patch for {sym}: {e}")

        l1_yaml = (
            patches.get("l1_fast_loop") if isinstance(patches.get("l1_fast_loop"), dict) else {}
        )
        if l1_yaml:
            l1_keys = set(L1FastLoopConfig.model_fields.keys())
            for k, v in l1_yaml.items():
                if k not in l1_keys or v is None:
                    continue
                if k == "enabled":
                    log.warning("[Darwin] Ignoring l1_fast_loop.enabled from LLM (safety).")
                    continue
                try:
                    setattr(self.config.l1_fast_loop, k, v)
                    changed = True
                except Exception as e:
                    log.warning(f"[Darwin] Ignoring invalid l1_fast_loop.{k}: {e}")

        l1_run = (
            patches.get("l1_runtime") if isinstance(patches.get("l1_runtime"), dict) else {}
        )
        if l1_run:
            from src.core.l1_fast_loop import apply_l1_tuning

            apply_l1_tuning(l1_run)

        if changed:
            self.save_config()
        return changed or bool(l1_run)

    def get_config(self) -> GlobalConfig:
        return self.config

# Global instance
config_manager = ConfigManager()
