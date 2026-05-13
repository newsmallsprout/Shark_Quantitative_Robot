// Package planning — SlowLoop 规划域（Go 实现，30分钟周期）
// 职责：拉取全量数据 → 构建 RangePlan → 写入 Redis → FastLoop 读取执行
// v2: 自进化 — 每个周期根据成交质量反馈调优 multiplier
package planning

import "time"

// PlanningState 状态机枚举
type PlanningState string

const (
	StateBootstrap     PlanningState = "BOOTSTRAP"
	StatePlanning      PlanningState = "PLANNING"
	StateLive          PlanningState = "LIVE"
	StatePaused        PlanningState = "PAUSED"
	StateReplanPending PlanningState = "REPLAN_PENDING"
)

// Regime 大盘状态
type Regime string

const (
	RegimeTrendUp       Regime = "trend_up"
	RegimeTrendDown     Regime = "trend_down"
	RegimeRange         Regime = "range_bound"
	RegimeBreakoutUp    Regime = "breakout_up"
	RegimeBreakoutDown  Regime = "breakout_down"
	RegimeSlowGrindUp   Regime = "slow_grind_up"
	RegimeSlowGrindDown Regime = "slow_grind_down"
	RegimeBleedDown     Regime = "bleed_down"
	RegimeChoppy        Regime = "choppy"
	RegimeDead          Regime = "dead"
	RegimeUnknown       Regime = "unknown"
)

// FocusSymbols 专注币对
var FocusSymbols = []string{"BTC/USDT", "ETH/USDT", "SOL/USDT"}

func IsLargeCap(symbol string) bool {
	for _, s := range FocusSymbols {
		if s == symbol {
			return true
		}
	}
	return false
}

// ── 数据结构 ──

// RangePlan — SlowLoop 产出的交易计划（Redis JSON）
type RangePlan struct {
	Symbol      string `json:"symbol"`
	GeneratedAt int64  `json:"generated_at"`
	ValidUntil  int64  `json:"valid_until"` // 30分钟后过期
	State       string `json:"state"`       // PlanningState
	Regime      string `json:"regime"`
	Bias        string `json:"bias"` // "long" | "short" | "neutral"

	// 价格区间
	RangeLow  float64 `json:"range_low"`
	RangeHigh float64 `json:"range_high"`

	// 入场带
	EntryZoneLow  float64 `json:"entry_zone_low"`
	EntryZoneHigh float64 `json:"entry_zone_high"`

	// 风控
	StopLoss    float64   `json:"stop_loss"`
	TakeProfit  []float64 `json:"take_profit"`
	LeverageCap int       `json:"leverage_cap"`

	// 深度信号
	SupportStrength    float64 `json:"support_strength"`
	ResistanceStrength float64 `json:"resistance_strength"`

	// 风险
	NewsRiskLevel int      `json:"news_risk_level"` // 0=正常, 1=警告, 2=熔断
	RiskFlags     []string `json:"risk_flags"`

	// 宏观联动
	MacroRegime Regime  `json:"macro_regime"`
	FundingRate float64 `json:"funding_rate"`
	ATR14       float64 `json:"atr14"`

	// 进化标记
	EvoGen int `json:"evo_gen"` // 当前进化代数

	// AI 驱动字段
	AiRationale     string    `json:"ai_rationale"`      // AI分析摘要
	PositionSizePct float64   `json:"position_size_pct"` // 仓位%余额
	Leverage        int       `json:"leverage"`          // 建议杠杆
	PyramidPrices   []float64 `json:"pyramid_prices"`    // 补仓点位
	CutLossPct      float64   `json:"cut_loss_pct"`      // 割肉线%
	AiModel         string    `json:"ai_model"`          // "deepseek"|"qwen"|"math"
	AiConfidence    float64   `json:"ai_confidence"`     // 0-100

	// 震荡双方向（regime=range_bound 时同时填充）
	LongEntryLow   float64   `json:"long_entry_low"`
	LongEntryHigh  float64   `json:"long_entry_high"`
	LongStopLoss   float64   `json:"long_stop_loss"`
	LongTakeProfit []float64 `json:"long_take_profit"`

	ShortEntryLow   float64   `json:"short_entry_low"`
	ShortEntryHigh  float64   `json:"short_entry_high"`
	ShortStopLoss   float64   `json:"short_stop_loss"`
	ShortTakeProfit []float64 `json:"short_take_profit"`
}

// MacroContext 大盘环境快照
type MacroContext struct {
	Symbol      string
	Regime      Regime
	RangeLow    float64
	RangeHigh   float64
	ATR14       float64
	VolPct      float64 // 波动率分位(0-100)
	TrendStr    float64 // 趋势强度(-1到1)
	BreakoutDir string  // "up"/"down"/"" — 当前是否在突破区间边界
	Timestamp   int64
}

// DepthProfile 订单簿深度分析
type DepthProfile struct {
	Symbol             string
	SupportPrice       float64
	ResistancePrice    float64
	SupportStrength    float64 // 0-1
	ResistanceStrength float64
	SpreadPct          float64
	BidVolume1Pct      float64
	AskVolume1Pct      float64
	Timestamp          int64
}

// NewsDigest 新闻摘要
type NewsDigest struct {
	RiskLevel int
	Flags     []string
	Headlines []string
	Timestamp int64
}

// FuseState 熔断状态
type FuseState struct {
	Triggered   bool
	Reason      string
	PriceChange float64
	TriggeredAt int64
}

// ── 自进化 ──

// EvoState 自进化状态：跟踪计划质量，反馈调优 multiplier
type EvoState struct {
	Generation  int     // 进化代数
	AtRMult     float64 // ATR倍率 → 控制区间宽度
	StopOffset  float64 // 止损偏移 (ATR倍数)
	TpMult      float64 // 止盈倍率
	EntryMargin float64 // 入场带边距 (ATR倍数)

	// 质量追踪
	PlansGenerated int     // 累计生成计划数
	TradesInPlan   int     // 计划内成交数
	WinRate        float64 // 胜率 (0-1)
	AvgPnlPct      float64 // 平均盈亏%
	StopHitRate    float64 // 止损触发率
	LastEval       int64   // 上次评估时间戳
}

func DefaultEvo() *EvoState {
	return &EvoState{
		AtRMult:     1.5,  // 超短线：更窄区间
		StopOffset:  0.6,  // 更紧止损
		TpMult:      1.5,  // 更快止盈
		EntryMargin: 0.10, // 更窄入场带
	}
}

// TradeSnapshot 从 Redis 读取的单笔成交摘要
type TradeSnapshot struct {
	Symbol   string  `json:"symbol"`
	Side     string  `json:"side"`
	Pnl      float64 `json:"pnl"`
	PnlPct   float64 `json:"pnl_pct"`
	ExitType string  `json:"exit_type"` // "tp" | "sl" | "timeout" | "manual"
	EntryPx  float64 `json:"entry_px"`
	ExitPx   float64 `json:"exit_px"`
	Ts       int64   `json:"ts"`
}

// ── 方法 ──

func (p *RangePlan) IsExpired() bool {
	return time.Now().Unix() > p.ValidUntil
}

func (p *RangePlan) InEntryZone(px float64) bool {
	return px >= p.EntryZoneLow && px <= p.EntryZoneHigh
}

func (p *RangePlan) InRange(px float64) bool {
	return px >= p.RangeLow && px <= p.RangeHigh
}

func (p *RangePlan) ShouldPause() bool {
	return p.NewsRiskLevel >= 2 || p.State == string(StatePaused)
}
