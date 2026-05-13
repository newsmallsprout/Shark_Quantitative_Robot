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

func TestClassifyRegimeDetectsBreakoutAndBleed(t *testing.T) {
	up, upBreakout := classifyRegime(0.004, 55, 110, 100, 110, 0.006)
	if up != RegimeBreakoutUp || upBreakout != "up" {
		t.Fatalf("expected breakout up, got regime=%s breakout=%s", up, upBreakout)
	}

	down, downBreakout := classifyRegime(-0.004, 55, 90, 90, 110, -0.006)
	if down != RegimeBreakoutDown || downBreakout != "down" {
		t.Fatalf("expected breakout down, got regime=%s breakout=%s", down, downBreakout)
	}

	bleed, bleedBreakout := classifyRegime(-0.003, 35, 96, 90, 110, -0.001)
	if bleed != RegimeBleedDown || bleedBreakout != "" {
		t.Fatalf("expected bleed down, got regime=%s breakout=%s", bleed, bleedBreakout)
	}
}

func TestApplyRegimePlaybookSetsRiskForMathPlans(t *testing.T) {
	plan := &RangePlan{Bias: "short"}
	macro := &MacroContext{Regime: RegimeBleedDown}

	applyRegimePlaybook(plan, macro, 100, 2)

	if plan.PositionSizePct <= 0 || plan.PositionSizePct > 0.01 {
		t.Fatalf("expected defensive bleed position size, got %.4f", plan.PositionSizePct)
	}
	if plan.Leverage < 60 || plan.Leverage > 95 {
		t.Fatalf("expected high-leverage bleed plan, got %d", plan.Leverage)
	}
	if plan.CutLossPct <= 0 {
		t.Fatalf("expected cut loss pct to be set")
	}
}

func TestPlaybookUsesMicroPositionHighLeverageWideStopQuickProfit(t *testing.T) {
	plan := &RangePlan{
		Bias:          "long",
		EntryZoneLow:  99,
		EntryZoneHigh: 100,
		StopLoss:      98,
		TakeProfit:    []float64{106},
	}
	macro := &MacroContext{Regime: RegimeBreakoutUp}

	applyRegimePlaybook(plan, macro, 100, 2)

	entry := (plan.EntryZoneLow + plan.EntryZoneHigh) / 2
	firstTPDistance := plan.TakeProfit[0] - entry
	stopDistance := entry - plan.StopLoss
	if plan.PositionSizePct <= 0 || plan.PositionSizePct > 0.006 {
		t.Fatalf("expected micro position <=0.6%%, got %.4f", plan.PositionSizePct)
	}
	if plan.Leverage < 65 {
		t.Fatalf("expected high leverage >=65x, got %d", plan.Leverage)
	}
	if firstTPDistance <= 0 || firstTPDistance > 2*0.9 {
		t.Fatalf("expected quick first TP within 0.9 ATR, got %.4f", firstTPDistance)
	}
	if stopDistance < firstTPDistance*1.5 {
		t.Fatalf("expected stop to be wider than quick TP, stop=%.4f tp=%.4f", stopDistance, firstTPDistance)
	}
}

func TestAuditRejectsInvalidBothSideStops(t *testing.T) {
	planner := &Planner{}
	plan := &RangePlan{
		Bias:            "both",
		LongEntryLow:    95,
		LongEntryHigh:   98,
		LongStopLoss:    99,
		LongTakeProfit:  []float64{100},
		ShortEntryLow:   102,
		ShortEntryHigh:  105,
		ShortStopLoss:   101,
		ShortTakeProfit: []float64{100},
	}

	if err := planner.audit(plan); err == nil {
		t.Fatal("expected invalid both-side stops to be rejected")
	}
}
