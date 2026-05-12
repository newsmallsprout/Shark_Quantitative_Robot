package rl

import (
	"fmt"
	"math"
	"sort"
)

// ── Backtest Engine: Historical replay for offline training ──

type BacktestResult struct {
	TotalPnL     float64
	Sharpe       float64
	WinRate      float64
	MaxDrawdown  float64
	TotalTrades  int
	FinalBalance float64
	EquityCurve  []float64
	Trades       []BacktestTrade
}

type BacktestTrade struct {
	EntryIdx    int
	ExitIdx     int
	Side        string
	EntryPrice  float64
	ExitPrice   float64
	PnL         float64
	PnLPct      float64
}

// RunBacktest evaluates a gene on historical prices using simple rules
func RunBacktest(prices []float64, gene StrategyGene) BacktestResult {
	if len(prices) < 50 {
		return BacktestResult{}
	}

	balance := 1000.0
	initial := balance
	peak := balance
	var equity []float64
	var trades []BacktestTrade
	var returns []float64

	var position string // "", "long", "short"
	var entryPrice float64
	var entryIdx int

	for i := 50; i < len(prices)-1; i++ {
		px := prices[i]

		// compute indicators
		rsi := computeRSI(prices, i, 14)
		vol := computeVolatility(prices, i, int(gene.TrendPeriod))
		trend := computeTrend(prices, i, int(gene.TrendPeriod))

		// position management: stop loss / take profit
		if position != "" && entryPrice > 0 {
			var pnlPct float64
			if position == "long" {
				pnlPct = (px - entryPrice) / entryPrice
			} else {
				pnlPct = (entryPrice - px) / entryPrice
			}

			// trailing stop
			trailStop := gene.TrailingPct
			if position == "long" && pnlPct > trailStop {
				// check if price drops trailStop from peak
				peakSince := px
				for j := entryIdx; j <= i; j++ {
					if prices[j] > peakSince {
						peakSince = prices[j]
					}
				}
				if px < peakSince*(1-trailStop) {
					// close on trailing stop
					realized := balance * (peakSince*(1-trailStop) - entryPrice) / entryPrice
					trades = append(trades, BacktestTrade{
						EntryIdx: entryIdx, ExitIdx: i,
						Side: position, EntryPrice: entryPrice, ExitPrice: px,
						PnL: realized, PnLPct: pnlPct,
					})
					balance += realized
					returns = append(returns, realized)
					position = ""
					continue
				}
			}

			// stop loss (ATR-based)
			atr := computeATR(prices, i, 14)
			stopDist := atr * gene.StopATRMult
			if position == "long" && px < entryPrice-stopDist {
				loss := balance * (-stopDist) / entryPrice
				trades = append(trades, BacktestTrade{
					EntryIdx: entryIdx, ExitIdx: i,
					Side: position, EntryPrice: entryPrice, ExitPrice: px,
					PnL: loss, PnLPct: pnlPct,
				})
				balance += loss
				returns = append(returns, loss)
				position = ""
				continue
			} else if position == "short" && px > entryPrice+stopDist {
				loss := balance * (-stopDist) / entryPrice
				trades = append(trades, BacktestTrade{
					EntryIdx: entryIdx, ExitIdx: i,
					Side: position, EntryPrice: entryPrice, ExitPrice: px,
					PnL: loss, PnLPct: pnlPct,
				})
				balance += loss
				returns = append(returns, loss)
				position = ""
				continue
			}

			// take profit
			tpDist := atr * gene.TakeATRMult
			if position == "long" && px > entryPrice+tpDist {
				profit := balance * tpDist / entryPrice
				trades = append(trades, BacktestTrade{
					EntryIdx: entryIdx, ExitIdx: i,
					Side: position, EntryPrice: entryPrice, ExitPrice: px,
					PnL: profit, PnLPct: pnlPct,
				})
				balance += profit
				returns = append(returns, profit)
				position = ""
				continue
			} else if position == "short" && px < entryPrice-tpDist {
				profit := balance * tpDist / entryPrice
				trades = append(trades, BacktestTrade{
					EntryIdx: entryIdx, ExitIdx: i,
					Side: position, EntryPrice: entryPrice, ExitPrice: px,
					PnL: profit, PnLPct: pnlPct,
				})
				balance += profit
				returns = append(returns, profit)
				position = ""
				continue
			}
		}

		// entry signals
		if position == "" && len(trades) < gene.MaxPositions*3 {
			margin := balance * gene.MarginPct
			if margin < 10 {
				continue
			}

			// long signal: RSI oversold + trend up
			if rsi < gene.RSIThreshold && trend > 0 && vol > gene.VolMinPct {
				position = "long"
				entryPrice = px
				entryIdx = i
			} else if rsi > (100-gene.RSIThreshold) && trend < 0 && vol > gene.VolMinPct {
				position = "short"
				entryPrice = px
				entryIdx = i
			}
		}

		// track equity
		if balance > peak {
			peak = balance
		}
		equity = append(equity, balance)

		// drawdown limit
		dd := (peak - balance) / peak
		if dd > gene.MaxDrawdownLimit {
			break
		}
	}

	// close any open position at end
	if position != "" {
		px := prices[len(prices)-1]
		var pnl float64
		if position == "long" {
			pnl = balance * (px - entryPrice) / entryPrice
		} else {
			pnl = balance * (entryPrice - px) / entryPrice
		}
		balance += pnl
		returns = append(returns, pnl)
		trades = append(trades, BacktestTrade{
			EntryIdx: entryIdx, ExitIdx: len(prices) - 1,
			Side: position, EntryPrice: entryPrice, ExitPrice: px,
			PnL: pnl,
		})
	}

	// compute metrics
	sharpe, winRate, maxDD := computeMetrics(returns, initial, peak)
	totalPnL := balance - initial

	return BacktestResult{
		TotalPnL:     totalPnL,
		Sharpe:       sharpe,
		WinRate:      winRate,
		MaxDrawdown:  maxDD,
		TotalTrades:  len(trades),
		FinalBalance: balance,
		EquityCurve:  equity,
		Trades:       trades,
	}
}

// computeFitness converts backtest result to a composite fitness score
func ComputeFitness(r BacktestResult) FitnessResult {
	score := 0.0
	score += math.Tanh(r.TotalPnL/200.0) * 2.0         // PnL contribution
	score += math.Max(-1.5, math.Min(1.5, r.Sharpe*0.5)) // Sharpe contribution
	score -= r.MaxDrawdown * 5.0                          // drawdown penalty
	if r.TotalTrades < 3 {
		score -= 2.0 // insufficient trades penalty
	}
	if r.TotalTrades > 50 {
		score += 0.5 // sufficient data bonus
	}

	return FitnessResult{
		Sharpe:      r.Sharpe,
		WinRate:     r.WinRate,
		TotalPnL:    r.TotalPnL,
		MaxDD:       r.MaxDrawdown,
		TradesCount: r.TotalTrades,
		Score:       score,
	}
}

// ── Indicator helpers ──

func computeRSI(prices []float64, idx, period int) float64 {
	if idx < period+1 {
		return 50
	}
	var gain, loss float64
	for i := idx - period; i < idx; i++ {
		diff := prices[i+1] - prices[i]
		if diff > 0 {
			gain += diff
		} else {
			loss -= diff
		}
	}
	if loss == 0 {
		return 100
	}
	avgGain := gain / float64(period)
	avgLoss := loss / float64(period)
	rs := avgGain / avgLoss
	return 100 - 100/(1+rs)
}

func computeVolatility(prices []float64, idx, period int) float64 {
	if idx < period+1 {
		return 0.01
	}
	returns := make([]float64, period)
	for i := 0; i < period; i++ {
		returns[i] = (prices[idx-period+i] - prices[idx-period+i-1]) / prices[idx-period+i-1]
	}
	mean := 0.0
	for _, r := range returns {
		mean += r
	}
	mean /= float64(period)
	variance := 0.0
	for _, r := range returns {
		d := r - mean
		variance += d * d
	}
	return math.Sqrt(variance / float64(period))
}

func computeTrend(prices []float64, idx, period int) float64 {
	if idx < period {
		return 0
	}
	// linear regression slope
	sumX, sumY, sumXY, sumX2 := 0.0, 0.0, 0.0, 0.0
	for i := 0; i < period; i++ {
		x := float64(i)
		y := prices[idx-period+i]
		sumX += x
		sumY += y
		sumXY += x * y
		sumX2 += x * x
	}
	n := float64(period)
	slope := (n*sumXY - sumX*sumY) / (n*sumX2 - sumX*sumX)
	return slope / prices[idx] // normalize
}

func computeATR(prices []float64, idx, period int) float64 {
	if idx < period+1 {
		return prices[idx] * 0.02
	}
	tr := 0.0
	for i := idx - period + 1; i <= idx; i++ {
		high := math.Max(prices[i], prices[i-1])
		low := math.Min(prices[i], prices[i-1])
		tr += high - low
	}
	return tr / float64(period)
}

func computeMetrics(returns []float64, initial, peak float64) (sharpe, winRate, maxDD float64) {
	if len(returns) == 0 {
		return 0, 0, 0
	}

	// sharpe
	mean := 0.0
	for _, r := range returns {
		mean += r
	}
	mean /= float64(len(returns))
	variance := 0.0
	for _, r := range returns {
		d := r - mean
		variance += d * d
	}
	if len(returns) > 1 {
		variance /= float64(len(returns) - 1)
	}
	if variance > 0 {
		sharpe = mean / math.Sqrt(variance)
	}

	// win rate
	wins := 0
	for _, r := range returns {
		if r > 0 {
			wins++
		}
	}
	winRate = float64(wins) / float64(len(returns))

	// max drawdown
	runningPeak := initial
	runningBal := initial
	for _, r := range returns {
		runningBal += r
		if runningBal > runningPeak {
			runningPeak = runningBal
		}
		dd := (runningPeak - runningBal) / runningPeak
		if dd > maxDD {
			maxDD = dd
		}
	}

	return
}

// ── Pattern extraction from trade history ──
// Learns recurring patterns from successful trades

type TradePattern struct {
	Symbol     string  `json:"symbol"`
	Side       string  `json:"side"`
	RSIEntry   float64 `json:"rsi_entry"`
	VolEntry   float64 `json:"vol_entry"`
	TrendEntry float64 `json:"trend_entry"`
	PnLPct     float64 `json:"pnl_pct"`
	Count      int     `json:"count"`
}

// ExtractPatterns analyzes trade history to find recurring profitable patterns
func ExtractPatterns(trades []BacktestTrade, prices []float64) []TradePattern {
	patterns := make(map[string]*TradePattern)

	for _, t := range trades {
		if t.PnLPct < 0.01 {
			continue // only learn from profitable trades
		}

		key := fmt.Sprintf("%s_%s", t.Side, classifyEntry(t.EntryPrice, prices, t.EntryIdx))

		if p, ok := patterns[key]; ok {
			p.Count++
			p.PnLPct = (p.PnLPct*float64(p.Count-1) + t.PnLPct) / float64(p.Count)
		} else {
			rsi := computeRSI(prices, t.EntryIdx, 14)
			vol := computeVolatility(prices, t.EntryIdx, 20)
			trend := computeTrend(prices, t.EntryIdx, 20)

			patterns[key] = &TradePattern{
				Symbol:     key,
				Side:       t.Side,
				RSIEntry:   rsi,
				VolEntry:   vol,
				TrendEntry: trend,
				PnLPct:     t.PnLPct,
				Count:      1,
			}
		}
	}

	// return top patterns sorted by count
	result := make([]TradePattern, 0, len(patterns))
	for _, p := range patterns {
		result = append(result, *p)
	}
	sort.Slice(result, func(i, j int) bool {
		return result[i].Count*int(result[i].PnLPct*100) > result[j].Count*int(result[j].PnLPct*100)
	})

	if len(result) > 20 {
		result = result[:20]
	}
	return result
}

func classifyEntry(px float64, prices []float64, idx int) string {
	if idx < 20 {
		return "unknown"
	}
	avg := 0.0
	for i := idx - 20; i < idx; i++ {
		avg += prices[i]
	}
	avg /= 20
	if px > avg*1.02 {
		return "breakout"
	} else if px < avg*0.98 {
		return "breakdown"
	}
	return "range"
}
