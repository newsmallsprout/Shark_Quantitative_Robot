package planning

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"net/http"
	"sort"
	"time"
)

// BookIngestor — Gate.io 订单簿深度分析
type BookIngestor struct {
	client *http.Client
}

func NewBookIngestor() *BookIngestor {
	return &BookIngestor{client: &http.Client{Timeout: 10 * time.Second}}
}

// Fetch 拉取订单簿 → 计算支撑/压力
func (b *BookIngestor) Fetch(ctx context.Context, symbol string) (*DepthProfile, error) {
	pair := stringsReplace(symbol, "/", "_")
	url := fmt.Sprintf("https://api.gateio.ws/api/v4/futures/usdt/order_book?contract=%s&limit=50", pair)

	req, _ := http.NewRequestWithContext(ctx, "GET", url, nil)
	resp, err := b.client.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	var raw struct {
		Bids []json.RawMessage `json:"bids"`
		Asks []json.RawMessage `json:"asks"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&raw); err != nil {
		return nil, err
	}

	bids := parseLevelsFlex(raw.Bids)
	asks := parseLevelsFlex(raw.Asks)

	if len(bids) == 0 || len(asks) == 0 {
		return nil, fmt.Errorf("empty orderbook")
	}

	mid := (bids[0].price + asks[0].price) / 2

	// 支撑：买盘深度加权
	support := computeSupport(bids, mid)
	resistance := computeResistance(asks, mid)
	spread := (asks[0].price - bids[0].price) / mid * 100

	// 1%深度内累计量
	bidVol1pct := cumulativeVolume(bids, mid*0.99, mid)
	askVol1pct := cumulativeVolume(asks, mid, mid*1.01)

	return &DepthProfile{
		Symbol:             symbol,
		SupportPrice:       support.price,
		ResistancePrice:    resistance.price,
		SupportStrength:    math.Min(1, support.strength),
		ResistanceStrength: math.Min(1, resistance.strength),
		SpreadPct:          spread,
		BidVolume1Pct:      bidVol1pct,
		AskVolume1Pct:      askVol1pct,
		Timestamp:          time.Now().Unix(),
	}, nil
}

type level struct {
	price float64
	vol   float64
}

type supportRes struct {
	price    float64
	strength float64
}

func parseLevelsFlex(raw []json.RawMessage) []level {
	levels := make([]level, len(raw))
	for i, r := range raw {
		// Try [price_str, vol_str]
		var arr []string
		if err := json.Unmarshal(r, &arr); err == nil && len(arr) >= 2 {
			levels[i] = level{price: parseFloat(arr[0]), vol: parseFloat(arr[1])}
			continue
		}
		// Try {"p": price, "v": vol}
		var obj struct {
			P string `json:"p"`
			V string `json:"v"`
		}
		if err := json.Unmarshal(r, &obj); err == nil {
			levels[i] = level{price: parseFloat(obj.P), vol: parseFloat(obj.V)}
			continue
		}
		// Try {"0": price, "1": vol}
		var obj2 struct {
			Price string `json:"0"`
			Vol   string `json:"1"`
		}
		if err := json.Unmarshal(r, &obj2); err == nil && obj2.Price != "" {
			levels[i] = level{price: parseFloat(obj2.Price), vol: parseFloat(obj2.Vol)}
		}
	}
	return levels
}

func computeSupport(bids []level, mid float64) supportRes {
	// 价格排序(升序)
	sort.Slice(bids, func(i, j int) bool { return bids[i].price < bids[j].price })

	// 找挂单量最大的价格
	var maxVol float64
	totalVol := 0.0
	weightedPrice := 0.0

	for _, b := range bids {
		totalVol += b.vol
		weightedPrice += b.price * b.vol
		if b.vol > maxVol {
			maxVol = b.vol
		}
	}

	if totalVol == 0 {
		return supportRes{mid * 0.99, 0.3}
	}

	avgPrice := weightedPrice / totalVol
	// 强度 = 深度量 / 预期（简单归一化）
	strength := math.Min(1, totalVol/100000*2)

	return supportRes{avgPrice, strength}
}

func computeResistance(asks []level, mid float64) supportRes {
	sort.Slice(asks, func(i, j int) bool { return asks[i].price < asks[j].price })

	var maxVol float64
	totalVol := 0.0
	weightedPrice := 0.0

	for _, a := range asks {
		totalVol += a.vol
		weightedPrice += a.price * a.vol
		if a.vol > maxVol {
			maxVol = a.vol
		}
	}

	if totalVol == 0 {
		return supportRes{mid * 1.01, 0.3}
	}

	avgPrice := weightedPrice / totalVol
	strength := math.Min(1, totalVol/100000*2)

	return supportRes{avgPrice, strength}
}

func cumulativeVolume(levels []level, low, high float64) float64 {
	var total float64
	for _, l := range levels {
		if l.price >= low && l.price <= high {
			total += l.vol
		}
	}
	return total
}
