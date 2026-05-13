package main

import "testing"

func TestValidateTradeCmdRejectsInvalidPaperSide(t *testing.T) {
	cmd := TradeCmd{Symbol: "BTC/USDT", Side: "hold", Action: "open", Mode: "paper"}

	if err := validateTradeCmd(cmd); err == nil {
		t.Fatal("expected invalid side to be rejected")
	}
}

func TestValidateTradeCmdAcceptsWellFormedPaperOpen(t *testing.T) {
	cmd := TradeCmd{Symbol: "BTC/USDT", Side: "long", Action: "open", Mode: "paper"}

	if err := validateTradeCmd(cmd); err != nil {
		t.Fatalf("expected valid paper command, got %v", err)
	}
}
