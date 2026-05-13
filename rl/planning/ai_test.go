package planning

import "testing"

func TestParseAIPlanNormalizesFractionalConfidence(t *testing.T) {
	body := []byte(`{
		"long_entry_low": 99,
		"long_entry_high": 100,
		"long_sl": 96,
		"long_tp": [101, 102],
		"position_size_pct": 0.012,
		"leverage": 80,
		"confidence": 0.82,
		"rationale": "fractional confidence"
	}`)

	plan := parseAIPlan(body)

	if plan == nil {
		t.Fatal("expected plan")
	}
	if plan.Confidence != 82 {
		t.Fatalf("expected fractional confidence normalized to 82, got %.2f", plan.Confidence)
	}
}

func TestNormalizeAIConfidenceKeepsPercentConfidence(t *testing.T) {
	if got := normalizeAIConfidence(68); got != 68 {
		t.Fatalf("expected percent confidence unchanged, got %.2f", got)
	}
}

func TestParseAIPlanDefaultsMissingConfidenceWhenPlanIsActionable(t *testing.T) {
	body := []byte(`{
		"short_entry_low": 101,
		"short_entry_high": 102,
		"short_sl": 104,
		"short_tp": [100, 99],
		"position_size_pct": 0.012,
		"leverage": 80,
		"confidence": 0,
		"rationale": "actionable plan but model emitted zero confidence"
	}`)

	plan := parseAIPlan(body)

	if plan == nil {
		t.Fatal("expected plan")
	}
	if plan.Confidence < 55 {
		t.Fatalf("expected actionable plan to get usable default confidence, got %.2f", plan.Confidence)
	}
}
