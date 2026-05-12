package rl

import (
	"math"
	"math/rand"
)

// ── TradingEnv — Gym-like environment for RL training ──

type Action int

const (
	Hold  Action = iota // 0
	Long                // 1
	Short               // 2
	Close               // 3
)

type PositionSide int

const (
	Flat   PositionSide = 0
	LongP  PositionSide = 1
	ShortP PositionSide = 2
)

type StepResult struct {
	State    []float64
	Reward   float64
	Done     bool
	Info     map[string]float64
}

type TradingEnv struct {
	Prices       []float64 // historical OHLC closes
	Idx          int
	MaxSteps     int
	Position     PositionSide
	EntryPrice   float64
	Balance      float64
	Initial      float64
	PeakBalance  float64
	MaxDrawdown  float64
	TotalTrades  int
	Wins         int
	TotalPnL     float64
	SharpeTerms  []float64

	rng *rand.Rand
}

func NewTradingEnv(prices []float64, initialBalance float64) *TradingEnv {
	return &TradingEnv{
		Prices:      prices,
		Idx:         20, // need at least 20 bars for indicators
		MaxSteps:    len(prices) - 1,
		Position:    Flat,
		Balance:     initialBalance,
		Initial:     initialBalance,
		PeakBalance: initialBalance,
		rng:         rand.New(rand.NewSource(42)),
	}
}

// Reset environment for a new episode
func (env *TradingEnv) Reset() []float64 {
	env.Idx = 20
	env.Position = Flat
	env.EntryPrice = 0
	env.Balance = env.Initial
	env.PeakBalance = env.Initial
	env.MaxDrawdown = 0
	env.TotalTrades = 0
	env.Wins = 0
	env.TotalPnL = 0
	env.SharpeTerms = nil
	return env.getState()
}

// Step executes an action and returns (state, reward, done, info)
func (env *TradingEnv) Step(action Action) StepResult {
	prevBalance := env.Balance

	switch action {
	case Long:
		if env.Position == Flat {
			env.Position = LongP
			env.EntryPrice = env.currentPrice()
			env.TotalTrades++
		}
	case Short:
		if env.Position == Flat {
			env.Position = ShortP
			env.EntryPrice = env.currentPrice()
			env.TotalTrades++
		}
	case Close:
		if env.Position != Flat {
			px := env.currentPrice()
			var pnl float64
			if env.Position == LongP {
				pnl = (px - env.EntryPrice) / env.EntryPrice * env.Balance
			} else {
				pnl = (env.EntryPrice - px) / env.EntryPrice * env.Balance
			}
			env.Balance += pnl
			env.TotalPnL += pnl
			env.SharpeTerms = append(env.SharpeTerms, pnl)
			if pnl > 0 {
				env.Wins++
			}
			env.Position = Flat
		}
	}

	// update drawdown
	if env.Balance > env.PeakBalance {
		env.PeakBalance = env.Balance
	}
	dd := (env.PeakBalance - env.Balance) / env.PeakBalance
	if dd > env.MaxDrawdown {
		env.MaxDrawdown = dd
	}

	env.Idx++
	done := env.Idx >= env.MaxSteps || env.Balance <= env.Initial*0.1
	reward := env.computeReward(prevBalance)
	state := env.getState()

	return StepResult{
		State:  state,
		Reward: reward,
		Done:   done,
		Info: map[string]float64{
			"balance":     env.Balance,
			"drawdown":    env.MaxDrawdown,
			"total_trades": float64(env.TotalTrades),
		},
	}
}

func (env *TradingEnv) getState() []float64 {
	px := env.currentPrice()
	if env.Idx < 5 {
		return make([]float64, 6)
	}

	// feature 0: price change % (5-bar)
	chg := (px - env.Prices[env.Idx-5]) / env.Prices[env.Idx-5]

	// feature 1: RSI-like (14-bar)
	rsi := env.rsi(14)

	// feature 2: volume proxy (price range ratio)
	vr := env.volatilityRatio(10)

	// feature 3: position side (-1, 0, 1)
	pos := 0.0
	if env.Position == LongP {
		pos = 1.0
	} else if env.Position == ShortP {
		pos = -1.0
	}

	// feature 4: unrealized PnL %
	var upnl float64
	if env.Position != Flat && env.EntryPrice > 0 {
		if env.Position == LongP {
			upnl = (px - env.EntryPrice) / env.EntryPrice
		} else {
			upnl = (env.EntryPrice - px) / env.EntryPrice
		}
	}

	// feature 5: drawdown ratio
	dd := 0.0
	if env.PeakBalance > 0 {
		dd = (env.PeakBalance - env.Balance) / env.PeakBalance
	}

	return []float64{chg, rsi, vr, pos, upnl, dd}
}

func (env *TradingEnv) currentPrice() float64 {
	if env.Idx >= len(env.Prices) {
		return env.Prices[len(env.Prices)-1]
	}
	return env.Prices[env.Idx]
}

func (env *TradingEnv) rsi(period int) float64 {
	if env.Idx < period+1 {
		return 50.0
	}
	var gain, loss float64
	for i := env.Idx - period; i < env.Idx; i++ {
		diff := env.Prices[i+1] - env.Prices[i]
		if diff > 0 {
			gain += diff
		} else {
			loss -= diff
		}
	}
	if loss == 0 {
		return 100.0
	}
	rs := (gain / float64(period)) / (loss / float64(period))
	return 100.0 - 100.0/(1.0+rs)
}

func (env *TradingEnv) volatilityRatio(period int) float64 {
	if env.Idx < period+1 {
		return 1.0
	}
	mean := 0.0
	for i := env.Idx - period; i < env.Idx; i++ {
		mean += env.Prices[i]
	}
	mean /= float64(period)
	variance := 0.0
	for i := env.Idx - period; i < env.Idx; i++ {
		d := env.Prices[i] - mean
		variance += d * d
	}
	return math.Sqrt(variance/float64(period)) / mean
}

func (env *TradingEnv) computeReward(prevBalance float64) float64 {
	// primary: PnL (clamped to prevent reward explosion)
	pnl := env.Balance - prevBalance
	if pnl > 100 {
		pnl = 100
	} else if pnl < -100 {
		pnl = -100
	}

	// drawdown penalty
	ddPenalty := -2.0 * env.MaxDrawdown

	// exploration bonus for first trades
	exploreBonus := 0.0
	if env.TotalTrades <= 5 {
		exploreBonus = 0.01
	}

	return pnl + ddPenalty + exploreBonus
}

// Metrics returns performance summary
func (env *TradingEnv) Metrics() map[string]float64 {
	sharpe := 0.0
	if len(env.SharpeTerms) >= 2 {
		mean := 0.0
		for _, v := range env.SharpeTerms {
			mean += v
		}
		mean /= float64(len(env.SharpeTerms))
		variance := 0.0
		for _, v := range env.SharpeTerms {
			d := v - mean
			variance += d * d
		}
		variance /= float64(len(env.SharpeTerms) - 1)
		if variance > 0 {
			sharpe = mean / math.Sqrt(variance)
		}
	}

	winRate := 0.0
	if env.TotalTrades > 0 {
		winRate = float64(env.Wins) / float64(env.TotalTrades)
	}

	return map[string]float64{
		"total_pnl":     env.TotalPnL,
		"sharpe":        sharpe,
		"win_rate":      winRate,
		"max_drawdown":  env.MaxDrawdown,
		"total_trades":  float64(env.TotalTrades),
		"final_balance": env.Balance,
	}
}
