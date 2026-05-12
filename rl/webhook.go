package rl

import (
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"sync"
	"time"
)

// ── TradingView Webhook & Knowledge Ingestion ──

// TVAlert represents a TradingView Pine Script alert payload
type TVAlert struct {
	Symbol    string             `json:"symbol"`
	Timeframe string             `json:"timeframe"`
	Price     float64            `json:"price"`
	Signal    string             `json:"signal"`    // "long", "short", "neutral"
	Strength  float64            `json:"strength"`  // 0-100
	Indicators map[string]float64 `json:"indicators"` // e.g. {"rsi": 35, "macd": 0.5}
	Message   string             `json:"message"`
	Timestamp int64              `json:"timestamp"`
}

// KnowledgeBase stores learned patterns and market insights
type KnowledgeBase struct {
	mu          sync.RWMutex
	Signals     []TVAlert           `json:"signals"`
	Patterns    []TradePattern      `json:"patterns"`
	Sentiment   map[string]float64  `json:"sentiment"`   // symbol → sentiment score (-1 to 1)
	SignalCount int                 `json:"signal_count"`
	LastUpdate  int64               `json:"last_update"`
}

func NewKnowledgeBase() *KnowledgeBase {
	return &KnowledgeBase{
		Signals:   make([]TVAlert, 0, 1000),
		Patterns:  make([]TradePattern, 0),
		Sentiment: make(map[string]float64),
	}
}

// IngestAlert processes a TradingView alert into the knowledge base
func (kb *KnowledgeBase) IngestAlert(alert TVAlert) {
	kb.mu.Lock()
	defer kb.mu.Unlock()

	kb.Signals = append(kb.Signals, alert)
	if len(kb.Signals) > 1000 {
		kb.Signals = kb.Signals[len(kb.Signals)-1000:]
	}
	kb.SignalCount++
	kb.LastUpdate = time.Now().Unix()

	// update sentiment
	sentimentDelta := 0.0
	switch alert.Signal {
	case "long":
		sentimentDelta = alert.Strength / 100.0
	case "short":
		sentimentDelta = -alert.Strength / 100.0
	}
	current := kb.Sentiment[alert.Symbol]
	kb.Sentiment[alert.Symbol] = current*0.9 + sentimentDelta*0.1 // EMA
}

// GetSentiment returns the current sentiment for a symbol
func (kb *KnowledgeBase) GetSentiment(symbol string) float64 {
	kb.mu.RLock()
	defer kb.mu.RUnlock()
	return kb.Sentiment[symbol]
}

// GetRecentSignals returns the last N alerts
func (kb *KnowledgeBase) GetRecentSignals(n int) []TVAlert {
	kb.mu.RLock()
	defer kb.mu.RUnlock()
	if len(kb.Signals) <= n {
		result := make([]TVAlert, len(kb.Signals))
		copy(result, kb.Signals)
		return result
	}
	return kb.Signals[len(kb.Signals)-n:]
}

// UpdatePatterns replaces the pattern library with new learned patterns
func (kb *KnowledgeBase) UpdatePatterns(patterns []TradePattern) {
	kb.mu.Lock()
	defer kb.mu.Unlock()
	kb.Patterns = patterns
	kb.LastUpdate = time.Now().Unix()
}

// GetPatternMatch returns the best matching pattern for current market conditions
func (kb *KnowledgeBase) GetPatternMatch(side string, rsi, vol, trend float64) *TradePattern {
	kb.mu.RLock()
	defer kb.mu.RUnlock()

	var best *TradePattern
	bestScore := -1e9
	for i := range kb.Patterns {
		p := &kb.Patterns[i]
		if p.Side != side {
			continue
		}
		// similarity score: closer indicators = higher match
		rsiDiff := 1.0 - abs(p.RSIEntry-rsi)/100.0
		volDiff := 1.0 - abs(p.VolEntry-vol)/mathMax(p.VolEntry, vol, 0.01)
		trendDiff := 1.0 - abs(p.TrendEntry-trend)/mathMax(abs(p.TrendEntry), abs(trend), 0.001)
		score := (rsiDiff + volDiff + trendDiff) / 3.0 * float64(p.Count)
		if score > bestScore {
			bestScore = score
			best = p
		}
	}
	return best
}

func abs(x float64) float64 {
	if x < 0 {
		return -x
	}
	return x
}

func mathMax(a, b, fallback float64) float64 {
	if a > b {
		if a > fallback {
			return a
		}
	} else {
		if b > fallback {
			return b
		}
	}
	return fallback
}

// ── HTTP Handlers ──

type WebhookServer struct {
	kb      *KnowledgeBase
	ga      *GAPopulation
	agent   *DQNAgent
	metrics map[string]interface{}
	mu      sync.RWMutex
}

func NewWebhookServer(kb *KnowledgeBase, ga *GAPopulation, agent *DQNAgent) *WebhookServer {
	return &WebhookServer{
		kb:      kb,
		ga:      ga,
		agent:   agent,
		metrics: make(map[string]interface{}),
	}
}

// POST /tv/alert — TradingView webhook endpoint
func (s *WebhookServer) HandleTVAlert(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		http.Error(w, "method not allowed", http.StatusMethodNotAllowed)
		return
	}

	body, err := io.ReadAll(r.Body)
	if err != nil {
		http.Error(w, "read error", http.StatusBadRequest)
		return
	}

	var alert TVAlert
	if err := json.Unmarshal(body, &alert); err != nil {
		// try TV raw format: {"ticker": "...", "price": ..., "message": "..."}
		var raw map[string]interface{}
		if err2 := json.Unmarshal(body, &raw); err2 != nil {
			http.Error(w, "invalid JSON", http.StatusBadRequest)
			return
		}
		// convert raw to alert
		alert = TVAlert{
			Symbol:    strVal(raw, "ticker"),
			Timeframe: strVal(raw, "timeframe"),
			Price:     floatVal(raw, "price"),
			Signal:    strVal(raw, "signal"),
			Strength:  floatVal(raw, "strength"),
			Message:   strVal(raw, "message"),
			Timestamp: time.Now().Unix(),
		}
		if alert.Signal == "" {
			// infer from message
			msg := alert.Message
			if contains(msg, "buy") || contains(msg, "long") {
				alert.Signal = "long"
			} else if contains(msg, "sell") || contains(msg, "short") {
				alert.Signal = "short"
			} else {
				alert.Signal = "neutral"
			}
		}
		if alert.Strength == 0 {
			alert.Strength = 50
		}
	}

	s.kb.IngestAlert(alert)
	log.Printf("[TV] alert ingested: %s %s strength=%.0f", alert.Symbol, alert.Signal, alert.Strength)
	w.WriteHeader(http.StatusOK)
	json.NewEncoder(w).Encode(map[string]string{"status": "ok"})
}

// GET /rl/status — current RL engine status
func (s *WebhookServer) HandleStatus(w http.ResponseWriter, r *http.Request) {
	s.mu.RLock()
	defer s.mu.RUnlock()

	resp := map[string]interface{}{
		"signals_received": s.kb.SignalCount,
		"patterns_learned": len(s.kb.Patterns),
		"ga_generation":    s.ga.Generation,
		"best_fitness":     s.ga.BestFitness,
		"epsilon":          s.agent.Epsilon,
		"sentiment":        s.kb.Sentiment,
		"metrics":          s.metrics,
	}
	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(resp)
}

// GET /rl/patterns — learned trading patterns
func (s *WebhookServer) HandlePatterns(w http.ResponseWriter, r *http.Request) {
	s.kb.mu.RLock()
	patterns := make([]TradePattern, len(s.kb.Patterns))
	copy(patterns, s.kb.Patterns)
	s.kb.mu.RUnlock()

	w.Header().Set("Content-Type", "application/json")
	json.NewEncoder(w).Encode(map[string]interface{}{
		"patterns": patterns,
		"count":    len(patterns),
	})
}

func (s *WebhookServer) UpdateMetrics(m map[string]interface{}) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.metrics = m
}

func strVal(m map[string]interface{}, key string) string {
	if v, ok := m[key]; ok {
		return fmt.Sprintf("%v", v)
	}
	return ""
}

func floatVal(m map[string]interface{}, key string) float64 {
	if v, ok := m[key]; ok {
		switch val := v.(type) {
		case float64:
			return val
		case int:
			return float64(val)
		case json.Number:
			f, _ := val.Float64()
			return f
		}
	}
	return 0
}

func contains(s, substr string) bool {
	return len(s) >= len(substr) && searchSubstring(s, substr)
}

func searchSubstring(s, substr string) bool {
	for i := 0; i <= len(s)-len(substr); i++ {
		if s[i:i+len(substr)] == substr {
			return true
		}
	}
	return false
}
