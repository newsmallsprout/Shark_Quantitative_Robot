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

func init() {
	if redisURL == "" {
		redisURL = "redis://redis:6379/0"
	}
	opt, _ := redis.ParseURL(redisURL)
	rdb = redis.NewClient(opt)
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Fatalf("Redis: %v", err)
	}
	log.Println("✅ Redis connected")
}

type TradeCmd struct {
	Symbol string `json:"symbol"`
	Side   string `json:"side"`
	Action string `json:"action"`
	Mode   string `json:"mode"`
}

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)

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

		// 读 Redis 价格
		priceStr, err := rdb.Get(ctx, "shark:price:"+cmd.Symbol).Result()
		if err != nil {
			log.Printf("⏳ %s 无价格，跳过", cmd.Symbol)
			continue
		}
		price, _ := strconv.ParseFloat(priceStr, 64)
		oid := fmt.Sprintf("paper-%d", time.Now().UnixNano())

		status := map[string]interface{}{
			"symbol":   cmd.Symbol,
			"side":     cmd.Side,
			"action":   cmd.Action,
			"price":    price,
			"order_id": oid,
			"mode":     "paper",
		}
		b, _ := json.Marshal(status)
		rdb.Publish(ctx, "shark:orders:status", string(b))
		log.Printf("📝 %s %s %s @%.4f", cmd.Symbol, cmd.Side, cmd.Action, price)
	}
}
