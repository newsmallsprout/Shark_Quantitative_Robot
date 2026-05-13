package planning

import "testing"

func TestClampPlanCapsAILeverageAndPositionSize(t *testing.T) {
	plan := &RangePlan{
		Bias:            "long",
		RangeLow:        99,
		RangeHigh:       105,
		EntryZoneLow:    99,
		EntryZoneHigh:   101,
		StopLoss:        98,
		TakeProfit:      []float64{104},
		LeverageCap:     125,
		Leverage:        300,
		PositionSizePct: 0.20,
	}

	clampPlan(plan, 100)

	if plan.Leverage != 125 {
		t.Fatalf("expected leverage capped at 125, got %d", plan.Leverage)
	}
	if plan.PositionSizePct != 0.05 {
		t.Fatalf("expected position size capped at 0.05, got %.4f", plan.PositionSizePct)
	}
}

func TestClampPlanDefaultsInvalidLeverageAndPositionSize(t *testing.T) {
	plan := &RangePlan{
		Bias:          "long",
		RangeLow:      99,
		RangeHigh:     105,
		EntryZoneLow:  99,
		EntryZoneHigh: 101,
		StopLoss:      98,
		TakeProfit:    []float64{104},
		LeverageCap:   0,
		Leverage:      0,
	}

	clampPlan(plan, 100)

	if plan.LeverageCap != 125 {
		t.Fatalf("expected default leverage cap 125, got %d", plan.LeverageCap)
	}
	if plan.Leverage != 1 {
		t.Fatalf("expected default leverage 1, got %d", plan.Leverage)
	}
	if plan.PositionSizePct != 0.001 {
		t.Fatalf("expected minimum position size 0.001, got %.4f", plan.PositionSizePct)
	}
}
