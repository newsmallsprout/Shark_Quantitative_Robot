package planning

import "testing"

func TestParseReplanSymbolAcceptsJSONPayload(t *testing.T) {
	got := parseReplanSymbol(`{"symbol":"BTC/USDT","reason":"loss_streak"}`)

	if got != "BTC/USDT" {
		t.Fatalf("expected BTC/USDT, got %q", got)
	}
}

func TestParseReplanSymbolRejectsUnsupportedSymbol(t *testing.T) {
	got := parseReplanSymbol(`{"symbol":"DOGE/USDT","reason":"loss_streak"}`)

	if got != "" {
		t.Fatalf("expected unsupported symbol rejected, got %q", got)
	}
}
