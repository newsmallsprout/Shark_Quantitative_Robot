package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha512"
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
	Symbol     string  `json:"symbol"`
	Side       string  `json:"side"`
	Size       int     `json:"size"`
	Leverage   int     `json:"leverage"`
	Action     string  `json:"action"` // "open" or "close"
	Mode       string  `json:"mode"`   // "paper" or "live"
	StopLoss   float64 `json:"stop_loss,omitempty"`
	TakeProfit float64 `json:"take_profit,omitempty"`
}

func executePaper(cmd TradeCmd) {
	// Read current price from Redis
	priceStr, err := rdb.Get(ctx, "shark:price:"+cmd.Symbol).Result()
	if err != nil {
		log.Printf("paper: no price for %s, skip", cmd.Symbol)
		return
	}
	price, _ := strconv.ParseFloat(priceStr, 64)
	oid := fmt.Sprintf("paper-%d", time.Now().UnixNano())

	msg := fmt.Sprintf("{\"symbol\":\"%s\",\"side\":\"%s\",\"action\":\"%s\",\"price\":%.4f,\"order_id\":\"%s\"}",
		cmd.Symbol, cmd.Side, cmd.Action, price, oid)
	rdb.Publish(ctx, "shark:orders:status", msg)
	log.Printf("📝 纸盘 %s %s %s @%.4f", cmd.Symbol, cmd.Side, cmd.Action, price)
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
		"text": fmt.Sprintf("t-shark-%d", time.Now().UnixNano()),
	})
	if err != nil {
		log.Printf("❌ 开仓失败 %s %s: %v", cmd.Symbol, cmd.Side, err)
		rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, "failed", 0)
		return
	}
	log.Printf("✅ 开仓 %s %s size=%d status=%v", cmd.Symbol, cmd.Side, cmd.Size, r["status"])

	// ── 挂止盈止损条件单 ──
	if cmd.StopLoss > 0 {
		slSize := size
		if slSize < 0 {
			slSize = -slSize
		}
		// 止损：stop-market
		slRule := 2 // long: price<=trigger, short: price>=trigger
		if cmd.Side == "short" {
			slRule = 1
		}
		slR, slErr := gateAPI("POST", "/price_orders", map[string]interface{}{
			"contract": contract,
			"size":     slSize,
			"price":    "0",
			"trigger": map[string]interface{}{
				"price":      strconv.FormatFloat(cmd.StopLoss, 'f', 1, 64),
				"rule":       slRule,
				"expiration": 3600,
			},
			"reduce_only": true,
			"text":        fmt.Sprintf("t-shark-sl-%d", time.Now().UnixNano()),
		})
		if slErr != nil {
			log.Printf("⚠ 止损单挂失败 %s: %v", cmd.Symbol, slErr)
		} else {
			log.Printf("🛑 止损 %s @%.1f status=%v", cmd.Symbol, cmd.StopLoss, slR["status"])
		}
	}
	if cmd.TakeProfit > 0 {
		tpSize := size
		if tpSize < 0 {
			tpSize = -tpSize
		}
		// 止盈：limit
		tpRule := 1 // long: price>=trigger, short: price<=trigger
		if cmd.Side == "short" {
			tpRule = 2
		}
		tpR, tpErr := gateAPI("POST", "/price_orders", map[string]interface{}{
			"contract": contract,
			"size":     tpSize,
			"price":    "0",
			"trigger": map[string]interface{}{
				"price":      strconv.FormatFloat(cmd.TakeProfit, 'f', 1, 64),
				"rule":       tpRule,
				"expiration": 3600,
			},
			"reduce_only": true,
			"text":        fmt.Sprintf("t-shark-tp-%d", time.Now().UnixNano()),
		})
		if tpErr != nil {
			log.Printf("⚠ 止盈单挂失败 %s: %v", cmd.Symbol, tpErr)
		} else {
			log.Printf("🎯 止盈 %s @%.1f status=%v", cmd.Symbol, cmd.TakeProfit, tpR["status"])
		}
	}

	rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, "open_ok", 0)
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
		"text":        fmt.Sprintf("t-shark-close-%d", time.Now().UnixNano()),
	})
	if err != nil {
		log.Printf("❌ 平仓失败 %s %s: %v", cmd.Symbol, cmd.Side, err)
		rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, "close_failed", 0)
		return
	}
	log.Printf("✅ 平仓 %s %s status=%v", cmd.Symbol, cmd.Side, r["status"])
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
