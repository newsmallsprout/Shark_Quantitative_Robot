package rl

import (
	"strings"
	"testing"
)

func TestBuildTVInsightExtractsStructuredPlanFields(t *testing.T) {
	ideas := []TVIdea{
		{
			Title:       "BTCUSDT bullish continuation",
			Symbol:      "BTCUSDT",
			Direction:   "long",
			Timeframe:   "4H",
			Likes:       40,
			Description: "Buy pullback above support 80200. Target 82000 and 90000. Stop loss below 79100. Risk only 1-2% per trade.",
		},
		{
			Title:       "ETH short rejection",
			Symbol:      "ETHUSDT",
			Direction:   "short",
			Timeframe:   "2H",
			Likes:       5,
			Description: "Resistance 2400, target 2200.",
		},
	}

	insight := buildTVInsight("BTC/USDT", ideas)

	if insight.Symbol != "BTC/USDT" {
		t.Fatalf("expected BTC symbol, got %s", insight.Symbol)
	}
	if insight.Bias != "long" {
		t.Fatalf("expected long bias, got %s", insight.Bias)
	}
	if insight.Support != 80200 {
		t.Fatalf("expected support 80200, got %.0f", insight.Support)
	}
	if len(insight.Targets) != 2 || insight.Targets[0] != 82000 || insight.Targets[1] != 90000 {
		t.Fatalf("unexpected targets: %v", insight.Targets)
	}
	if insight.Stop != 79100 {
		t.Fatalf("expected stop 79100, got %.0f", insight.Stop)
	}
	if !strings.Contains(insight.Summary(), "long") {
		t.Fatalf("expected summary to include bias, got %q", insight.Summary())
	}
}
