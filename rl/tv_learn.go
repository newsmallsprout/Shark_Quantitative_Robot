package rl

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// ── TradingView Knowledge Ingestion Engine ──
// 爬取 TradingView 公开策略/观点/指标 → 提取可执行模式 → 喂入知识库

// TVIdea represents a parsed TradingView trading idea
type TVIdea struct {
	Title       string   `json:"title"`
	Author      string   `json:"author"`
	Symbol      string   `json:"symbol"`
	Direction   string   `json:"direction"`   // "long", "short", "neutral"
	Indicators  []string `json:"indicators"`  // e.g. ["RSI", "MACD", "EMA"]
	Timeframe   string   `json:"timeframe"`
	Description string   `json:"description"`
	Likes       int      `json:"likes"`
	URL         string   `json:"url"`
	ScrapedAt   int64    `json:"scraped_at"`
}

// StrategyPattern extracted from TradingView ideas
type StrategyPattern struct {
	Name        string   `json:"name"`
	Direction   string   `json:"direction"`
	Indicators  []string `json:"indicators"`
	EntryRule   string   `json:"entry_rule"`
	ExitRule    string   `json:"exit_rule"`
	StopRule    string   `json:"stop_rule"`
	Timeframe   string   `json:"timeframe"`
	Confidence  float64  `json:"confidence"` // based on likes/engagement
	Source      string   `json:"source"`     // URL of original idea
	LastUpdated int64    `json:"last_updated"`
}

// TVScraper handles TradingView data ingestion
type TVScraper struct {
	client      *http.Client
	ideas       []TVIdea
	patterns    []StrategyPattern
	mu          sync.RWMutex
	lastFetch   int64
	fetchCount  int
	totalPatterns int
}

func NewTVScraper() *TVScraper {
	return &TVScraper{
		client: &http.Client{
			Timeout: 30 * time.Second,
			Transport: &http.Transport{
				MaxIdleConns:    5,
				IdleConnTimeout: 60 * time.Second,
			},
		},
	}
}

// FetchIdeas scrapes TradingView popular ideas
func (tv *TVScraper) FetchIdeas() ([]TVIdea, error) {
	// TradingView 热门观点 API（公开，无需认证）
	url := "https://www.tradingview.com/ideas/ideas-stream/?stream=hot"
	
	req, err := http.NewRequest("GET", url, nil)
	if err != nil {
		return nil, err
	}
	req.Header.Set("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
	req.Header.Set("Accept", "application/json, text/html")
	req.Header.Set("Referer", "https://www.tradingview.com/ideas/")

	resp, err := tv.client.Do(req)
	if err != nil {
		return nil, fmt.Errorf("fetch ideas: %w", err)
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	var ideas []TVIdea
	// Try JSON API response
	if err := json.Unmarshal(body, &ideas); err == nil && len(ideas) > 0 {
		tv.mu.Lock()
		tv.ideas = ideas
		tv.lastFetch = time.Now().Unix()
		tv.fetchCount++
		tv.mu.Unlock()
		return ideas, nil
	}

	// Fallback: parse HTML for idea cards
	ideas = parseTVHTML(string(body))
	if len(ideas) == 0 {
		return nil, fmt.Errorf("no ideas found")
	}

	tv.mu.Lock()
	tv.ideas = ideas
	tv.lastFetch = time.Now().Unix()
	tv.fetchCount++
	tv.mu.Unlock()

	return ideas, nil
}

// ExtractPatterns converts TradingView ideas into executable strategy patterns
func (tv *TVScraper) ExtractPatterns() []StrategyPattern {
	tv.mu.RLock()
	ideas := tv.ideas
	tv.mu.RUnlock()

	if len(ideas) == 0 {
		return nil
	}

	var patterns []StrategyPattern
	seen := make(map[string]bool)

	for _, idea := range ideas {
		if idea.Likes < 3 {
			continue // 跳过低质量观点
		}

		// 提取指标
		indicators := extractIndicators(idea.Description)
		if len(indicators) == 0 {
			indicators = idea.Indicators
		}

		// 提取入场/离场规则
		entry, exit, stop := extractRules(idea.Description, idea.Direction)

		// 去重
		key := fmt.Sprintf("%s_%s_%s", idea.Direction, strings.Join(indicators, ","), idea.Timeframe)
		if seen[key] {
			continue
		}
		seen[key] = true

		confidence := float64(idea.Likes) / 50.0
		if confidence > 1.0 {
			confidence = 1.0
		}
		if confidence < 0.1 {
			confidence = 0.1
		}

		patterns = append(patterns, StrategyPattern{
			Name:       idea.Title,
			Direction:  idea.Direction,
			Indicators: indicators,
			EntryRule:  entry,
			ExitRule:   exit,
			StopRule:   stop,
			Timeframe:  idea.Timeframe,
			Confidence: confidence,
			Source:     idea.URL,
			LastUpdated: time.Now().Unix(),
		})
	}

	tv.mu.Lock()
	tv.patterns = patterns
	tv.totalPatterns += len(patterns)
	tv.mu.Unlock()

	log.Printf("[TV] 提取 %d 个策略模式 (总计%d)", len(patterns), tv.totalPatterns)
	return patterns
}

// FetchPopularScripts scrapes popular Pine Script indicators/strategies
func (tv *TVScraper) FetchPopularScripts() ([]StrategyPattern, error) {
	url := "https://www.tradingview.com/pine-script-docs/en/v5/index.html"
	
	req, _ := http.NewRequest("GET", url, nil)
	req.Header.Set("User-Agent", "Mozilla/5.0")
	resp, err := tv.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, _ := io.ReadAll(resp.Body)
	// Parse Pine Script documentation for indicator patterns
	patterns := parsePineScriptDocs(string(body))

	tv.mu.Lock()
	tv.patterns = append(tv.patterns, patterns...)
	tv.totalPatterns += len(patterns)
	tv.mu.Unlock()

	return patterns, nil
}

// FeedKnowledgeBase updates the KnowledgeBase with learned patterns
func (tv *TVScraper) FeedKnowledgeBase(kb *KnowledgeBase) {
	tv.mu.RLock()
	patterns := tv.patterns
	tv.mu.RUnlock()

	if len(patterns) == 0 {
		return
	}

	// Convert StrategyPattern to TradePattern for KnowledgeBase
	var tradePatterns []TradePattern
	for _, sp := range patterns {
		rsi := 50.0
		vol := 0.02
		trend := 0.0

		// Infer RSI from entry rules
		if strings.Contains(strings.ToLower(sp.EntryRule), "oversold") {
			rsi = 30
		} else if strings.Contains(strings.ToLower(sp.EntryRule), "overbought") {
			rsi = 70
		}

		// Infer from timeframe
		switch sp.Timeframe {
		case "1m", "5m":
			vol = 0.03
		case "1h", "4h":
			vol = 0.01
		}

		tradePatterns = append(tradePatterns, TradePattern{
			Symbol:     sp.Name,
			Side:       sp.Direction,
			RSIEntry:   rsi,
			VolEntry:   vol,
			TrendEntry: trend,
			PnLPct:     sp.Confidence,
			Count:      int(sp.Confidence * 100),
		})
	}

	kb.UpdatePatterns(tradePatterns)
	log.Printf("[TV] 知识库已更新: %d 个模式", len(tradePatterns))
}

// Stats returns scraper statistics
func (tv *TVScraper) Stats() map[string]interface{} {
	tv.mu.RLock()
	defer tv.mu.RUnlock()
	return map[string]interface{}{
		"ideas_fetched":   len(tv.ideas),
		"patterns_learned": len(tv.patterns),
		"total_patterns":   tv.totalPatterns,
		"fetch_count":      tv.fetchCount,
		"last_fetch":       tv.lastFetch,
	}
}

// ── HTML/Text Parsing Helpers ──

func parseTVHTML(html string) []TVIdea {
	var ideas []TVIdea
	// 提取标题
	titleRe := regexp.MustCompile(`<a[^>]*class="[^"]*title[^"]*"[^>]*>([^<]+)</a>`)
	titleMatches := titleRe.FindAllStringSubmatch(html, -1)

	// 提取描述
	descRe := regexp.MustCompile(`<div[^>]*class="[^"]*description[^"]*"[^>]*>([^<]+)</div>`)
	descMatches := descRe.FindAllStringSubmatch(html, -1)

	// 提取作者
	authorRe := regexp.MustCompile(`<span[^>]*class="[^"]*username[^"]*"[^>]*>@?(\w+)</span>`)
	authorMatches := authorRe.FindAllStringSubmatch(html, -1)

	// 提取点赞数
	likesRe := regexp.MustCompile(`<span[^>]*class="[^"]*likes[^"]*"[^>]*>(\d+)</span>`)
	likesMatches := likesRe.FindAllStringSubmatch(html, -1)

	for i, t := range titleMatches {
		if i >= len(titleMatches) {
			break
		}
		idea := TVIdea{
			Title:       cleanHTML(t[1]),
			Description: "",
			Direction:   detectDirection(t[1]),
			ScrapedAt:   time.Now().Unix(),
		}
		if i < len(descMatches) {
			idea.Description = cleanHTML(descMatches[i][1])
		}
		if i < len(authorMatches) {
			idea.Author = authorMatches[i][1]
		}
		if i < len(likesMatches) {
			l, _ := strconv.Atoi(likesMatches[i][1])
			idea.Likes = l
		}
		idea.Indicators = extractIndicators(idea.Description)
		ideas = append(ideas, idea)
	}
	return ideas
}

func parsePineScriptDocs(html string) []StrategyPattern {
	var patterns []StrategyPattern
	// Extract indicator names from Pine Script docs
	indicatorRe := regexp.MustCompile(`(?:ta\.|request\.)(\w+)`)
	matches := indicatorRe.FindAllStringSubmatch(html, -1)
	seen := make(map[string]bool)
	for _, m := range matches {
		name := m[1]
		if seen[name] || len(name) < 3 {
			continue
		}
		seen[name] = true
		patterns = append(patterns, StrategyPattern{
			Name:       fmt.Sprintf("PineScript: %s", name),
			Indicators: []string{name},
			Confidence: 0.3,
			LastUpdated: time.Now().Unix(),
		})
	}
	return patterns
}

// extractIndicators finds indicator names mentioned in text
func extractIndicators(text string) []string {
	text = strings.ToUpper(text)
	known := []string{
		"RSI", "MACD", "EMA", "SMA", "BB", "ATR", "ADX",
		"VOLUME", "STOCH", "ICHIMOKU", "SAR", "CCI",
		"SUPERTREND", "VWAP", "OBV", "MFI",
		"FIBONACCI", "PIVOT", "MOVING AVERAGE",
	}
	var found []string
	for _, k := range known {
		if strings.Contains(text, k) {
			found = append(found, k)
		}
	}
	if len(found) == 0 {
		found = append(found, "PRICE_ACTION")
	}
	return found
}

// extractRules extracts entry/exit/stop rules from description
func extractRules(desc, direction string) (entry, exit, stop string) {
	desc = strings.ToLower(desc)

	// Entry rules
	switch {
	case strings.Contains(desc, "breakout") || strings.Contains(desc, "突破"):
		entry = "breakout_above_resistance" 
		if direction == "short" {
			entry = "breakdown_below_support"
		}
	case strings.Contains(desc, "oversold") || strings.Contains(desc, "超卖"):
		entry = "rsi_oversold_bounce"
	case strings.Contains(desc, "overbought") || strings.Contains(desc, "超买"):
		entry = "rsi_overbought_reversal"
	case strings.Contains(desc, "pullback") || strings.Contains(desc, "回调"):
		entry = "trend_pullback"
	case strings.Contains(desc, "crossover") || strings.Contains(desc, "金叉"):
		entry = "ma_crossover"
	default:
		entry = "trend_following"
	}

	// Exit rules
	if strings.Contains(desc, "target") || strings.Contains(desc, "目标") {
		exit = "fixed_target"
	} else if strings.Contains(desc, "trailing") || strings.Contains(desc, "移动") {
		exit = "trailing_stop"
	} else {
		exit = "atr_multiple"
	}

	// Stop rules
	if strings.Contains(desc, "swing low") || strings.Contains(desc, "低点") {
		stop = "swing_low"
	} else if strings.Contains(desc, "atr") {
		stop = "atr_stop"
	} else {
		stop = "percentage_stop"
	}

	return
}

// detectDirection determines trade direction from title text
func detectDirection(text string) string {
	text = strings.ToLower(text)
	longWords := []string{"long", "buy", "bullish", "看涨", "做多", "买入", "上涨"}
	shortWords := []string{"short", "sell", "bearish", "看跌", "做空", "卖出", "下跌"}
	
	for _, w := range longWords {
		if strings.Contains(text, w) {
			return "long"
		}
	}
	for _, w := range shortWords {
		if strings.Contains(text, w) {
			return "short"
		}
	}
	return "neutral"
}

func cleanHTML(s string) string {
	s = regexp.MustCompile(`<[^>]+>`).ReplaceAllString(s, "")
	s = regexp.MustCompile(`&[a-z]+;`).ReplaceAllString(s, " ")
	s = regexp.MustCompile(`\s+`).ReplaceAllString(s, " ")
	return strings.TrimSpace(s)
}

// ── Periodic Knowledge Refresh ──

// StartTVLearning starts the periodic TradingView learning loop
func (tv *TVScraper) StartTVLearning(kb *KnowledgeBase, interval time.Duration) {
	go func() {
		log.Println("[TV] 知识学习引擎启动 (每30分钟抓取TradingView)")
		
		// Initial fetch
		tv.learnOnce(kb)
		
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			tv.learnOnce(kb)
		}
	}()
}

func (tv *TVScraper) learnOnce(kb *KnowledgeBase) {
	ideas, err := tv.FetchIdeas()
	if err != nil {
		log.Printf("[TV] 抓取失败: %v", err)
		return
	}
	log.Printf("[TV] 抓取 %d 条TradingView观点", len(ideas))
	
	tv.ExtractPatterns()
	tv.FeedKnowledgeBase(kb)
	
	// Also try popular scripts
	scripts, err := tv.FetchPopularScripts()
	if err == nil && len(scripts) > 0 {
		tv.FeedKnowledgeBase(kb)
		log.Printf("[TV] 学习 %d 个Pine Script指标", len(scripts))
	}
}
