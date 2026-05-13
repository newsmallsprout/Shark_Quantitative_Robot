package main

import "testing"

func TestValidateTradeCmdRejectsMalformedLiveOpen(t *testing.T) {
	cmd := TradeCmd{
		Symbol:   "BTC/USDT",
		Side:     "long",
		Action:   "open",
		Mode:     "live",
		Size:     0,
		Leverage: 50,
	}

	if err := validateTradeCmd(cmd); err == nil {
		t.Fatal("expected zero-size live open to be rejected")
	}
}

func TestValidateTradeCmdRejectsLiveCommandWithoutTokenWhenConfigured(t *testing.T) {
	t.Setenv("SHARK_ORDER_TOKEN", "secret-token")
	cmd := TradeCmd{
		Symbol:   "BTC/USDT",
		Side:     "long",
		Action:   "open",
		Mode:     "live",
		Size:     1,
		Leverage: 50,
	}

	if err := validateTradeCmd(cmd); err == nil {
		t.Fatal("expected missing live command token to be rejected")
	}
}

func TestValidateTradeCmdAcceptsWellFormedLiveOpen(t *testing.T) {
	t.Setenv("SHARK_ORDER_TOKEN", "secret-token")
	cmd := TradeCmd{
		Symbol:   "BTC/USDT",
		Side:     "long",
		Action:   "open",
		Mode:     "live",
		Size:     1,
		Leverage: 50,
		Token:    "secret-token",
	}

	if err := validateTradeCmd(cmd); err != nil {
		t.Fatalf("expected valid command, got %v", err)
	}
}

func TestSplitReduceSizesPreservesTotalAcrossTargets(t *testing.T) {
	sizes := splitReduceSizes(5, 3)

	if len(sizes) != 3 {
		t.Fatalf("expected 3 target sizes, got %v", sizes)
	}
	total := 0
	for _, size := range sizes {
		if size <= 0 {
			t.Fatalf("expected positive split sizes, got %v", sizes)
		}
		total += size
	}
	if total != 5 {
		t.Fatalf("expected total size 5, got %d from %v", total, sizes)
	}
}
