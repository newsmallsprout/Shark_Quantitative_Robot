package rl

import (
	"math"
	"math/rand"
	"sort"
)

// ── Genetic Algorithm: Strategy Population Evolution ──
// Evolves complete strategy PARAMETERS (not just margin_mult)
// Each individual = a full trading strategy configuration

type StrategyGene struct {
	// Position sizing
	MarginPct    float64 `json:"margin_pct"`    // 0.01 - 0.30
	MaxPositions int     `json:"max_positions"` // 2 - 8

	// Entry conditions
	RSIThreshold float64 `json:"rsi_threshold"` // 20 - 80
	VolMinPct    float64 `json:"vol_min_pct"`   // 0.001 - 0.05
	TrendPeriod  int     `json:"trend_period"`  // 5 - 50

	// Exit conditions
	StopATRMult  float64 `json:"stop_atr_mult"`  // 1.0 - 5.0
	TakeATRMult  float64 `json:"take_atr_mult"`  // 1.0 - 8.0
	TrailingPct  float64 `json:"trailing_pct"`   // 0.01 - 0.10

	// Risk management
	MaxDrawdownLimit float64 `json:"max_dd_limit"`   // 0.05 - 0.25
	CoolDownSec      int     `json:"cooldown_sec"`    // 10 - 300
	PyramidLevels    int     `json:"pyramid_levels"`  // 0 - 5

	// RL hyperparameters
	LearningRate  float64 `json:"lr"`
	EpsilonDecay  float64 `json:"epsilon_decay"`
	Gamma         float64 `json:"gamma"`
}

// Fitness result from backtest
type FitnessResult struct {
	Sharpe     float64
	WinRate    float64
	TotalPnL   float64
	MaxDD      float64
	TradesCount int
	Score      float64 // composite fitness
}

type GAPopulation struct {
	Individuals []StrategyGene
	Fitness     []FitnessResult
	PopSize     int
	EliteCount  int
	MutationRate float64
	CrossoverRate float64
	Generation   int
	BestEver    StrategyGene
	BestFitness  float64

	rng *rand.Rand
}

func NewGAPopulation(popSize int) *GAPopulation {
	ga := &GAPopulation{
		PopSize:       popSize,
		EliteCount:    popSize / 5,
		MutationRate:  0.15,
		CrossoverRate: 0.7,
		rng:           rand.New(rand.NewSource(777)),
	}
	ga.Individuals = make([]StrategyGene, popSize)
	ga.Fitness = make([]FitnessResult, popSize)
	for i := 0; i < popSize; i++ {
		ga.Individuals[i] = randomGene(ga.rng)
	}
	return ga
}

func randomGene(rng *rand.Rand) StrategyGene {
	return StrategyGene{
		MarginPct:        0.02 + rng.Float64()*0.18,    // 2% - 20%
		MaxPositions:     2 + rng.Intn(7),                // 2 - 8
		RSIThreshold:     20 + rng.Float64()*40,          // 20 - 60
		VolMinPct:        0.001 + rng.Float64()*0.02,     // 0.1% - 2.1%
		TrendPeriod:      5 + rng.Intn(46),               // 5 - 50
		StopATRMult:      1.0 + rng.Float64()*3.0,        // 1.0 - 4.0
		TakeATRMult:      2.0 + rng.Float64()*5.0,        // 2.0 - 7.0
		TrailingPct:      0.02 + rng.Float64()*0.06,      // 2% - 8%
		MaxDrawdownLimit: 0.05 + rng.Float64()*0.15,      // 5% - 20%
		CoolDownSec:      10 + rng.Intn(291),              // 10 - 300
		PyramidLevels:    rng.Intn(6),                     // 0 - 5
		LearningRate:     0.0001 + rng.Float64()*0.01,    // 0.0001 - 0.0101
		EpsilonDecay:     0.99 + rng.Float64()*0.009,     // 0.99 - 0.999
		Gamma:            0.9 + rng.Float64()*0.09,       // 0.9 - 0.99
	}
}

// Evaluate population fitness using backtest results
func (ga *GAPopulation) Evaluate(fitnesses []FitnessResult) {
	copy(ga.Fitness, fitnesses)
	// find best
	for i, f := range ga.Fitness {
		if f.Score > ga.BestFitness {
			ga.BestFitness = f.Score
			ga.BestEver = ga.Individuals[i]
		}
	}
}

// indexedFitness is used for tournament selection ranking
type indexedFitness struct {
	idx int
	f   float64
}

// Evolve one generation
func (ga *GAPopulation) Evolve() {
	// sort by fitness
	ranked := make([]indexedFitness, ga.PopSize)
	for i := range ga.Fitness {
		ranked[i] = indexedFitness{i, ga.Fitness[i].Score}
	}
	sort.Slice(ranked, func(i, j int) bool {
		return ranked[i].f > ranked[j].f
	})

	newPop := make([]StrategyGene, ga.PopSize)

	// elitism: keep best
	for i := 0; i < ga.EliteCount && i < len(ranked); i++ {
		newPop[i] = ga.Individuals[ranked[i].idx]
	}

	// fill rest with crossover + mutation
	for i := ga.EliteCount; i < ga.PopSize; i++ {
		p1 := ga.tournamentSelect(ranked, 3)
		p2 := ga.tournamentSelect(ranked, 3)

		var child StrategyGene
		if ga.rng.Float64() < ga.CrossoverRate {
			child = ga.uniformCrossover(ga.Individuals[p1], ga.Individuals[p2])
		} else {
			child = ga.Individuals[p1]
		}

		if ga.rng.Float64() < ga.MutationRate {
			child = ga.mutate(child)
		}
		newPop[i] = child
	}

	ga.Individuals = newPop
	ga.Generation++
}

func (ga *GAPopulation) tournamentSelect(ranked []indexedFitness, k int) int {
	best := -1
	bestScore := -1e9
	for i := 0; i < k; i++ {
		idx := ga.rng.Intn(len(ranked))
		if ranked[idx].f > bestScore {
			bestScore = ranked[idx].f
			best = ranked[idx].idx
		}
	}
	return best
}

func (ga *GAPopulation) uniformCrossover(p1, p2 StrategyGene) StrategyGene {
	return StrategyGene{
		MarginPct:        pick(p1.MarginPct, p2.MarginPct, ga.rng),
		MaxPositions:     pickInt(p1.MaxPositions, p2.MaxPositions, ga.rng),
		RSIThreshold:     pick(p1.RSIThreshold, p2.RSIThreshold, ga.rng),
		VolMinPct:        pick(p1.VolMinPct, p2.VolMinPct, ga.rng),
		TrendPeriod:      pickInt(p1.TrendPeriod, p2.TrendPeriod, ga.rng),
		StopATRMult:      pick(p1.StopATRMult, p2.StopATRMult, ga.rng),
		TakeATRMult:      pick(p1.TakeATRMult, p2.TakeATRMult, ga.rng),
		TrailingPct:      pick(p1.TrailingPct, p2.TrailingPct, ga.rng),
		MaxDrawdownLimit: pick(p1.MaxDrawdownLimit, p2.MaxDrawdownLimit, ga.rng),
		CoolDownSec:      pickInt(p1.CoolDownSec, p2.CoolDownSec, ga.rng),
		PyramidLevels:    pickInt(p1.PyramidLevels, p2.PyramidLevels, ga.rng),
		LearningRate:     pick(p1.LearningRate, p2.LearningRate, ga.rng),
		EpsilonDecay:     pick(p1.EpsilonDecay, p2.EpsilonDecay, ga.rng),
		Gamma:            pick(p1.Gamma, p2.Gamma, ga.rng),
	}
}

func (ga *GAPopulation) mutate(g StrategyGene) StrategyGene {
	nudge := func(v, min, max, scale float64) float64 {
		v += ga.rng.NormFloat64() * scale * (max - min) * 0.1
		return math.Max(min, math.Min(max, v))
	}
	nudgeInt := func(v, min, max int) int {
		v += int(ga.rng.NormFloat64() * 2)
		if v < min {
			v = min
		}
		if v > max {
			v = max
		}
		return v
	}

	return StrategyGene{
		MarginPct:        nudge(g.MarginPct, 0.01, 0.30, 1),
		MaxPositions:     nudgeInt(g.MaxPositions, 2, 8),
		RSIThreshold:     nudge(g.RSIThreshold, 20, 80, 1),
		VolMinPct:        nudge(g.VolMinPct, 0.001, 0.05, 1),
		TrendPeriod:      nudgeInt(g.TrendPeriod, 5, 50),
		StopATRMult:      nudge(g.StopATRMult, 1.0, 5.0, 1),
		TakeATRMult:      nudge(g.TakeATRMult, 1.0, 8.0, 1),
		TrailingPct:      nudge(g.TrailingPct, 0.01, 0.10, 1),
		MaxDrawdownLimit: nudge(g.MaxDrawdownLimit, 0.05, 0.25, 1),
		CoolDownSec:      nudgeInt(g.CoolDownSec, 10, 300),
		PyramidLevels:    nudgeInt(g.PyramidLevels, 0, 5),
		LearningRate:     nudge(g.LearningRate, 0.0001, 0.01, 0.3),
		EpsilonDecay:     nudge(g.EpsilonDecay, 0.99, 0.999, 0.3),
		Gamma:            nudge(g.Gamma, 0.9, 0.99, 0.3),
	}
}

func pick(a, b float64, rng *rand.Rand) float64 {
	if rng.Float64() < 0.5 {
		return a
	}
	return b
}

func pickInt(a, b int, rng *rand.Rand) int {
	if rng.Float64() < 0.5 {
		return a
	}
	return b
}
