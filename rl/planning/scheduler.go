package planning

import (
	"context"
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

	for {
		select {
		case <-ticker.C:
			log.Println("[Planning] ⏰ 定时计划更新触发...")
			s.planner.PlanAll(ctx)
		case <-ctx.Done():
			log.Println("[Planning] 调度器停止")
			return
		}
	}
}
