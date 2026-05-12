package planning

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"time"
)

// MacroBuilder — BTC/ETH 宏观环境分析
type MacroBuilder struct {
	cache  map[string]*MacroContext
	client *http.Client
}

func NewMacroBuilder() *MacroBuilder {
	return &MacroBuilder{
		cache:  make(map[string]*MacroContext),
		client: &http.Client{Timeout: 15 * time.Second},
	}
}

// Fetch 从 Gate.io 拉取日K线 + 计算宏观指标
func (m *MacroBuilder) Fetch(ctx context.Context, symbol string) error {
	// Gate.io 日K线端点
	url := fmt.Sprintf("https://api.gateio.ws/api/v4/spot/candlesticks?currency_pair=%s&interval=1d&limit=30",
		stringsReplace(symbol, "/", "_"))

	req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
	resp, err := m.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	var raw [][]string
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return err
	}

	if len(raw) < 5 {
		return fmt.Errorf("insufficient data: %d candles", len(raw))
	}

	// 解析K线: [timestamp, volume, close, high, low, open]
	closes := make([]float64, len(raw))
	highs := make([]float64, len(raw))
	lows := make([]float64, len(raw))
	for i, r := range raw {
		closes[i] = parseFloat(r[2])
		highs[i] = parseFloat(r[3])
		lows[i] = parseFloat(r[4])
	}

	

	// ATR14
	atr := calcATR(highs, lows, closes, 14)

	// 趋势：线性回归斜率
	trend := calcTrend(closes, 20)

	// 波动率分位
	volPct := calcVolPercentile(closes, 20)

	// Regime判定
	var regime Regime
	switch {
	case trend > 0.01 && volPct < 70:
		regime = RegimeTrendUp
	case trend < -0.01 && volPct < 70:
		regime = RegimeTrendDown
	default:
		regime = RegimeRange
	}

	macro := &MacroContext{
		Symbol:    symbol,
		Regime:    regime,
		RangeLow:  minFloat(lows[len(lows)-20:]...) * 0.99,
		RangeHigh: maxFloat(highs[len(highs)-20:]...) * 1.01,
		ATR14:     atr,
		VolPct:    volPct,
		TrendStr:  trend,
		Timestamp: time.Now().Unix(),
	}

	m.cache[symbol] = macro
	return nil
}

func (m *MacroBuilder) Get(symbol string) *MacroContext {
	if c, ok := m.cache[symbol]; ok {
		return c
	}
	return &MacroContext{Symbol: symbol, Regime: RegimeUnknown, ATR14: 1000}
}

// ── 计算工具 ──

func calcATR(highs, lows, closes []float64, period int) float64 {
	if len(closes) < period+1 {
		return closes[len(closes)-1] * 0.02
	}
	tr := 0.0
	for i := len(closes) - period; i < len(closes); i++ {
		h := highs[i]
		l := lows[i]
		pc := closes[i-1]
		tr += math.Max(h-l, math.Max(math.Abs(h-pc), math.Abs(l-pc)))
	}
	return tr / float64(period)
}

func calcTrend(closes []float64, period int) float64 {
	if len(closes) < period {
		return 0
	}
	n := len(closes)
	sumX, sumY, sumXY, sumX2 := 0.0, 0.0, 0.0, 0.0
	for i := n - period; i < n; i++ {
		x := float64(i - (n - period))
		y := closes[i]
		sumX += x
		sumY += y
		sumXY += x * y
		sumX2 += x * x
	}
	p := float64(period)
	slope := (p*sumXY - sumX*sumY) / (p*sumX2 - sumX*sumX)
	return slope / closes[n-1]
}

func calcVolPercentile(closes []float64, period int) float64 {
	if len(closes) < period+1 {
		return 50
	}
	n := len(closes)
	returns := make([]float64, period)
	for i := 0; i < period; i++ {
		returns[i] = (closes[n-period+i] - closes[n-period+i-1]) / closes[n-period+i-1]
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
	// 简单分位：标准差<5%→低波动, <15%→中, >15%→高
	std := math.Sqrt(variance / float64(period-1))
	return math.Min(100, std*400) // ~std*400映射到0-100
}

func minFloat(vals ...float64) float64 {
	m := vals[0]
	for _, v := range vals[1:] {
		if v < m {
			m = v
		}
	}
	return m
}

func maxFloat(vals ...float64) float64 {
	m := vals[0]
	for _, v := range vals[1:] {
		if v > m {
			m = v
		}
	}
	return m
}

func stringsReplace(s, old, new string) string {
	result := ""
	for i := 0; i < len(s); i++ {
		if i+len(old) <= len(s) && s[i:i+len(old)] == old {
			result += new
			i += len(old) - 1
		} else {
			result += string(s[i])
		}
	}
	return result
}

func parseFloat(s string) float64 {
	var v float64
	fmt.Sscanf(s, "%f", &v)
	return v
}
