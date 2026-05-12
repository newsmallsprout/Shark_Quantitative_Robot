package planning

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math"
	"net/http"
	"time"

	"github.com/redis/go-redis/v9"
)

// Planner — SlowLoop 核心：数据聚合 → 计划生成 → Redis 持久化
// v2: 自进化 — 每个周期根据成交质量反馈调优 multiplier
type Planner struct {
	rdb    *redis.Client
	state  PlanningState
	plans  map[string]*RangePlan
	fuse   map[string]*FuseState
	prices map[string]float64

	// 子模块
	macro  *MacroBuilder
	book   *BookIngestor
	news   *NewsIngestor
	http   *http.Client

	// 自进化
	evo       *EvoState
	lastPlan  int64
	symbols   []string
}

func NewPlanner(rdb *redis.Client, symbols []string) *Planner {
	return &Planner{
		rdb:     rdb,
		state:   StateBootstrap,
		plans:   make(map[string]*RangePlan),
		fuse:    make(map[string]*FuseState),
		prices:  make(map[string]float64),
		macro:   NewMacroBuilder(),
		book:    NewBookIngestor(),
		news:    NewNewsIngestor(),
		http:    &http.Client{Timeout: 10 * time.Second},
		evo:     DefaultEvo(),
		symbols: symbols,
	}
}

func (p *Planner) State() PlanningState { return p.state }

// Bootstrap → 全量数据拉取 → 生成所有币对计划
func (p *Planner) Bootstrap(ctx context.Context) error {
	log.Printf("[Planning] BOOTSTRAP — 拉取全量数据，覆盖%d个币对...", len(p.symbols))

	p.fetchPrices(ctx)

	_ = p.macro.Fetch(ctx, "BTC/USDT")
	_ = p.macro.Fetch(ctx, "ETH/USDT")

	digest := p.news.Fetch(ctx)

	bootstrapOrder := priorityOrder(p.symbols)
	successCount := 0
	for _, sym := range bootstrapOrder {
		if err := p.Plan(ctx, sym, digest); err != nil {
			log.Printf("[Planning] Bootstrap计划生成失败(%s): %v", sym, err)
		} else {
			successCount++
		}
	}

	if successCount == 0 {
		p.state = StatePaused
		return fmt.Errorf("bootstrap: 所有计划生成失败")
	}

	p.state = StateLive
	log.Printf("[Planning] BOOTSTRAP → LIVE (%d/%d 成功) · 进化代数=%d ATR×%.1f STOP×%.1f TP×%.1f",
		successCount, len(p.symbols), p.evo.Generation, p.evo.AtRMult, p.evo.StopOffset, p.evo.TpMult)
	return nil
}

func (p *Planner) Plan(ctx context.Context, symbol string, digest *NewsDigest) error {
	start := time.Now()
	p.state = StatePlanning

	if !IsLargeCap(symbol) {
		return nil
	}

	depth, err := p.book.Fetch(ctx, symbol)
	if err != nil {
		px := p.prices[symbol]
		if px <= 0 {
			px = 80000
		}
		depth = &DepthProfile{
			SupportPrice: px * 0.98, ResistancePrice: px * 1.02,
			SupportStrength: 0.5, ResistanceStrength: 0.5,
		}
	}

	macro := p.getMacro(symbol)

	if digest == nil {
		digest = p.news.Fetch(ctx)
	}

	funding := p.fetchFunding(ctx, symbol)

	plan := p.buildPlan(symbol, depth, macro, digest, funding)
	plan.GeneratedAt = time.Now().Unix()
	plan.ValidUntil = plan.GeneratedAt + 1800
	plan.EvoGen = p.evo.Generation

	if err := p.audit(plan); err != nil {
		log.Printf("[Planning] 审计失败(%s): %v — 沿用旧计划", symbol, err)
		return nil
	}

	data, _ := json.Marshal(plan)
	key := fmt.Sprintf("shark:plan:%s", symbol)
	p.rdb.Set(ctx, key, data, 35*time.Minute)
	p.rdb.Publish(ctx, "shark:plan:updated", string(data))

	if digest.RiskLevel >= 2 {
		plan.State = string(StatePaused)
		p.state = StatePaused
		log.Printf("[Planning] ⚠️ 新闻风险爆表 → PAUSED (flags=%v)", digest.Flags)
	} else if fs, ok := p.fuse[symbol]; ok && fs != nil && fs.Triggered {
		plan.State = string(StatePaused)
	} else {
		plan.State = string(StateLive)
	}

	p.plans[symbol] = plan
	p.lastPlan = time.Now().Unix()

	log.Printf("[Planning] ✅ %s: regime=%s bias=%s range=[%.0f,%.0f] entry=[%.0f,%.0f] sl=%.0f evo=gen%d (%.1fs)",
		symbol, plan.Regime, plan.Bias,
		plan.RangeLow, plan.RangeHigh,
		plan.EntryZoneLow, plan.EntryZoneHigh,
		plan.StopLoss, plan.EvoGen, time.Since(start).Seconds())
	return nil
}

// PlanAll — 批量为所有币对生成计划；完成后评估并自进化
func (p *Planner) PlanAll(ctx context.Context) {
	log.Printf("[Planning] 开始全量计划更新 (%d个币对)...", len(p.symbols))

	p.fetchPrices(ctx)
	_ = p.macro.Fetch(ctx, "BTC/USDT")
	_ = p.macro.Fetch(ctx, "ETH/USDT")

	digest := p.news.Fetch(ctx)

	bootstrapOrder := priorityOrder(p.symbols)
	successCount := 0
	for _, sym := range bootstrapOrder {
		if err := p.Plan(ctx, sym, digest); err != nil {
			log.Printf("[Planning] 计划生成失败(%s): %v", sym, err)
		} else {
			successCount++
		}
	}

	// 自进化：每3个周期评估一次
	p.evo.PlansGenerated += successCount
	if p.evo.PlansGenerated%105 == 0 {
		p.evolve(ctx)
	}

	log.Printf("[Planning] 全量计划完成 (%d/%d)", successCount, len(bootstrapOrder))
}

// ── 自进化 ──

func (p *Planner) evolve(ctx context.Context) {
	trades := p.readRecentTrades(ctx, 50)
	if len(trades) < 5 {
		return
	}

	var wins, stops int
	var totalPnl float64
	for _, t := range trades {
		if t.Pnl > 0 {
			wins++
		}
		if t.ExitType == "sl" {
			stops++
		}
		totalPnl += t.PnlPct
	}

	p.evo.WinRate = float64(wins) / float64(len(trades))
	p.evo.StopHitRate = float64(stops) / float64(len(trades))
	p.evo.AvgPnlPct = totalPnl / float64(len(trades))
	p.evo.Generation++
	p.evo.LastEval = time.Now().Unix()

	// 自适应调优
	prevAtR := p.evo.AtRMult
	prevStop := p.evo.StopOffset
	prevTp := p.evo.TpMult

	// 止损率高 → 放宽止损
	if p.evo.StopHitRate > 0.5 {
		p.evo.StopOffset = clamp(p.evo.StopOffset+0.2, 0.3, 2.0)
		p.evo.AtRMult = clamp(p.evo.AtRMult*1.1, 1.0, 4.0)
	}
	// 胜率低 → 收紧入场带
	if p.evo.WinRate < 0.4 {
		p.evo.EntryMargin = clamp(p.evo.EntryMargin*0.8, 0.1, 1.0)
	}
	// 胜率高 → 放大止盈
	if p.evo.WinRate > 0.6 {
		p.evo.TpMult = clamp(p.evo.TpMult*1.15, 1.0, 5.0)
	}
	// 平均亏损 → 收窄区间
	if p.evo.AvgPnlPct < -0.5 {
		p.evo.AtRMult = clamp(p.evo.AtRMult*0.9, 1.0, 4.0)
	}

	log.Printf("[Evo] gen=%d trades=%d win=%.0f%% stop=%.0f%% avgPnl=%.1f%% adjustments: ATR×%.1f→%.1f STOP×%.1f→%.1f TP×%.1f→%.1f entry×%.1f",
		p.evo.Generation, len(trades), p.evo.WinRate*100, p.evo.StopHitRate*100, p.evo.AvgPnlPct,
		prevAtR, p.evo.AtRMult, prevStop, p.evo.StopOffset, prevTp, p.evo.TpMult, p.evo.EntryMargin)
}

func (p *Planner) readRecentTrades(ctx context.Context, limit int) []TradeSnapshot {
	raw, err := p.rdb.LRange(ctx, "shark:trade_history", 0, int64(limit-1)).Result()
	if err != nil {
		return nil
	}

	var trades []TradeSnapshot
	for _, r := range raw {
		var t TradeSnapshot
		if json.Unmarshal([]byte(r), &t) == nil {
			trades = append(trades, t)
		}
	}
	return trades
}

// ── 价格 ──

func (p *Planner) fetchPrices(ctx context.Context) {
	pattern := "shark:price:*"
	keys, err := p.rdb.Keys(ctx, pattern).Result()
	if err != nil || len(keys) == 0 {
		// Redis无价格 → 从Gate.io API拉取
		log.Println("[Planning] Redis无价格数据，从Gate.io拉取...")
		p.fetchPricesFromAPI(ctx)
		return
	}

	count := 0
	for _, k := range keys {
		sym := k[12:]
		if !IsLargeCap(sym) {
			continue
		}
		val, err := p.rdb.Get(ctx, k).Float64()
		if err == nil && val > 0 {
			p.prices[sym] = val
			count++
		}
	}
	log.Printf("[Planning] 价格快照: %d个币对 (来源=Redis)", count)
}

func (p *Planner) fetchPricesFromAPI(ctx context.Context) {
	// Gate.io 永续合约ticker — 批量获取所有合约价格
	url := "https://api.gateio.ws/api/v4/futures/usdt/tickers"
	req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
	resp, err := p.http.Do(req)
	if err != nil {
		log.Printf("[Planning] Gate.io ticker拉取失败: %v", err)
		return
	}
	defer resp.Body.Close()

	var tickers []struct {
		Contract string `json:"contract"`
		Last     string `json:"last"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&tickers); err != nil {
		log.Printf("[Planning] ticker解析失败: %v", err)
		return
	}

	count := 0
	for _, t := range tickers {
		if t.Contract == "" || t.Last == "" {
			continue
		}
		sym := contractToSymbol(t.Contract)
		if !IsLargeCap(sym) {
			continue
		}
		px := parseFloat(t.Last)
		if px <= 0 {
			continue
		}
		p.prices[sym] = px
		// 发布到Redis供evolver使用
		p.rdb.Set(ctx, "shark:price:"+sym, px, 2*time.Minute)
		count++
	}
	log.Printf("[Planning] 价格快照: %d个币对 (来源=Gate.io API)", count)
}

func contractToSymbol(contract string) string {
	for i, c := range contract {
		if c == '_' {
			return contract[:i] + "/USDT"
		}
	}
	return contract + "/USDT"
}

// ── Fuse ──

func (p *Planner) CheckFuse(symbol string, currentPrice, price1MinAgo float64) *FuseState {
	if price1MinAgo <= 0 {
		return &FuseState{}
	}
	chg := math.Abs((currentPrice-price1MinAgo)/price1MinAgo) * 100
	if chg > 3.0 {
		fs := &FuseState{
			Triggered:   true,
			Reason:      fmt.Sprintf("1分钟波动%.1f%% > 3%%阈值", chg),
			PriceChange: chg,
			TriggeredAt: time.Now().Unix(),
		}
		p.fuse[symbol] = fs
		p.state = StatePaused
		log.Printf("[Fuse] ⚡ 熔断触发! %s %s", symbol, fs.Reason)
		return fs
	}
	if fs, ok := p.fuse[symbol]; ok && fs != nil && fs.Triggered && time.Now().Unix()-fs.TriggeredAt > 300 {
		p.fuse[symbol] = nil
		p.state = StateReplanPending
		log.Printf("[Fuse] %s 熔断解除 → REPLAN_PENDING", symbol)
	}
	return p.fuse[symbol]
}

func (p *Planner) GetPlan(symbol string) *RangePlan {
	plan, ok := p.plans[symbol]
	if !ok || plan == nil {
		return nil
	}
	if plan.IsExpired() && p.state == StateLive {
		p.state = StateReplanPending
	}
	return plan
}

// ── 内部 ──

func (p *Planner) fetchFunding(ctx context.Context, symbol string) float64 {
	val, err := p.rdb.Get(ctx, fmt.Sprintf("shark:funding:%s", symbol)).Float64()
	if err != nil {
		return 0
	}
	return val
}

func (p *Planner) getMacro(symbol string) *MacroContext {
	macro := p.macro.Get(symbol)
	if macro != nil {
		return macro
	}
	// 山寨币用BTC宏观推断
	if btcMacro := p.macro.Get("BTC/USDT"); btcMacro != nil {
		px := p.prices[symbol]
		if px <= 0 {
			px = 80000
		}
		return &MacroContext{
			Symbol:   symbol,
			Regime:   btcMacro.Regime,
			ATR14:    px * 0.02,
			VolPct:   btcMacro.VolPct,
			TrendStr: btcMacro.TrendStr,
		}
	}
	px := p.prices[symbol]
	if px <= 0 {
		px = 80000
	}
	return &MacroContext{
		Symbol: symbol, Regime: RegimeRange,
		ATR14: px * 0.02, VolPct: 50,
	}
}

// buildPlan — 纯数学构建 RangePlan（自进化 multiplier 驱动）
func (p *Planner) buildPlan(symbol string, depth *DepthProfile, macro *MacroContext,
	news *NewsDigest, funding float64) *RangePlan {

	px := p.prices[symbol]
	if px <= 0 {
		px = 80000
	}

	pxPlan := px
	if depth != nil && depth.SupportPrice > 0 && depth.ResistancePrice > depth.SupportPrice {
		midBook := (depth.SupportPrice + depth.ResistancePrice) / 2
		if midBook > 0 {
			if pxPlan <= 0 {
				pxPlan = midBook
			} else {
				dv := math.Abs(midBook-pxPlan) / pxPlan
				if dv <= 0.05 {
					// 订单簿重心与 ticker 接近时，用中间价减少区间锚在「错价」上
					pxPlan = (pxPlan + midBook) / 2
				}
			}
		}
	}
	if pxPlan <= 0 {
		pxPlan = px
	}

	e := p.evo
	atr := macro.ATR14
	if atr <= 0 {
		atr = pxPlan * 0.02
	}

	plan := &RangePlan{
		Symbol:      symbol,
		Regime:      string(macro.Regime),
		LeverageCap: 5,
		ATR14:       atr,
	}

	// 区间：ATR 带，并至少覆盖订单簿支撑/压力
	plan.RangeLow = pxPlan - atr*e.AtRMult
	plan.RangeHigh = pxPlan + atr*e.AtRMult
	if depth != nil && depth.SupportPrice > 0 && depth.ResistancePrice > depth.SupportPrice {
		plan.RangeLow = math.Min(plan.RangeLow, depth.SupportPrice)
		plan.RangeHigh = math.Max(plan.RangeHigh, depth.ResistancePrice)
	}

	// 方向 + 入场带
	switch {
	case macro.Regime == RegimeTrendUp && depth.SupportStrength > 0.5:
		plan.Bias = "long"
		plan.EntryZoneLow = depth.SupportPrice
		plan.EntryZoneHigh = pxPlan - atr*e.EntryMargin
		plan.StopLoss = depth.SupportPrice - atr*e.StopOffset
	case macro.Regime == RegimeTrendDown && depth.ResistanceStrength > 0.5:
		plan.Bias = "short"
		plan.EntryZoneLow = pxPlan + atr*e.EntryMargin
		plan.EntryZoneHigh = depth.ResistancePrice
		plan.StopLoss = depth.ResistancePrice + atr*e.StopOffset
	default:
		plan.Bias = "long"
		plan.EntryZoneLow = depth.SupportPrice
		plan.EntryZoneHigh = pxPlan - atr*e.EntryMargin
		plan.StopLoss = depth.SupportPrice - atr*e.StopOffset
	}

	if plan.Bias == "short" {
		plan.TakeProfit = []float64{pxPlan - atr*e.TpMult, pxPlan - atr*e.TpMult*2}
	} else {
		plan.TakeProfit = []float64{pxPlan + atr*e.TpMult, pxPlan + atr*e.TpMult*2}
	}

	plan.SupportStrength = depth.SupportStrength
	plan.ResistanceStrength = depth.ResistanceStrength
	plan.NewsRiskLevel = news.RiskLevel
	plan.RiskFlags = news.Flags
	plan.MacroRegime = macro.Regime
	plan.FundingRate = funding

	clampPlan(plan, pxPlan)
	return plan
}

func clampPlan(plan *RangePlan, px float64) {
	// 支撑不应高于现价
	if plan.Bias == "long" && plan.EntryZoneLow > px {
		plan.EntryZoneLow = px * 0.97
	}
	// 阻力不应低于现价
	if plan.Bias == "short" && plan.EntryZoneHigh < px {
		plan.EntryZoneHigh = px * 1.03
	}
	// 入场带高低顺序修正
	if plan.EntryZoneLow > plan.EntryZoneHigh {
		plan.EntryZoneLow, plan.EntryZoneHigh = plan.EntryZoneHigh, plan.EntryZoneLow
	}
	if plan.EntryZoneLow < px*0.9 || plan.EntryZoneLow <= 0 {
		plan.EntryZoneLow = px * 0.97
	}
	if plan.EntryZoneHigh > px*1.1 || plan.EntryZoneHigh <= 0 {
		plan.EntryZoneHigh = px * 1.01
	}
	if plan.RangeLow > plan.RangeHigh {
		plan.RangeLow, plan.RangeHigh = plan.RangeHigh, plan.RangeLow
	}
	if plan.RangeLow <= 0 {
		plan.RangeLow = px * 0.99
	}
	if plan.RangeHigh <= 0 {
		plan.RangeHigh = px * 1.01
	}
	// 避免退化成一条线；不再把区间硬夹到 ±15%（会砍掉真实支撑/压力）
	if plan.RangeHigh-plan.RangeLow < px*1e-4 {
		plan.RangeLow = px * 0.995
		plan.RangeHigh = px * 1.005
	}
	if px > 0 && px < plan.RangeLow {
		plan.RangeLow = math.Min(plan.RangeLow, px*0.995)
	}
	if px > 0 && px > plan.RangeHigh {
		plan.RangeHigh = math.Max(plan.RangeHigh, px*1.005)
	}
	if plan.Bias == "long" {
		if plan.StopLoss >= plan.EntryZoneLow || plan.StopLoss < px*0.85 || plan.StopLoss <= 0 {
			plan.StopLoss = plan.EntryZoneLow * 0.97
		}
	} else {
		if plan.StopLoss <= plan.EntryZoneHigh || plan.StopLoss > px*1.15 || plan.StopLoss <= 0 {
			plan.StopLoss = plan.EntryZoneHigh * 1.03
		}
	}
}

func (p *Planner) audit(plan *RangePlan) error {
	entry := (plan.EntryZoneLow + plan.EntryZoneHigh) / 2
	if plan.Bias == "long" {
		if plan.StopLoss >= entry {
			return fmt.Errorf("long: sl(%.2f) >= entry(%.2f)", plan.StopLoss, entry)
		}
		for _, tp := range plan.TakeProfit {
			if tp <= entry {
				return fmt.Errorf("long: tp(%.2f) <= entry(%.2f)", tp, entry)
			}
		}
	} else if plan.Bias == "short" {
		if plan.StopLoss <= entry {
			return fmt.Errorf("short: sl(%.2f) <= entry(%.2f)", plan.StopLoss, entry)
		}
		for _, tp := range plan.TakeProfit {
			if tp >= entry {
				return fmt.Errorf("short: tp(%.2f) >= entry(%.2f)", tp, entry)
			}
		}
	}
	return nil
}

// ── 工具 ──

func clamp(v, low, high float64) float64 {
	if v < low {
		return low
	}
	if v > high {
		return high
	}
	return v
}

func priorityOrder(symbols []string) []string {
	ordered := make([]string, 0, len(symbols))
	if contains(symbols, "BTC/USDT") {
		ordered = append(ordered, "BTC/USDT")
	}
	if contains(symbols, "ETH/USDT") {
		ordered = append(ordered, "ETH/USDT")
	}
	for _, s := range symbols {
		if s != "BTC/USDT" && s != "ETH/USDT" {
			ordered = append(ordered, s)
		}
	}
	return ordered
}

func contains(slice []string, item string) bool {
	for _, s := range slice {
		if s == item {
			return true
		}
	}
	return false
}
