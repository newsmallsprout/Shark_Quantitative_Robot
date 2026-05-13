package planning

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"net/http"
	"os"
	"strings"
	"time"
)

// PlanContext AI 分析所需的全部上下文
type PlanContext struct {
	Symbol        string
	Regime        string
	Price         float64
	ATR14         float64
	FundingRate   float64
	RangeLow      float64
	RangeHigh     float64
	SupportStr    float64
	ResistanceStr float64
	NewsRisk      int
	NewsFlags     []string
	WinRate       float64 // 近期胜率 0-1
	AvgPnl        float64 // 近期平均盈亏%
	TVInsights    string  // TradingView 学习成果
	BreakoutDir   string  // "up"/"down"/"" — 突破方向提示
}

// AIPlan AI 返回的交易计划
type AIPlan struct {
	LongEntryLow    float64   `json:"long_entry_low"`
	LongEntryHigh   float64   `json:"long_entry_high"`
	ShortEntryLow   float64   `json:"short_entry_low"`
	ShortEntryHigh  float64   `json:"short_entry_high"`
	LongSL          float64   `json:"long_sl"`
	ShortSL         float64   `json:"short_sl"`
	LongTP          []float64 `json:"long_tp"`
	ShortTP         []float64 `json:"short_tp"`
	PositionSizePct float64   `json:"position_size_pct"`
	Leverage        int       `json:"leverage"`
	PyramidPrices   []float64 `json:"pyramid_prices"`
	CutLossPct      float64   `json:"cut_loss_pct"`
	Rationale       string    `json:"rationale"`
	Confidence      float64   `json:"confidence"`
}

// ── 主入口：三步 AI 调用 ──

func (p *Planner) aiGeneratePlan(ctx context.Context, symbol string, pc *PlanContext) (*AIPlan, error) {
	start := time.Now()
	model := "math"
	confidence := 0.0

	// Step 1: Qwen 基础分析
	qwenResult := p.callQwen(ctx, pc)
	hasQwen := qwenResult != nil

	// Step 2: DeepSeek 主力分析（传入 Qwen 结果做参考）
	dsResult := p.callDeepSeek(ctx, pc, qwenResult)
	if dsResult == nil {
		log.Printf("[AI] %s DeepSeek 失败，降级到数学模式", symbol)
		return nil, fmt.Errorf("deepseek failed")
	}
	model = "deepseek"
	confidence = dsResult.Confidence

	// 置信度过滤：低于55分的AI计划不可信，降级到数学模式
	if confidence < 55 {
		log.Printf("[AI] %s DeepSeek conf=%.0f 过低，降级到数学模式", symbol, confidence)
		return nil, fmt.Errorf("low confidence %.0f", confidence)
	}

	// Step 3: 火山(豆包) 情绪验证
	if huoshanResult := p.callHuoshan(ctx, pc, dsResult); huoshanResult != nil {
		// 方向一致 → 提置信度
		if huoshanResult.Confidence > 0 && confidence > 0 {
			confidence = (confidence + huoshanResult.Confidence) / 2
		}
		_ = hasQwen
	}

	dsResult.Confidence = confidence
	log.Printf("[AI] ✅ %s model=%s conf=%.0f (%.1fs)", symbol, model, confidence, time.Since(start).Seconds())
	return dsResult, nil
}

// ── Qwen 基础分析（便宜快速） ──

func (p *Planner) callQwen(ctx context.Context, pc *PlanContext) *AIPlan {
	key := os.Getenv("QWEN_KEY")
	if key == "" {
		return nil
	}

	prompt := fmt.Sprintf(qwenPrompt, pc.Symbol, pc.Price, pc.ATR14, pc.FundingRate,
		pc.Regime, pc.RangeLow, pc.RangeHigh, pc.SupportStr, pc.ResistanceStr,
		pc.NewsRisk, pc.WinRate, pc.AvgPnl)

	body, err := p.llmCall(ctx, "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
		key, "qwen-plus", prompt, 300, 10*time.Second)
	if err != nil {
		return nil
	}

	var result AIPlan
	if err := json.Unmarshal(body, &result); err != nil {
		return nil
	}
	normalizeAIPlanConfidence(&result)
	return &result
}

// ── DeepSeek 主力分析 ──

func (p *Planner) callDeepSeek(ctx context.Context, pc *PlanContext, qwenHint *AIPlan) *AIPlan {
	key := os.Getenv("DEEPSEEK_API_KEY")
	if key == "" {
		return nil
	}

	qwenNote := ""
	if qwenHint != nil {
		qwenNote = fmt.Sprintf("Qwen初步判断: 方向偏%s 置信%.0f",
			qwenBias(qwenHint), qwenHint.Confidence)
	}

	prompt := fmt.Sprintf(deepseekPrompt, pc.Symbol, pc.Price, pc.ATR14, pc.FundingRate,
		pc.Regime, pc.RangeLow, pc.RangeHigh,
		pc.BreakoutDir, pc.SupportStr, pc.ResistanceStr,
		pc.NewsRisk, strings.Join(pc.NewsFlags, ","),
		pc.WinRate*100, pc.AvgPnl, qwenNote, pc.TVInsights)

	body, err := p.llmCall(ctx, "https://api.deepseek.com/v1/chat/completions",
		key, "deepseek-chat", prompt, 800, 20*time.Second)
	if err != nil {
		return nil
	}
	return parseAIPlan(body)
}

// ── 火山(豆包) 情绪验证 ──

func (p *Planner) callHuoshan(ctx context.Context, pc *PlanContext, dsResult *AIPlan) *AIPlan {
	key := os.Getenv("VOLC_KEY")
	if key == "" {
		return nil
	}

	dir := "多"
	if dsResult.LongEntryLow <= 0 {
		dir = "空"
	}

	prompt := fmt.Sprintf(huoshanPrompt, pc.Symbol, pc.Price, pc.FundingRate,
		pc.Regime, dir, dsResult.PositionSizePct, dsResult.Leverage,
		pc.NewsRisk, dsResult.Rationale)

	body, err := p.llmCall(ctx, "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
		key, "doubao-seed-2-0-pro-251215", prompt, 200, 10*time.Second)
	if err != nil {
		return nil
	}

	var result struct {
		Confidence float64 `json:"confidence"`
		Agree      bool    `json:"agree"`
		Note       string  `json:"note"`
	}
	if err := json.Unmarshal(body, &result); err != nil {
		return nil
	}
	return &AIPlan{Confidence: normalizeAIConfidence(result.Confidence)}
}

// ── LLM 通用调用 ──

func (p *Planner) llmCall(ctx context.Context, url, key, model, prompt string, maxTokens int, timeout time.Duration) ([]byte, error) {
	type msg struct {
		Role    string `json:"role"`
		Content string `json:"content"`
	}
	payload := map[string]interface{}{
		"model":       model,
		"messages":    []msg{{Role: "user", Content: prompt}},
		"temperature": 0.3,
		"max_tokens":  maxTokens,
	}
	payloadBytes, _ := json.Marshal(payload)

	client := &http.Client{Timeout: timeout}
	req, err := http.NewRequestWithContext(ctx, "POST", url, bytes.NewReader(payloadBytes))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Authorization", "Bearer "+key)

	resp, err := client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	if resp.StatusCode != 200 {
		return nil, fmt.Errorf("llm status %d", resp.StatusCode)
	}

	var result struct {
		Choices []struct {
			Message struct {
				Content string `json:"content"`
			} `json:"message"`
		} `json:"choices"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&result); err != nil {
		return nil, err
	}
	if len(result.Choices) == 0 {
		return nil, fmt.Errorf("empty response")
	}

	content := result.Choices[0].Message.Content
	// 提取 JSON 块
	content = extractJSON(content)
	return []byte(content), nil
}

func extractJSON(s string) string {
	// 尝试 ```json ... ``` 代码块
	if i := strings.Index(s, "```json"); i >= 0 {
		s = s[i+7:]
		if j := strings.Index(s, "```"); j >= 0 {
			return strings.TrimSpace(s[:j])
		}
	}
	// 尝试 { 开头
	if i := strings.Index(s, "{"); i >= 0 {
		s = s[i:]
		if j := strings.LastIndex(s, "}"); j >= 0 {
			return s[:j+1]
		}
	}
	return s
}

func parseAIPlan(body []byte) *AIPlan {
	var plan AIPlan
	if err := json.Unmarshal(body, &plan); err != nil {
		return nil
	}
	normalizeAIPlanConfidence(&plan)
	return &plan
}

func normalizeAIPlanConfidence(plan *AIPlan) {
	if plan == nil {
		return
	}
	plan.Confidence = normalizeAIConfidence(plan.Confidence)
	if plan.Confidence <= 0 && hasActionableAIPlan(plan) {
		plan.Confidence = 65
	}
}

func normalizeAIConfidence(confidence float64) float64 {
	if confidence > 0 && confidence <= 1 {
		return confidence * 100
	}
	if confidence < 0 {
		return 0
	}
	if confidence > 100 {
		return 100
	}
	return confidence
}

func hasActionableAIPlan(plan *AIPlan) bool {
	if plan == nil {
		return false
	}
	hasLong := plan.LongEntryLow > 0 && plan.LongEntryHigh > plan.LongEntryLow && plan.LongSL > 0 && len(plan.LongTP) > 0
	hasShort := plan.ShortEntryLow > 0 && plan.ShortEntryHigh > plan.ShortEntryLow && plan.ShortSL > 0 && len(plan.ShortTP) > 0
	return hasLong || hasShort
}

func qwenBias(p *AIPlan) string {
	if p.LongEntryLow > 0 && p.ShortEntryLow <= 0 {
		return "多"
	}
	if p.ShortEntryLow > 0 && p.LongEntryLow <= 0 {
		return "空"
	}
	return "震荡"
}

// ── Prompt 模板 ──

const qwenPrompt = `你是加密货币短线交易分析师。快速判断 %s 的短线方向。

数据: 价格%.2f ATR%.2f 费率%.4f%% 行情%s 区间[%.0f,%.0f] 支撑强度%.0f%% 阻力强度%.0f%% 新闻风险%d/2
近期: 胜率%.0f%% 均盈亏%.1f%%

输出纯JSON（不要markdown代码块）:
{"long_entry_low":0,"long_entry_high":0,"short_entry_low":0,"short_entry_high":0,"long_sl":0,"short_sl":0,"long_tp":[],"short_tp":[],"position_size_pct":0,"leverage":0,"pyramid_prices":[],"cut_loss_pct":0,"rationale":"","confidence":65}

规则: 超短线(3-10分钟持仓)。小保证金高杠杆，首个止盈要快；止损给足ATR呼吸空间，避免刚开仓被噪音扫掉。confidence必须是0-100百分制数字，禁止用0-1小数或布尔0/1。long_entry_low < long_entry_high必须成立。只输出JSON，不要其他文字。`

const deepseekPrompt = `你是顶级加密货币超短线交易员。为 %s 生成完整交易计划。

## 市场数据
- 现价: %.2f | ATR14: %.2f | 资金费率: %.4f%%
- 行情: %s | 区间: [%.0f, %.0f]
- 突破方向: %s | 订单簿支撑强度: %.0f%% | 阻力强度: %.0f%%
- 新闻风险: %d/2 | 风险标志: %s

## 交易质量
- 近期胜率: %.0f%% | 均盈亏: %.1f%%

## 参考
%s

## TradingView 社区观点
%s

## 要求（微利多仓高杠杆纪律 — 违反任何一条=废单）
- 杠杆80-125x（小保证金承载高杠杆，ATR越大杠杆越低）别超出交易所对币对的上限
- 仓位0.8-1.8%%（position_size_pct用小数 0.015=1.5%%，别给蚊子仓）
- ⚠️ 首个止盈0.5-1.2%%价格幅度，第二止盈1.0-2.0%%，开仓要快止盈也要快
- ⚠️ 止损必须比首个止盈更宽，至少 ≥ ATR14/价格×100×1.8，避免开仓即被损
- ⚠️ 震荡行情(both)多空入场带必须各占区间30%%以上，绝不能让entry_low > entry_high
- ⚠️ long_entry_low < long_entry_high 必须成立！short_entry_low < short_entry_high 必须成立！
- pyramid_prices只给顺势盈利加仓点，禁止亏损摊平；cut_loss_pct给绝对割肉线%%（用小数 0.03=3%%）
- rationale用中文（20-40字），针对该币对当前行情做具体分析

输出纯JSON（不要markdown代码块，不要额外文字）:
{"long_entry_low":0,"long_entry_high":0,"short_entry_low":0,"short_entry_high":0,"long_sl":0,"short_sl":0,"long_tp":[],"short_tp":[],"position_size_pct":0,"leverage":0,"pyramid_prices":[],"cut_loss_pct":0,"rationale":"<30字中文>","confidence":65}

confidence必须是0-100百分制数字，禁止用0-1小数或布尔0/1。`

const huoshanPrompt = `验证交易计划。%s 现价%.2f 费率%.4f%% 行情%s。计划方向:%s 仓位%.0f%% 杠杆%dx 风险%d。计划摘要:%s。输出纯JSON:{"agree":true/false,"confidence":0-100,"note":"<10字>"}`
