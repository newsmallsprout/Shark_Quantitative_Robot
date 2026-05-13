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
	macro *MacroBuilder
	book  *BookIngestor
	news  *NewsIngestor
	http  *http.Client

	// 自进化
	evo      *EvoState
	lastPlan int64
	symbols  []string

	// TV insights (set from main.go)
	tvFn func(symbol string) string
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

// SetTVFn sets the TradingView insights provider (closure from main.go)
func (p *Planner) SetTVFn(fn func(string) string) { p.tvFn = fn }

func (p *Planner) State() PlanningState { return p.state }

// Bootstrap → 全量数据拉取 → 生成所有币对计划
func (p *Planner) Bootstrap(ctx context.Context) error {
	log.Printf("[Planning] BOOTSTRAP — 拉取全量数据，覆盖%d个币对...", len(p.symbols))

	p.fetchPrices(ctx)

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

	if err := p.macro.Fetch(ctx, symbol); err != nil {
		log.Printf("[Planning] 永续宏观(%s): %v — ATR/regime 本币回退", symbol, err)
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

	plan := p.buildPlan(ctx, symbol, depth, macro, digest, funding)
	if plan == nil {
		return nil // 无价格数据，跳过
	}
	plan.GeneratedAt = time.Now().Unix()
	plan.ValidUntil = plan.GeneratedAt + 1800
	plan.EvoGen = p.evo.Generation

	if err := p.audit(plan); err != nil {
		log.Printf("[Planning] 审计失败(%s): %v — 沿用旧计划", symbol, err)
		return nil
	}

	data, _ := json.Marshal(plan)
	key := fmt.Sprintf("shark:plan:%s", symbol)
	if err := p.rdb.Set(ctx, key, data, 35*time.Minute).Err(); err != nil {
		log.Printf("[Planning] Redis写入失败(%s): %v", symbol, err)
		return nil
	}
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

	// 自进化：每6个周期评估一次（3币对×6=18计划）
	p.evo.PlansGenerated += successCount
	if p.evo.PlansGenerated >= 18 && p.evo.PlansGenerated%18 == 0 {
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
	if p.macro.Cached(symbol) {
		return p.macro.Get(symbol)
	}
	px := p.prices[symbol]
	if px <= 0 {
		px = 80000
	}
	return &MacroContext{
		Symbol:   symbol,
		Regime:   RegimeRange,
		ATR14:    px * 0.02,
		VolPct:   50,
		TrendStr: 0,
	}
}

// buildPlan — AI 主导（DeepSeek→数学审计兜底）
// v3: AI 优先，失败降级到纯数学
func (p *Planner) buildPlan(ctx context.Context, symbol string, depth *DepthProfile,
	macro *MacroContext, news *NewsDigest, funding float64) *RangePlan {

	px := p.prices[symbol]
	if px <= 0 {
		return nil
	}

	pxPlan := px
	if depth != nil && depth.SupportPrice > 0 && depth.ResistancePrice > depth.SupportPrice {
		midBook := (depth.SupportPrice + depth.ResistancePrice) / 2
		if midBook > 0 {
			dv := math.Abs(midBook-pxPlan) / pxPlan
			if dv <= 0.05 {
				pxPlan = (pxPlan + midBook) / 2
			}
		}
	}
	if pxPlan <= 0 {
		pxPlan = px
	}

	atr := macro.ATR14
	if atr <= 0 {
		atr = pxPlan * 0.02
	}

	// 尝试 AI 生成
	if aiPlan, err := p.tryAIBuild(ctx, symbol, pxPlan, atr, depth, macro, news, funding); err == nil && aiPlan != nil {
		if err := p.audit(aiPlan); err != nil {
			log.Printf("[AI] %s 审计失败(%v)，降级到数学", symbol, err)
		} else {
			log.Printf("[AI] ✅ %s 计划由 %s 生成 conf=%.0f", symbol, aiPlan.AiModel, aiPlan.AiConfidence)
			return aiPlan
		}
	}

	// ── 数学 fallback ──
	return p.mathBuild(symbol, pxPlan, atr, depth, macro, news, funding)
}

// tryAIBuild 尝试用 AI 生成计划，失败返回 error
func (p *Planner) tryAIBuild(ctx context.Context, symbol string, px, atr float64,
	depth *DepthProfile, macro *MacroContext, news *NewsDigest, funding float64) (*RangePlan, error) {

	// 构建 AI 分析上下文
	pc := &PlanContext{
		Symbol:        symbol,
		Regime:        string(macro.Regime),
		Price:         px,
		ATR14:         atr,
		FundingRate:   funding,
		SupportStr:    depth.SupportStrength,
		ResistanceStr: depth.ResistanceStrength,
		NewsRisk:      news.RiskLevel,
		NewsFlags:     news.Flags,
		BreakoutDir:   macro.BreakoutDir,
	}
	if pc.SupportStr <= 0 {
		pc.SupportStr = 0.5
	}
	if pc.ResistanceStr <= 0 {
		pc.ResistanceStr = 0.5
	}
	// 估算区间供 AI 参考
	pc.RangeLow = px - atr*1.5
	pc.RangeHigh = px + atr*1.5

	// TV insights
	if p.tvFn != nil {
		pc.TVInsights = p.tvFn(symbol)
	}

	// 调用 AI
	ai, err := p.aiGeneratePlan(ctx, symbol, pc)
	if err != nil {
		return nil, err
	}

	// AI → RangePlan
	plan := &RangePlan{
		Symbol:             symbol,
		Regime:             string(macro.Regime),
		LeverageCap:        125,
		ATR14:              atr,
		AiModel:            "deepseek",
		AiRationale:        ai.Rationale,
		AiConfidence:       ai.Confidence,
		PositionSizePct:    ai.PositionSizePct,
		Leverage:           ai.Leverage,
		PyramidPrices:      ai.PyramidPrices,
		CutLossPct:         ai.CutLossPct,
		SupportStrength:    depth.SupportStrength,
		ResistanceStrength: depth.ResistanceStrength,
		NewsRiskLevel:      news.RiskLevel,
		RiskFlags:          news.Flags,
		MacroRegime:        macro.Regime,
		FundingRate:        funding,
	}

	// 填入 AI 产出的区间值
	aiRangeLow := ai.LongEntryLow
	aiRangeHigh := ai.LongEntryHigh
	hasLong := ai.LongEntryLow > 0 && ai.LongEntryHigh > ai.LongEntryLow
	hasShort := ai.ShortEntryLow > 0 && ai.ShortEntryHigh > ai.ShortEntryLow

	if hasLong && hasShort {
		plan.Bias = "both"
		plan.LongEntryLow = ai.LongEntryLow
		plan.LongEntryHigh = ai.LongEntryHigh
		plan.LongStopLoss = ai.LongSL
		plan.LongTakeProfit = ai.LongTP
		plan.ShortEntryLow = ai.ShortEntryLow
		plan.ShortEntryHigh = ai.ShortEntryHigh
		plan.ShortStopLoss = ai.ShortSL
		plan.ShortTakeProfit = ai.ShortTP
		plan.RangeLow = math.Min(ai.LongEntryLow*0.995, ai.LongSL)
		plan.RangeHigh = math.Max(ai.ShortEntryHigh*1.005, ai.ShortSL)
	} else if hasLong {
		plan.Bias = "long"
		plan.EntryZoneLow = ai.LongEntryLow
		plan.EntryZoneHigh = ai.LongEntryHigh
		plan.StopLoss = ai.LongSL
		plan.TakeProfit = ai.LongTP
		plan.LongEntryLow = ai.LongEntryLow
		plan.LongEntryHigh = ai.LongEntryHigh
		plan.LongStopLoss = ai.LongSL
		plan.LongTakeProfit = ai.LongTP
		aiRangeLow = ai.LongEntryLow
		aiRangeHigh = ai.LongTP[len(ai.LongTP)-1]
	} else if hasShort {
		plan.Bias = "short"
		plan.EntryZoneLow = ai.ShortEntryLow
		plan.EntryZoneHigh = ai.ShortEntryHigh
		plan.StopLoss = ai.ShortSL
		plan.TakeProfit = ai.ShortTP
		plan.ShortEntryLow = ai.ShortEntryLow
		plan.ShortEntryHigh = ai.ShortEntryHigh
		plan.ShortStopLoss = ai.ShortSL
		plan.ShortTakeProfit = ai.ShortTP
		aiRangeLow = ai.ShortTP[len(ai.ShortTP)-1]
		aiRangeHigh = ai.ShortEntryHigh
	} else {
		return nil, fmt.Errorf("AI未产出有效入场")
	}

	if aiRangeHigh <= 0 || aiRangeLow <= 0 || aiRangeHigh <= aiRangeLow {
		plan.RangeLow = px * 0.99
		plan.RangeHigh = px * 1.01
	} else {
		plan.RangeLow = math.Min(aiRangeLow, px)
		plan.RangeHigh = math.Max(aiRangeHigh, px)
	}
	if plan.RangeHigh-plan.RangeLow < px*0.002 {
		plan.RangeLow = px * 0.99
		plan.RangeHigh = px * 1.01
	}

	// 基于 AI 的区间调用 clampPlan
	clampPlan(plan, px)
	return plan, nil
}

// mathBuild 纯数学构建（原 buildPlan 逻辑，AI 失败时的 fallback）
func (p *Planner) mathBuild(symbol string, pxPlan, atr float64,
	depth *DepthProfile, macro *MacroContext, news *NewsDigest, funding float64) *RangePlan {

	e := p.evo
	plan := &RangePlan{
		Symbol:      symbol,
		Regime:      string(macro.Regime),
		LeverageCap: 125,
		ATR14:       atr,
		AiModel:     "math",
	}

	plan.RangeLow = pxPlan - atr*e.AtRMult
	plan.RangeHigh = pxPlan + atr*e.AtRMult
	if depth != nil && depth.SupportPrice > 0 && depth.ResistancePrice > depth.SupportPrice {
		plan.RangeLow = math.Min(plan.RangeLow, depth.SupportPrice)
		plan.RangeHigh = math.Max(plan.RangeHigh, depth.ResistancePrice)
	}

	switch {
	case macro.Regime == RegimeTrendUp:
		plan.Bias = "long"
		plan.EntryZoneLow = pxPlan - atr*e.AtRMult*0.3
		plan.EntryZoneHigh = pxPlan - atr*e.EntryMargin
		plan.StopLoss = plan.EntryZoneLow - atr*e.StopOffset
		plan.TakeProfit = []float64{pxPlan + atr*e.TpMult, pxPlan + atr*e.TpMult*2}
		plan.LongEntryLow = plan.EntryZoneLow
		plan.LongEntryHigh = plan.EntryZoneHigh
		plan.LongStopLoss = plan.StopLoss
		plan.LongTakeProfit = plan.TakeProfit

	case macro.Regime == RegimeTrendDown:
		plan.Bias = "short"
		plan.EntryZoneLow = pxPlan + atr*e.EntryMargin
		plan.EntryZoneHigh = pxPlan + atr*e.AtRMult*0.3
		plan.StopLoss = plan.EntryZoneHigh + atr*e.StopOffset
		plan.TakeProfit = []float64{pxPlan - atr*e.TpMult, pxPlan - atr*e.TpMult*2}
		plan.ShortEntryLow = plan.EntryZoneLow
		plan.ShortEntryHigh = plan.EntryZoneHigh
		plan.ShortStopLoss = plan.StopLoss
		plan.ShortTakeProfit = plan.TakeProfit

	default:
		plan.Bias = "both"
		midRange := (plan.RangeLow + plan.RangeHigh) / 2
		plan.LongEntryLow = plan.RangeLow
		plan.LongEntryHigh = plan.RangeLow + atr*e.EntryMargin
		plan.LongStopLoss = plan.RangeLow - atr*e.StopOffset
		plan.LongTakeProfit = []float64{midRange, plan.RangeHigh}
		plan.ShortEntryLow = plan.RangeHigh - atr*e.EntryMargin
		plan.ShortEntryHigh = plan.RangeHigh
		plan.ShortStopLoss = plan.RangeHigh + atr*e.StopOffset
		plan.ShortTakeProfit = []float64{midRange, plan.RangeLow}
		plan.EntryZoneLow = plan.LongEntryLow
		plan.EntryZoneHigh = plan.ShortEntryHigh
		plan.StopLoss = plan.LongStopLoss
		plan.TakeProfit = plan.LongTakeProfit
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
	clampPlanRisk(plan)
	// both 方向：同时修正 long 和 short 的入场带
	if plan.Bias == "both" {
		minZone := px * 0.005 // 最低入场带宽 = 0.5%
		if plan.LongEntryLow > px || plan.LongEntryLow <= 0 {
			plan.LongEntryLow = px * 0.97
		}
		if plan.LongEntryHigh-plan.LongEntryLow < minZone {
			plan.LongEntryHigh = plan.LongEntryLow + minZone
			if plan.LongEntryHigh > px {
				plan.LongEntryHigh = px * 0.995
			}
		}
		if plan.ShortEntryLow < px || plan.ShortEntryLow <= 0 {
			plan.ShortEntryLow = px * 1.005
		}
		if plan.ShortEntryHigh-plan.ShortEntryLow < minZone {
			plan.ShortEntryHigh = plan.ShortEntryLow + minZone
			if plan.ShortEntryHigh < px {
				plan.ShortEntryHigh = px * 1.03
			}
		}
		if plan.LongStopLoss >= plan.LongEntryLow || plan.LongStopLoss <= 0 {
			plan.LongStopLoss = plan.LongEntryLow * 0.97
		}
		if plan.ShortStopLoss <= plan.ShortEntryHigh || plan.ShortStopLoss <= 0 {
			plan.ShortStopLoss = plan.ShortEntryHigh * 1.03
		}
		plan.EntryZoneLow = plan.LongEntryLow
		plan.EntryZoneHigh = plan.ShortEntryHigh
		plan.StopLoss = plan.LongStopLoss
		return
	}
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

func clampPlanRisk(plan *RangePlan) {
	const (
		defaultLeverageCap = 125
		minLeverage        = 1
		maxLeverage        = 125
		minPositionPct     = 0.001
		maxPositionPct     = 0.05
	)
	if plan.LeverageCap <= 0 || plan.LeverageCap > maxLeverage {
		plan.LeverageCap = defaultLeverageCap
	}
	if plan.Leverage <= 0 {
		plan.Leverage = minLeverage
	}
	if plan.Leverage > plan.LeverageCap {
		plan.Leverage = plan.LeverageCap
	}
	if plan.Leverage > maxLeverage {
		plan.Leverage = maxLeverage
	}
	if plan.PositionSizePct <= 0 {
		plan.PositionSizePct = minPositionPct
	}
	if plan.PositionSizePct > maxPositionPct {
		plan.PositionSizePct = maxPositionPct
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
