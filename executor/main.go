package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha512"
	"crypto/subtle"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
)

// ═══════════════════════════════════════════════
// Config
// ═══════════════════════════════════════════════

var (
	gateKey    = os.Getenv("GATE_API_KEY")
	gateSecret = os.Getenv("GATE_API_SECRET")
	redisURL   = os.Getenv("SHARK_REDIS_URL")
	gateBase   = "https://api.gateio.ws/api/v4/futures/usdt"
	rdb        *redis.Client
	ctx        = context.Background()
)

// ═══════════════════════════════════════════════
// Gate.io API
// ═══════════════════════════════════════════════

func gateSign(method, path, query, body string) map[string]string {
	ts := strconv.FormatInt(time.Now().Unix(), 10)
	payload := method + "\n" + path + "\n" + query + "\n" + sha512Hex(body) + "\n" + ts
	mac := hmac.New(sha512.New, []byte(gateSecret))
	mac.Write([]byte(payload))
	return map[string]string{
		"KEY": gateKey, "Timestamp": ts, "SIGN": hex.EncodeToString(mac.Sum(nil)),
		"Content-Type": "application/json", "Accept": "application/json",
	}
}

func sha512Hex(s string) string {
	h := sha512.Sum512([]byte(s))
	return hex.EncodeToString(h[:])
}

func gateAPI(method, path string, body map[string]interface{}) (map[string]interface{}, error) {
	url := gateBase + path
	// Build signing path and extract query
	fullPath := "/api/v4/futures/usdt" + path
	signPath := fullPath
	queryStr := ""
	if idx := strings.Index(fullPath, "?"); idx >= 0 {
		signPath = fullPath[:idx]
		queryStr = fullPath[idx+1:]
	}
	var bodyStr string
	if body != nil {
		b, _ := json.Marshal(body)
		bodyStr = string(b)
	}
	req, _ := http.NewRequest(method, url, strings.NewReader(bodyStr))
	headers := gateSign(method, signPath, queryStr, bodyStr)
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	data, _ := io.ReadAll(resp.Body)
	var result map[string]interface{}
	json.Unmarshal(data, &result)
	if resp.StatusCode >= 400 {
		return nil, fmt.Errorf("API %d: %s", resp.StatusCode, string(data))
	}
	return result, nil
}

func getBalance() float64 {
	r, err := gateAPI("GET", "/accounts?currency=USDT", nil)
	if err != nil {
		log.Printf("getBalance error: %v", err)
		return 0
	}
	if available, ok := r["available"].(string); ok {
		v, _ := strconv.ParseFloat(available, 64)
		return v
	}
	return 0
}

// ═══════════════════════════════════════════════
// Order Execution
// ═══════════════════════════════════════════════

type TradeCmd struct {
	Symbol           string    `json:"symbol"`
	Side             string    `json:"side"`
	Size             int       `json:"size"`
	Leverage         int       `json:"leverage"`
	Action           string    `json:"action"` // "open" or "close"
	Mode             string    `json:"mode"`   // "paper" or "live"
	StopLoss         float64   `json:"stop_loss,omitempty"`
	TakeProfit       float64   `json:"take_profit,omitempty"`
	TakeProfitLevels []float64 `json:"take_profit_levels,omitempty"`
	Source           string    `json:"source,omitempty"`
	Token            string    `json:"token,omitempty"`
}

func validateTradeCmd(cmd TradeCmd) error {
	if cmd.Mode != "live" {
		return fmt.Errorf("unsupported mode %q", cmd.Mode)
	}
	if cmd.Action != "open" && cmd.Action != "close" {
		return fmt.Errorf("unsupported action %q", cmd.Action)
	}
	if cmd.Side != "long" && cmd.Side != "short" {
		return fmt.Errorf("unsupported side %q", cmd.Side)
	}
	if !strings.Contains(cmd.Symbol, "/") {
		return fmt.Errorf("invalid symbol %q", cmd.Symbol)
	}
	if cmd.Size <= 0 {
		return fmt.Errorf("size must be positive")
	}
	if cmd.Leverage < 1 || cmd.Leverage > 125 {
		return fmt.Errorf("leverage out of range")
	}
	expectedToken := os.Getenv("SHARK_ORDER_TOKEN")
	if expectedToken == "" {
		expectedToken = os.Getenv("SHARK_API_TOKEN")
	}
	if expectedToken != "" && subtle.ConstantTimeCompare([]byte(cmd.Token), []byte(expectedToken)) != 1 {
		return fmt.Errorf("invalid order token")
	}
	return nil
}

func executeOpen(cmd TradeCmd) {
	contract := strings.ReplaceAll(cmd.Symbol, "/", "_")

	// Set leverage
	gateAPI("POST", "/positions/"+contract+"/leverage",
		map[string]interface{}{"leverage": strconv.Itoa(cmd.Leverage)})

	// Place market order
	size := cmd.Size
	if cmd.Side == "short" {
		size = -size
	}
	r, err := gateAPI("POST", "/orders", map[string]interface{}{
            "contract": contract, "size": size, "price": "0", "tif": "ioc",
            "text": fmt.Sprintf("t-op-%d", time.Now().UnixMilli()),
    })
    if err != nil {
            log.Printf("❌ 开仓失败 %s %s: %v", cmd.Symbol, cmd.Side, err)
            rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, "failed", 0)
            return
    }
    log.Printf("✅ 开仓 %s %s size=%d status=%v", cmd.Symbol, cmd.Side, cmd.Size, r["status"])

    // ── 清理该合约旧的条件单，防止积累 ──
    gateAPI("DELETE", "/price_orders?contract="+contract, nil)

    // ── 挂止盈止损条件单 ──
	if cmd.StopLoss > 0 {
		slSize := size // 这里是开仓的size，带符号的（做多为正，做空为负）
		// 平仓必须是相反的size
		slReduceSize := -slSize

		// 止损：stop-market
		slRule := 2 // long: price<=trigger, short: price>=trigger
		if cmd.Side == "short" {
			slRule = 1
		}
		slR, slErr := gateAPI("POST", "/price_orders", map[string]interface{}{
			"initial": map[string]interface{}{
				"contract":    contract,
				"size":        slReduceSize,  // 平仓必须是相反的size
				"price":       "0",
				"tif":         "ioc",
				"reduce_only": true,
				"text":        fmt.Sprintf("t-sl-%d", time.Now().UnixMilli()),
			},
			"trigger": map[string]interface{}{
				"price":         strconv.FormatFloat(cmd.StopLoss, 'f', -1, 64),
				"rule":          slRule,
				"expiration":    86400 * 7, // 必须是 86400 的倍数
				"strategy_type": 0,
			},
		})
		if slErr != nil {
			log.Printf("⚠ 止损单挂失败 %s: %v", cmd.Symbol, slErr)
		} else {
			log.Printf("🛑 止损 %s @%.1f status=%v", cmd.Symbol, cmd.StopLoss, slR["status"])
		}
	}
	tpLevels := cmd.TakeProfitLevels
	if len(tpLevels) == 0 && cmd.TakeProfit > 0 {
		tpLevels = []float64{cmd.TakeProfit}
	}
	if len(tpLevels) > 0 {
		tpSize := size
		// 止盈：stop-market
		tpRule := 1 // long: price>=trigger, short: price<=trigger
		if cmd.Side == "short" {
			tpRule = 2
		}

		// 将平仓总仓位切分为多份止盈 (取绝对值进行切分)
		absTpSize := tpSize
		if absTpSize < 0 {
			absTpSize = -absTpSize
		}
		splits := splitReduceSizes(absTpSize, len(tpLevels))
		for i, target := range tpLevels {
			if target <= 0 || i >= len(splits) {
				continue
			}

			// 确定正确的反向 size
			reduceSize := -splits[i]
			if cmd.Side == "short" {
				reduceSize = splits[i]
			}
			tpR, tpErr := gateAPI("POST", "/price_orders", map[string]interface{}{
				"initial": map[string]interface{}{
					"contract":    contract,
					"size":        reduceSize, // 已经处理过反向的正确 size
					"price":       "0",
					"tif":         "ioc",
					"reduce_only": true,
					"text":        fmt.Sprintf("t-tp-%d", time.Now().UnixMilli()),
				},
				"trigger": map[string]interface{}{
					"price":         strconv.FormatFloat(target, 'f', -1, 64),
					"rule":          tpRule,
					"expiration":    86400 * 7, // 必须是 86400 的倍数
					"strategy_type": 0,
				},
			})
			if tpErr != nil {
				log.Printf("⚠ 止盈单挂失败 %s: %v", cmd.Symbol, tpErr)
			} else {
				log.Printf("🎯 止盈 %s @%.1f size=%d status=%v", cmd.Symbol, target, splits[i], tpR["status"])
			}
		}
	}

	rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, "open_ok", 0)
}

func splitReduceSizes(total, targets int) []int {
	if total <= 0 || targets <= 0 {
		return nil
	}
	if targets > total {
		targets = total
	}
	sizes := make([]int, targets)
	base := total / targets
	rem := total % targets
	for i := range sizes {
		sizes[i] = base
		if i < rem {
			sizes[i]++
		}
	}
	return sizes
}

func executeClose(cmd TradeCmd) {
	contract := strings.ReplaceAll(cmd.Symbol, "/", "_")
	// Close position: reverse direction
	size := cmd.Size
	if cmd.Side == "long" {
		size = -size
	}
	r, err := gateAPI("POST", "/orders", map[string]interface{}{
            "contract": contract, "size": size, "price": "0", "tif": "ioc",
            "reduce_only": true,
            "text":        fmt.Sprintf("t-cl-%d", time.Now().UnixMilli()),
    })
	if err != nil {
            log.Printf("❌ 平仓失败 %s %s: %v", cmd.Symbol, cmd.Side, err)
            rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, "close_failed", 0)
            return
    }
    log.Printf("✅ 平仓 %s %s status=%v", cmd.Symbol, cmd.Side, r["status"])
    
    // ── 平仓后清理所有条件单 ──
    gateAPI("DELETE", "/price_orders?contract="+contract, nil)
    
    rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, "close_ok", 0)
}

// ═══════════════════════════════════════════════
// Main Loop — Redis pub/sub for trade commands
// ═══════════════════════════════════════════════

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

	if gateKey == "" || gateSecret == "" {
		log.Fatal("GATE_API_KEY/GATE_API_SECRET not set")
	}
	if redisURL == "" {
		redisURL = "redis://localhost:6379/0"
	}

	// Redis
	opt, _ := redis.ParseURL(redisURL)
	rdb = redis.NewClient(opt)
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("Redis connect failed: %v", err)
	}
	log.Println("✅ Redis connected")

	// Verify Gate.io
	bal := getBalance()
	log.Printf("✅ Gate.io connected, balance: $%.4f", bal)
	rdb.Set(ctx, "shark:balance", bal, 10*time.Second)

	// Subscribe to trade commands
	pubsub := rdb.Subscribe(ctx, "shark:orders:new")
	defer pubsub.Close()
	ch := pubsub.Channel()

	log.Println("🦈 Shark Executor ready, waiting for orders...")

	// Balance refresh ticker
	balTicker := time.NewTicker(30 * time.Second)
	defer balTicker.Stop()

	for {
		select {
		case msg := <-ch:
			var cmd TradeCmd
			if err := json.Unmarshal([]byte(msg.Payload), &cmd); err != nil {
				log.Printf("invalid command: %v", err)
				continue
			}
			log.Printf("📩 收到指令: %s %s %s [%s]", cmd.Symbol, cmd.Side, cmd.Action, cmd.Mode)
			if cmd.Mode == "paper" {
				continue // paper 由 matcher 处理
			}
			if err := validateTradeCmd(cmd); err != nil {
				log.Printf("reject command: %v", err)
				continue
			}
			switch cmd.Action {
			case "open":
				executeOpen(cmd)
			case "close":
				executeClose(cmd)
			}

		case <-balTicker.C:
			bal := getBalance()
			rdb.Set(ctx, "shark:balance", bal, 60*time.Second)
		}
	}
}
