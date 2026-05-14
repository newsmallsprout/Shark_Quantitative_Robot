package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strconv"
	"time"

	"github.com/redis/go-redis/v9"
)

var (
	redisURL = os.Getenv("SHARK_REDIS_URL")
	rdb      *redis.Client
	ctx      = context.Background()
)

func connectRedis() *redis.Client {
	if redisURL == "" {
		redisURL = "redis://redis:6379/0"
	}
	opt, _ := redis.ParseURL(redisURL)
	client := redis.NewClient(opt)
	if err := client.Ping(ctx).Err(); err != nil {
		log.Fatalf("Redis: %v", err)
	}
	log.Println("✅ Redis connected")
	return client
}

type TradeCmd struct {
	Symbol     string  `json:"symbol"`
	Side       string  `json:"side"`
	Action     string  `json:"action"`
	Mode       string  `json:"mode"`
	Size       int     `json:"size"`
	Leverage   int     `json:"leverage"`
	StopLoss   float64 `json:"stop_loss,omitempty"`
	TakeProfit float64 `json:"take_profit,omitempty"`
}

func validateTradeCmd(cmd TradeCmd) error {
	if cmd.Mode != "paper" {
		return fmt.Errorf("unsupported mode %q", cmd.Mode)
	}
	if cmd.Action != "open" && cmd.Action != "close" {
		return fmt.Errorf("unsupported action %q", cmd.Action)
	}
	if cmd.Side != "long" && cmd.Side != "short" {
		return fmt.Errorf("unsupported side %q", cmd.Side)
	}
	if cmd.Symbol == "" {
		return fmt.Errorf("symbol required")
	}
	if cmd.Size <= 0 {
		return fmt.Errorf("size must be positive")
	}
	if cmd.Leverage < 1 || cmd.Leverage > 125 {
		return fmt.Errorf("leverage out of range")
	}
	return nil
}

func paperStatusValue(cmd TradeCmd) string {
	return cmd.Action + "_ok"
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	rdb = connectRedis()
	defer rdb.Close()

	pubsub := rdb.Subscribe(ctx, "shark:orders:new")
	defer pubsub.Close()
	ch := pubsub.Channel()

	log.Println("🦈 Shark Matcher ready (paper only)")

	for msg := range ch {
		var cmd TradeCmd
		if err := json.Unmarshal([]byte(msg.Payload), &cmd); err != nil {
			continue
		}
		if cmd.Mode != "paper" {
			continue // 只处理纸盘
		}
		if err := validateTradeCmd(cmd); err != nil {
			log.Printf("reject paper command: %v", err)
			continue
		}

		// 读 Redis 价格
		priceStr, err := rdb.Get(ctx, "shark:price:"+cmd.Symbol).Result()
		if err != nil {
			log.Printf("⏳ %s 无价格，跳过", cmd.Symbol)
			continue
		}
		price, _ := strconv.ParseFloat(priceStr, 64)
		oid := fmt.Sprintf("paper-%d", time.Now().UnixNano())

		status := map[string]interface{}{
			"symbol":      cmd.Symbol,
			"side":        cmd.Side,
			"action":      cmd.Action,
			"price":       price,
			"order_id":    oid,
			"mode":        "paper",
			"size":        cmd.Size,
			"leverage":    cmd.Leverage,
			"stop_loss":   cmd.StopLoss,
			"take_profit": cmd.TakeProfit,
		}
		b, _ := json.Marshal(status)
		rdb.Publish(ctx, "shark:orders:status", string(b))
		rdb.Set(ctx, "shark:orders:status:"+cmd.Symbol, paperStatusValue(cmd), 0)
		log.Printf("📝 %s %s %s size=%d lev=%dx @%.4f", cmd.Symbol, cmd.Side, cmd.Action, cmd.Size, cmd.Leverage, price)
	}
}
