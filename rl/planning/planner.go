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

	// 自进化 — 每币对独立
	evo      map[string]*EvoState
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
		evo:     make(map[string]*EvoState),
		symbols: symbols,
	}
}

// getEvo returns per-symbol EvoState, creating a default if none exists.
// Each symbol evolves independently based on its own trade quality.
func (p *Planner) getEvo(symbol string) *EvoState {
	if e, ok := p.evo[symbol]; ok && e != nil {
		return e
	}
	e := DefaultEvo()
	p.evo[symbol] = e
	return e
}

// SetTVFn sets the TradingView insights provider (closure from main.go)
func (p *Planner) SetTVFn(fn func(string) string) { p.tvFn = fn }

func (p *Planner) State() PlanningState { return p.state }

func (p *Planner) publishPlanningStatus(ctx context.Context, phase, symbol, message string, done, total int, active bool) {
	if p == nil || p.rdb == nil {
		return
	}
	payload := map[string]interface{}{
		"active":  active,
		"phase":   phase,
		"symbol":  symbol,
		"message": message,
		"done":    done,
		"total":   total,
		"ts":      time.Now().Unix(),
	}
	data, err := json.Marshal(payload)
	if err != nil {
		return
	}
	_ = p.rdb.Set(ctx, "shark:planning:status", data, 10*time.Minute).Err()
	_ = p.rdb.Publish(ctx, "shark:planning:status", string(data)).Err()
}

// Bootstrap → 全量数据拉取 → 生成所有币对计划
func (p *Planner) Bootstrap(ctx context.Context) error {
    p.refreshSymbolsFromRedis(ctx)
    log.Printf("[Planning] BOOTSTRAP — 拉取全量数据，覆盖%d个币对...", len(p.symbols))
	p.publishPlanningStatus(ctx, "bootstrap", "", "启动全量计划生成，开仓会等新计划落地", 0, len(p.symbols), true)

	p.fetchPrices(ctx)

	digest := p.news.Fetch(ctx)

	bootstrapOrder := priorityOrder(p.symbols)
	successCount := 0
	for i, sym := range bootstrapOrder {
		p.publishPlanningStatus(ctx, "bootstrap", sym, fmt.Sprintf("正在生成 %s 计划 (%d/%d)", sym, i+1, len(bootstrapOrder)), i, len(bootstrapOrder), true)
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
	p.publishPlanningStatus(ctx, "live", "", fmt.Sprintf("全量计划完成 %d/%d，恢复按计划开仓", successCount, len(p.symbols)), successCount, len(p.symbols), false)
	log.Printf("[Planning] BOOTSTRAP → LIVE (%d/%d 成功) · 各币对独立进化参数见日志",
		successCount, len(p.symbols))
	return nil
}

func (p *Planner) Plan(ctx context.Context, symbol string, digest *NewsDigest) error {
	start := time.Now()
	p.state = StatePlanning
	p.publishPlanningStatus(ctx, "planning", symbol, fmt.Sprintf("%s 正在拉宏观/深度/AI计划", symbol), 0, len(p.symbols), true)

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
	if p.prices[symbol] > 0 {
		plan.PlanPrice = p.prices[symbol]
	}
	plan.FuseThresholdPct = calcFuseThreshold(macro.ATR14, p.prices[symbol])
	plan.GeneratedAt = time.Now().Unix()
	plan.ValidUntil = plan.GeneratedAt + 1800
	plan.EvoGen = p.getEvo(symbol).Generation
	if digest.RiskLevel >= 2 {
		plan.State = string(StatePaused)
		p.state = StatePaused
		log.Printf("[Planning] ⚠️ 新闻风险爆表 → PAUSED (flags=%v)", digest.Flags)
	} else if fs, ok := p.fuse[symbol]; ok && fs != nil && fs.Triggered {
		plan.State = string(StatePaused)
	} else {
		plan.State = string(StateLive)
	}

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

	p.plans[symbol] = plan
	p.lastPlan = time.Now().Unix()

	// 每币对独立自进化：累计计划数达到阈值后评估调优
	p.evolveSymbol(ctx, symbol)

	log.Printf("[Planning] ✅ %s: regime=%s bias=%s range=[%.0f,%.0f] entry=[%.0f,%.0f] sl=%.0f evo=gen%d (%.1fs)",
		symbol, plan.Regime, plan.Bias,
		plan.RangeLow, plan.RangeHigh,
		plan.EntryZoneLow, plan.EntryZoneHigh,
		plan.StopLoss, plan.EvoGen, time.Since(start).Seconds())
	p.publishPlanningStatus(ctx, "planning", symbol, fmt.Sprintf("%s 计划已落地，用时%.1fs", symbol, time.Since(start).Seconds()), 0, len(p.symbols), true)
	return nil
}

// PlanAll — 批量为所有币对生成计划；完成后评估并自进化
func (p *Planner) refreshSymbolsFromRedis(ctx context.Context) {
    if p.rdb == nil {
        return
    }
    raw, err := p.rdb.Get(ctx, "shark:high_vol_alts").Result()
    if err == nil && raw != "" {
        var alts []string
        if err := json.Unmarshal([]byte(raw), &alts); err == nil {
            // Merge FocusSymbols (stable coins) and alts
            newSymbols := make([]string, 0)
            seen := make(map[string]bool)
            for _, s := range FocusSymbols {
                newSymbols = append(newSymbols, s)
                seen[s] = true
            }
            for _, s := range alts {
                if !seen[s] {
                    newSymbols = append(newSymbols, s)
                    seen[s] = true
                }
            }
            p.symbols = newSymbols
        }
    }
}

func (p *Planner) PlanAll(ctx context.Context) {
    p.refreshSymbolsFromRedis(ctx)
    log.Printf("[Planning] 开始全量计划更新 (%d个币对)...", len(p.symbols))
	p.publishPlanningStatus(ctx, "full_replan", "", "正在全量重做计划，开仓会等新计划落地", 0, len(p.symbols), true)

	p.fetchPrices(ctx)

	digest := p.news.Fetch(ctx)

	bootstrapOrder := priorityOrder(p.symbols)
	successCount := 0
	for i, sym := range bootstrapOrder {
		p.publishPlanningStatus(ctx, "full_replan", sym, fmt.Sprintf("正在重做 %s 计划 (%d/%d)", sym, i+1, len(bootstrapOrder)), i, len(bootstrapOrder), true)
		if err := p.Plan(ctx, sym, digest); err != nil {
			log.Printf("[Planning] 计划生成失败(%s): %v", sym, err)
		} else {
			successCount++
		}
	}

	// 所有币对计划完成（自进化已由各币对 Plan() 独立完成）
	log.Printf("[Planning] 全量计划完成 (%d/%d)", successCount, len(bootstrapOrder))
	p.publishPlanningStatus(ctx, "live", "", fmt.Sprintf("全量计划完成 %d/%d，恢复按计划开仓", successCount, len(bootstrapOrder)), successCount, len(bootstrapOrder), false)
}

// ── 自进化（每币对独立）──

// evolveSymbol 单币对自进化：过滤该币对成交，评估质量，自适应调优参数。
// 每积累6个计划触发一次进化（6×30min = 3小时）。
func (p *Planner) evolveSymbol(ctx context.Context, symbol string) {
	e := p.getEvo(symbol)
	e.PlansGenerated++
	if e.PlansGenerated < 6 || e.PlansGenerated%6 != 0 {
		return // 未达到进化周期
	}

	trades := p.readRecentTrades(ctx, 50)
	// 过滤该币对成交
	var symTrades []TradeSnapshot
	for _, t := range trades {
		if t.Symbol == symbol {
			symTrades = append(symTrades, t)
		}
	}
	if len(symTrades) < 3 {
		return // 该币对数据不足，暂不进化
	}

	var wins, stops int
	var totalPnl float64
	for _, t := range symTrades {
		if t.Pnl > 0 {
			wins++
		}
		if t.ExitType == "sl" {
			stops++
		}
		totalPnl += t.PnlPct
	}

	e.WinRate = float64(wins) / float64(len(symTrades))
	e.StopHitRate = float64(stops) / float64(len(symTrades))
	e.AvgPnlPct = totalPnl / float64(len(symTrades))
	e.Generation++
	e.LastEval = time.Now().Unix()

	// 自适应调优
	prevAtR := e.AtRMult
	prevStop := e.StopOffset
	prevTp := e.TpMult

	// 止损率高 → 放宽止损
	if e.StopHitRate > 0.5 {
		e.StopOffset = clamp(e.StopOffset+0.2, 0.3, 2.0)
		e.AtRMult = clamp(e.AtRMult*1.1, 1.0, 4.0)
	}
	// 胜率低 → 收紧入场带
	if e.WinRate < 0.4 {
		e.EntryMargin = clamp(e.EntryMargin*0.8, 0.1, 1.0)
	}
	// 胜率高 → 放大止盈
	if e.WinRate > 0.6 {
		e.TpMult = clamp(e.TpMult*1.15, 1.0, 5.0)
	}
	// 平均亏损 → 收窄区间
	if e.AvgPnlPct < -0.5 {
		e.AtRMult = clamp(e.AtRMult*0.9, 1.0, 4.0)
	}

	log.Printf("[Evo] %s gen=%d trades=%d win=%.0f%% stop=%.0f%% avgPnl=%.1f%% adjustments: ATR×%.1f→%.1f STOP×%.1f→%.1f TP×%.1f→%.1f entry×%.1f",
		symbol, e.Generation, len(symTrades), e.WinRate*100, e.StopHitRate*100, e.AvgPnlPct,
		prevAtR, e.AtRMult, prevStop, e.StopOffset, prevTp, e.TpMult, e.EntryMargin)
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
// 【核心逻辑说明】
// 1. 优先尝试调用 AI (DeepSeek) 模型生成交易计划。
// 2. 将当前价格、宏观因子、订单簿等环境数据构建为 prompt，交给 AI 判断。
// 3. AI 返回的结果会经过数学维度的交叉验证 (如：止损空间是否合理)。
// 4. 如果 AI 调用失败或验证不通过，则降级为纯数学指标生成的兜底计划。
// 5. 计划中包含: 方向(Bias), 入场带(EntryZone), 止损(StopLoss), 止盈(TakeProfit), 杠杆(Leverage)。
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
		LeverageCap:        35,
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
	widenBothSideStops(plan, atr)
	clampPlan(plan, px)
	enforceSwingHeavyPlan(plan, atr)
	return plan, nil
}

// mathBuild 纯数学构建（原 buildPlan 逻辑，AI 失败时的 fallback）
func (p *Planner) mathBuild(symbol string, pxPlan, atr float64,
	depth *DepthProfile, macro *MacroContext, news *NewsDigest, funding float64) *RangePlan {

	e := p.getEvo(symbol)
	plan := &RangePlan{
		Symbol:      symbol,
		Regime:      string(macro.Regime),
		LeverageCap: 35,
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
	case macro.Regime == RegimeTrendUp || macro.Regime == RegimeBreakoutUp || macro.Regime == RegimeSlowGrindUp:
		plan.Bias = "long"
		plan.EntryZoneLow = pxPlan - atr*e.AtRMult*0.3
		plan.EntryZoneHigh = pxPlan - atr*e.EntryMargin
		plan.StopLoss = plan.EntryZoneLow - atr*e.StopOffset
		plan.TakeProfit = []float64{pxPlan + atr*e.TpMult, pxPlan + atr*e.TpMult*2}
		plan.LongEntryLow = plan.EntryZoneLow
		plan.LongEntryHigh = plan.EntryZoneHigh
		plan.LongStopLoss = plan.StopLoss
		plan.LongTakeProfit = plan.TakeProfit

	case macro.Regime == RegimeTrendDown || macro.Regime == RegimeBreakoutDown || macro.Regime == RegimeSlowGrindDown || macro.Regime == RegimeBleedDown:
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

	applyRegimePlaybook(plan, macro, pxPlan, atr)
	clampPlan(plan, pxPlan)
	enforceSwingHeavyPlan(plan, atr)
	return plan
}

func applyRegimePlaybook(plan *RangePlan, macro *MacroContext, px, atr float64) {
	if plan == nil || macro == nil {
		return
	}
	plan.LeverageCap = 35
	switch macro.Regime {
	case RegimeTrendUp, RegimeTrendDown:
		plan.PositionSizePct = 0.18
		plan.Leverage = 32
		plan.CutLossPct = 0.08
	case RegimeBreakoutUp, RegimeBreakoutDown:
		plan.PositionSizePct = 0.14
		plan.Leverage = 30
		plan.CutLossPct = 0.07
	case RegimeSlowGrindUp, RegimeSlowGrindDown:
		plan.PositionSizePct = 0.12
		plan.Leverage = 26
		plan.CutLossPct = 0.06
	case RegimeBleedDown:
		plan.PositionSizePct = 0.10
		plan.Leverage = 24
		plan.CutLossPct = 0.06
	case RegimeChoppy:
		plan.PositionSizePct = 0.08
		plan.Leverage = 20
		plan.CutLossPct = 0.06
	case RegimeDead:
		plan.PositionSizePct = 0.02
		plan.Leverage = 1
		plan.CutLossPct = 0.03
	default:
		plan.PositionSizePct = 0.10
		plan.Leverage = 25
		plan.CutLossPct = 0.06
	}
	applyQuickProfitWideStop(plan, px, atr)
}

func applyQuickProfitWideStop(plan *RangePlan, px, atr float64) {
	if plan == nil || px <= 0 || atr <= 0 {
		return
	}
	firstTP := atr * 2.5
	secondTP := atr * 4.5
	stopDistance := atr * 3.5
	if plan.Bias == "long" {
		entry := (plan.EntryZoneLow + plan.EntryZoneHigh) / 2
		if entry <= 0 {
			entry = px
		}
		plan.StopLoss = entry - stopDistance
		plan.TakeProfit = []float64{entry + firstTP, entry + secondTP}
		plan.LongStopLoss = plan.StopLoss
		plan.LongTakeProfit = plan.TakeProfit
		return
	}
	if plan.Bias == "short" {
		entry := (plan.EntryZoneLow + plan.EntryZoneHigh) / 2
		if entry <= 0 {
			entry = px
		}
		plan.StopLoss = entry + stopDistance
		plan.TakeProfit = []float64{entry - firstTP, entry - secondTP}
		plan.ShortStopLoss = plan.StopLoss
		plan.ShortTakeProfit = plan.TakeProfit
		return
	}
	if plan.Bias == "both" {
		longEntry := (plan.LongEntryLow + plan.LongEntryHigh) / 2
		if longEntry <= 0 {
			longEntry = px * 0.995
		}
		shortEntry := (plan.ShortEntryLow + plan.ShortEntryHigh) / 2
		if shortEntry <= 0 {
			shortEntry = px * 1.005
		}
		plan.LongTakeProfit = []float64{longEntry + firstTP, longEntry + secondTP}
		plan.ShortTakeProfit = []float64{shortEntry - firstTP, shortEntry - secondTP}
		widenBothSideStops(plan, atr)
	}
}

func widenBothSideStops(plan *RangePlan, atr float64) {
	if plan == nil || plan.Bias != "both" || atr <= 0 {
		return
	}
	stopDistance := atr * 4.0
	longEntry := (plan.LongEntryLow + plan.LongEntryHigh) / 2
	if longEntry > 0 && (plan.LongStopLoss <= 0 || longEntry-plan.LongStopLoss < stopDistance) {
		plan.LongStopLoss = longEntry - stopDistance
	}
	shortEntry := (plan.ShortEntryLow + plan.ShortEntryHigh) / 2
	if shortEntry > 0 && (plan.ShortStopLoss <= 0 || plan.ShortStopLoss-shortEntry < stopDistance) {
		plan.ShortStopLoss = shortEntry + stopDistance
	}
	plan.StopLoss = plan.LongStopLoss
}

func enforceSwingHeavyPlan(plan *RangePlan, atr float64) {
	if plan == nil || atr <= 0 {
		return
	}
	plan.LeverageCap = 35
	if plan.Leverage <= 0 {
		plan.Leverage = 25
	}
	if plan.Leverage > 35 {
		plan.Leverage = 35
	}
	if plan.Leverage < 20 {
		plan.Leverage = 20
	}
	if plan.PositionSizePct < 0.10 {
		plan.PositionSizePct = 0.10
	}
	if plan.PositionSizePct > 0.18 {
		plan.PositionSizePct = 0.18
	}
	if plan.CutLossPct < 0.06 {
		plan.CutLossPct = 0.06
	}
	if plan.CutLossPct > 0.10 {
		plan.CutLossPct = 0.10
	}

	firstTPPct := 0.50
	secondTPPct := 1.00
	stopPct := 0.50
	ref := plan.PlanPrice
	if ref <= 0 && plan.RangeLow > 0 && plan.RangeHigh > 0 {
		ref = (plan.RangeLow + plan.RangeHigh) / 2
	}
	if ref <= 0 {
		ref = math.Max(plan.EntryZoneLow, plan.EntryZoneHigh)
	}
	halfRange := math.Max(atr*6.0, ref*0.035)
	if plan.Bias == "long" {
		entry := (plan.EntryZoneLow + plan.EntryZoneHigh) / 2
		if entry <= 0 {
			return
		}
		stopDistance := leveragedPriceDistance(entry, plan.Leverage, stopPct)
		firstTP := leveragedPriceDistance(entry, plan.Leverage, firstTPPct)
		secondTP := leveragedPriceDistance(entry, plan.Leverage, secondTPPct)
		plan.StopLoss = entry - stopDistance
		plan.LongStopLoss = plan.StopLoss
		plan.TakeProfit = []float64{entry + firstTP, entry + secondTP}
		plan.LongTakeProfit = plan.TakeProfit
		ensureSwingPlanRange(plan, entry, halfRange)
		return
	}
	if plan.Bias == "short" {
		entry := (plan.EntryZoneLow + plan.EntryZoneHigh) / 2
		if entry <= 0 {
			return
		}
		stopDistance := leveragedPriceDistance(entry, plan.Leverage, stopPct)
		firstTP := leveragedPriceDistance(entry, plan.Leverage, firstTPPct)
		secondTP := leveragedPriceDistance(entry, plan.Leverage, secondTPPct)
		plan.StopLoss = entry + stopDistance
		plan.ShortStopLoss = plan.StopLoss
		plan.TakeProfit = []float64{entry - firstTP, entry - secondTP}
		plan.ShortTakeProfit = plan.TakeProfit
		ensureSwingPlanRange(plan, entry, halfRange)
		return
	}
	if plan.Bias == "both" {
		longEntry := (plan.LongEntryLow + plan.LongEntryHigh) / 2
		if longEntry > 0 {
			longStopDistance := leveragedPriceDistance(longEntry, plan.Leverage, stopPct)
			longFirstTP := leveragedPriceDistance(longEntry, plan.Leverage, firstTPPct)
			longSecondTP := leveragedPriceDistance(longEntry, plan.Leverage, secondTPPct)
			plan.LongStopLoss = longEntry - longStopDistance
			plan.LongTakeProfit = []float64{longEntry + longFirstTP, longEntry + longSecondTP}
			plan.StopLoss = plan.LongStopLoss
			plan.TakeProfit = plan.LongTakeProfit
		}
		shortEntry := (plan.ShortEntryLow + plan.ShortEntryHigh) / 2
		if shortEntry > 0 {
			shortStopDistance := leveragedPriceDistance(shortEntry, plan.Leverage, stopPct)
			shortFirstTP := leveragedPriceDistance(shortEntry, plan.Leverage, firstTPPct)
			shortSecondTP := leveragedPriceDistance(shortEntry, plan.Leverage, secondTPPct)
			plan.ShortStopLoss = shortEntry + shortStopDistance
			plan.ShortTakeProfit = []float64{shortEntry - shortFirstTP, shortEntry - shortSecondTP}
		}
		center := ref
		if center <= 0 && longEntry > 0 && shortEntry > 0 {
			center = (longEntry + shortEntry) / 2
		}
		ensureSwingPlanRange(plan, center, halfRange)
	}
}

func leveragedPriceDistance(entry float64, leverage int, pnlPct float64) float64 {
	lev := leverage
	if lev <= 0 {
		lev = 25
	}
	return entry * pnlPct / float64(lev)
}

func ensureSwingPlanRange(plan *RangePlan, center, halfRange float64) {
	if plan == nil || center <= 0 || halfRange <= 0 {
		return
	}
	low := center - halfRange
	if low <= 0 {
		low = center * 0.5
	}
	high := center + halfRange
	if plan.RangeLow <= 0 || plan.RangeLow > low {
		plan.RangeLow = low
	}
	if plan.RangeHigh <= 0 || plan.RangeHigh < high {
		plan.RangeHigh = high
	}
	if plan.StopLoss > 0 {
		plan.RangeLow = math.Min(plan.RangeLow, plan.StopLoss)
		plan.RangeHigh = math.Max(plan.RangeHigh, plan.StopLoss)
	}
	if plan.LongStopLoss > 0 {
		plan.RangeLow = math.Min(plan.RangeLow, plan.LongStopLoss)
	}
	if plan.ShortStopLoss > 0 {
		plan.RangeHigh = math.Max(plan.RangeHigh, plan.ShortStopLoss)
	}
	for _, tp := range plan.TakeProfit {
		if tp > 0 {
			plan.RangeLow = math.Min(plan.RangeLow, tp)
			plan.RangeHigh = math.Max(plan.RangeHigh, tp)
		}
	}
	for _, tp := range plan.LongTakeProfit {
		if tp > 0 {
			plan.RangeHigh = math.Max(plan.RangeHigh, tp)
		}
	}
	for _, tp := range plan.ShortTakeProfit {
		if tp > 0 {
			plan.RangeLow = math.Min(plan.RangeLow, tp)
		}
	}
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
		rangeLow := minPositive(plan.RangeLow, plan.LongEntryLow, plan.LongEntryHigh, px*0.995)
		rangeHigh := math.Max(math.Max(plan.RangeHigh, plan.ShortEntryLow), math.Max(plan.ShortEntryHigh, px*1.005))
		if len(plan.LongTakeProfit) > 0 {
			rangeHigh = math.Max(rangeHigh, plan.LongTakeProfit[len(plan.LongTakeProfit)-1])
		}
		if len(plan.ShortTakeProfit) > 0 {
			rangeLow = minPositive(rangeLow, plan.ShortTakeProfit[len(plan.ShortTakeProfit)-1])
		}
		if rangeLow > 0 && rangeHigh > rangeLow {
			plan.RangeLow = rangeLow
			plan.RangeHigh = rangeHigh
		}
		plan.EntryZoneLow = plan.LongEntryLow
		plan.EntryZoneHigh = plan.ShortEntryHigh
		widenBothSideStops(plan, plan.ATR14)
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

func minPositive(values ...float64) float64 {
	minVal := 0.0
	for _, v := range values {
		if v <= 0 {
			continue
		}
		if minVal == 0 || v < minVal {
			minVal = v
		}
	}
	return minVal
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
	if plan.Bias == "both" {
		longEntry := (plan.LongEntryLow + plan.LongEntryHigh) / 2
		if plan.LongStopLoss >= longEntry {
			return fmt.Errorf("both long: sl(%.2f) >= entry(%.2f)", plan.LongStopLoss, longEntry)
		}
		for _, tp := range plan.LongTakeProfit {
			if tp <= longEntry {
				return fmt.Errorf("both long: tp(%.2f) <= entry(%.2f)", tp, longEntry)
			}
		}
		shortEntry := (plan.ShortEntryLow + plan.ShortEntryHigh) / 2
		if plan.ShortStopLoss <= shortEntry {
			return fmt.Errorf("both short: sl(%.2f) <= entry(%.2f)", plan.ShortStopLoss, shortEntry)
		}
		for _, tp := range plan.ShortTakeProfit {
			if tp >= shortEntry {
				return fmt.Errorf("both short: tp(%.2f) >= entry(%.2f)", tp, shortEntry)
			}
		}
		return nil
	}
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

// calcFuseThreshold 根据币对ATR波动率计算自适应熔断阈值。
// BTC(~1.8%日ATR)→3.0%, ETH(~2.5%)→3.75%, SOL(~9%)→12.0%封顶。
func calcFuseThreshold(atr, price float64) float64 {
	if price <= 0 || atr <= 0 {
		return 3.0
	}
	atrPct := atr / price         // 日ATR占价格百分比
	threshold := 3.0 * (atrPct / 0.02) // 以BTC(2%日ATR)为基准缩放
	return clamp(threshold, 3.0, 12.0)
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
