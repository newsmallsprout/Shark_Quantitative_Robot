package planning

import (
	"context"
	"encoding/json"
	"log"
	"time"

	"github.com/redis/go-redis/v9"
)

// Scheduler — SlowLoop 调度器：首次 Bootstrap + 每30分钟全量 Planning
type Scheduler struct {
	planner  *Planner
	interval time.Duration
}

func NewScheduler(rdb *redis.Client, symbols []string) *Scheduler {
	return &Scheduler{
		planner:  NewPlanner(rdb, symbols),
		interval: 30 * time.Minute,
	}
}

func (s *Scheduler) Planner() *Planner { return s.planner }

func (s *Scheduler) Start(ctx context.Context) {
	// Phase 1: Bootstrap（全量数据拉取 + 首批计划生成）
	log.Println("[Planning] 开始 BOOTSTRAP...")
	if err := s.planner.Bootstrap(ctx); err != nil {
		log.Printf("[Planning] BOOTSTRAP 失败: %v — 30秒后重试", err)
		select {
		case <-time.After(30 * time.Second):
			if err := s.planner.Bootstrap(ctx); err != nil {
				log.Printf("[Planning] BOOTSTRAP 二次失败: %v — 进入定时重试模式", err)
			}
		case <-ctx.Done():
			return
		}
	}

	// Phase 2: 定时循环
	log.Printf("[Planning] 定时循环已启动（每%d分钟全量更新）", int(s.interval.Minutes()))
	ticker := time.NewTicker(s.interval)
	defer ticker.Stop()
	replanCh := s.replanChannel(ctx)

	for {
		select {
		case <-ticker.C:
			log.Println("[Planning] ⏰ 定时计划更新触发...")
			s.planner.PlanAll(ctx)
		case symbol := <-replanCh:
			if symbol != "" {
				log.Printf("[Planning] 🔁 单币种强制重规划: %s", symbol)
				if err := s.planner.Plan(ctx, symbol, nil); err != nil {
					log.Printf("[Planning] 强制重规划失败(%s): %v", symbol, err)
				}
			}
		case <-ctx.Done():
			log.Println("[Planning] 调度器停止")
			return
		}
	}
}

func (s *Scheduler) replanChannel(ctx context.Context) <-chan string {
	out := make(chan string, 8)
	pubsub := s.planner.rdb.Subscribe(ctx, "shark:plan:replan")
	go func() {
		defer close(out)
		defer pubsub.Close()
		ch := pubsub.Channel()
		for {
			select {
			case msg, ok := <-ch:
				if !ok {
					return
				}
				if symbol := parseReplanSymbol(msg.Payload); symbol != "" {
					select {
					case out <- symbol:
					case <-ctx.Done():
						return
					}
				}
			case <-ctx.Done():
				return
			}
		}
	}()
	return out
}

func parseReplanSymbol(payload string) string {
	var req struct {
		Symbol string `json:"symbol"`
	}
	if err := json.Unmarshal([]byte(payload), &req); err == nil && IsLargeCap(req.Symbol) {
		return req.Symbol
	}
	if IsLargeCap(payload) {
		return payload
	}
	return ""
}
