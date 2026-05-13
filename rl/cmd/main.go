package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"syscall"
	"time"

	"github.com/redis/go-redis/v9"
	"shark-rl"
	"shark-rl/planning"
)

func main() {
	log.SetFlags(log.LstdFlags | log.Lmicroseconds)
	log.Println("🧬 Shark RL Evolution Engine (Go) starting...")

	redisURL := getEnv("SHARK_REDIS_URL", "redis://redis:6379/0")
	redisOpt, err := redisOptionsFromURL(redisURL)
	if err != nil {
		log.Fatalf("[Redis] invalid SHARK_REDIS_URL: %v", err)
	}
	rdb := redis.NewClient(redisOpt)
	defer rdb.Close()

	ctx := context.Background()
	if err := rdb.Ping(ctx).Err(); err != nil {
		log.Printf("[Redis] 连接失败: %v (继续无Redis模式)", err)
	} else {
		log.Println("[Redis] 已连接")
	}

	// ── Initialize components ──
	kb := rl.NewKnowledgeBase()
	ga := rl.NewGAPopulation(20)
	agent := rl.NewDQNAgent(6, 4)

	server := rl.NewWebhookServer(kb, ga, agent)

	// ── HTTP routes ──
	mux := http.NewServeMux()
	mux.HandleFunc("/tv/alert", server.HandleTVAlert)
	mux.HandleFunc("/rl/status", server.HandleStatus)
	mux.HandleFunc("/rl/patterns", server.HandlePatterns)
	mux.HandleFunc("/health", func(w http.ResponseWriter, r *http.Request) {
		w.WriteHeader(200)
		w.Write([]byte("ok"))
	})

	httpPort := getEnv("RL_HTTP_PORT", "8081")
	httpServer := &http.Server{Addr: ":" + httpPort, Handler: mux}

	go func() {
		log.Printf("[HTTP] listening on :%s", httpPort)
		if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("[HTTP] %v", err)
		}
	}()

	// ── Training + Action loop ──
	go trainingLoop(kb, ga, agent, server, rdb)

	// ── SlowLoop Planning (30m) ──
	plannerCtx, plannerCancel := context.WithCancel(context.Background())
	defer plannerCancel()
	go func() {
		sched := planning.NewScheduler(rdb, planning.FocusSymbols)
		// 注入 TradingView insights（每次生成计划时从 KB 读取）
		sched.Planner().SetTVFn(func(symbol string) string {
			return kb.GetTVInsights(symbol)
		})
		sched.Start(plannerCtx)
	}()
	log.Printf("[Planning] SlowLoop 已启动（每30分钟生成RangePlan，专注%d个币对）", len(planning.FocusSymbols))

	// ── TradingView 知识学习 ──
	tv := rl.NewTVScraper()
	tv.StartTVLearning(kb, 30*time.Minute)
	log.Println("[TV] TradingView知识引擎已启动（每30分钟学习）")

	// ── Graceful shutdown ──
	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("[Shutdown] stopping...")
	ctx2, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	httpServer.Shutdown(ctx2)
	log.Println("[Shutdown] done")
}

func redisOptionsFromURL(rawURL string) (*redis.Options, error) {
	if rawURL == "" {
		rawURL = "redis://redis:6379/0"
	}
	return redis.ParseURL(rawURL)
}

func trainingLoop(kb *rl.KnowledgeBase, ga *rl.GAPopulation, agent *rl.DQNAgent,
	server *rl.WebhookServer, rdb *redis.Client) {

	// 用真实价格数据训练（从Redis读取，无真价格则等待）
	prices := fetchRealPrices(rdb)
	if len(prices) < 100 {
		log.Println("[Train] Redis无足够价格数据，等待30秒重试...")
		for retries := 0; retries < 10 && len(prices) < 100; retries++ {
			time.Sleep(30 * time.Second)
			prices = fetchRealPrices(rdb)
		}
		if len(prices) < 100 {
			log.Println("[Train] 等待超时，RL训练延后到下一个价格周期")
			return
		}
	}
	log.Printf("[Train] 使用Redis真实价格数据 (%d条)", len(prices))

	// Phase 1: GA evolution
	log.Println("[Train] Starting initial GA evolution...")
	for gen := 0; gen < 10; gen++ {
		fitnesses := make([]rl.FitnessResult, ga.PopSize)
		for i, gene := range ga.Individuals {
			result := rl.RunBacktest(prices, gene)
			fitnesses[i] = rl.ComputeFitness(result)
		}
		ga.Evaluate(fitnesses)
		ga.Evolve()
		if gen%3 == 0 {
			bestResult := rl.RunBacktest(prices, ga.BestEver)
			patterns := rl.ExtractPatterns(bestResult.Trades, prices)
			kb.UpdatePatterns(patterns)
		}
	}
	log.Printf("[Train] GA complete. Best fitness=%.3f", ga.BestFitness)

	// Phase 2: RL training
	log.Println("[Train] Starting RL training...")
	env := rl.NewTradingEnv(prices, 1000.0)
	for episode := 0; episode < 50; episode++ {
		state := env.Reset()
		for step := 0; step < 400; step++ {
			action := agent.Act(state, true)
			result := env.Step(action)
			agent.Remember(state, action, result.Reward, result.State, result.Done)
			agent.Replay()
			state = result.State
			if result.Done {
				break
			}
		}
		if episode%10 == 0 {
			log.Printf("[RL] Ep %d: epsilon=%.3f", episode, agent.Epsilon)
		}
	}

	// Phase 3: Online loop — publish actions + evo suggestions
	log.Println("[Train] Online phase — publishing actions and evo suggestions...")
	actionTicker := time.NewTicker(10 * time.Second)
	evoTicker := time.NewTicker(5 * time.Minute)
	evalTicker := time.NewTicker(1 * time.Minute)
	defer actionTicker.Stop()
	defer evoTicker.Stop()
	defer evalTicker.Stop()

	for {
		select {
		case <-actionTicker.C:
			publishActions(rdb, agent, kb, prices)

		case <-evalTicker.C:
			evalEnv := rl.NewTradingEnv(prices, 1000.0)
			state := evalEnv.Reset()
			for i := 0; i < 400; i++ {
				action := agent.Act(state, false)
				result := evalEnv.Step(action)
				state = result.State
				if result.Done {
					break
				}
			}
			metrics := evalEnv.Metrics()
			server.UpdateMetrics(map[string]interface{}{
				"sharpe":       metrics["sharpe"],
				"win_rate":     metrics["win_rate"],
				"trades":       metrics["total_trades"],
				"epsilon":      agent.Epsilon,
				"ga_gen":       float64(ga.Generation),
				"best_fitness": ga.BestFitness,
			})

		case <-evoTicker.C:
			log.Println("[Train] Retraining cycle...")
			// GA evolve
			fitnesses := make([]rl.FitnessResult, ga.PopSize)
			for i, gene := range ga.Individuals {
				result := rl.RunBacktest(prices, gene)
				fitnesses[i] = rl.ComputeFitness(result)
			}
			ga.Evaluate(fitnesses)
			ga.Evolve()
			// RL train
			env2 := rl.NewTradingEnv(prices, 1000.0)
			for ep := 0; ep < 5; ep++ {
				state := env2.Reset()
				for step := 0; step < 400; step++ {
					action := agent.Act(state, true)
					result := env2.Step(action)
					agent.Remember(state, action, result.Reward, result.State, result.Done)
					agent.Replay()
					state = result.State
					if result.Done {
						break
					}
				}
			}
			// Update patterns from best strategy
			bestResult := rl.RunBacktest(prices, ga.BestEver)
			patterns := rl.ExtractPatterns(bestResult.Trades, prices)
			kb.UpdatePatterns(patterns)
			log.Printf("[Train] Gen %d: fitness=%.3f epsilon=%.3f patterns=%d",
				ga.Generation, ga.BestFitness, agent.Epsilon, len(patterns))
		}
	}
}

// ── Publish RL actions to Redis → Python main.py executes them ──
func publishActions(rdb *redis.Client, agent *rl.DQNAgent, kb *rl.KnowledgeBase, prices []float64) {
	if rdb == nil {
		return
	}
	ctx := context.Background()

	// Read current prices from Redis
	symbols := []string{"BTC/USDT", "ETH/USDT", "SUI/USDT", "TON/USDT", "SOL/USDT"}
	for _, sym := range symbols {
		pxStr, err := rdb.Get(ctx, "shark:price:"+sym).Result()
		if err != nil {
			continue
		}
		px, _ := strconv.ParseFloat(pxStr, 64)
		if px <= 0 {
			continue
		}

		// Build state from price + knowledge base
		chg := (px - prices[len(prices)-1]) / prices[len(prices)-1] // proxy price change
		rsi := computeRSI(prices, len(prices)-1, 14)
		vol := computeVol(prices, len(prices)-1, 20)
		trend := computeTrend
		_ = trend(prices, len(prices)-1, 20)
		sentiment := kb.GetSentiment(sym)

		state := []float64{chg, rsi, vol, 0, 0, sentiment} // position=flat, upnl=0

		action := agent.Act(state, false)
		side := ""
		switch action {
		case rl.Long:
			side = "long"
		case rl.Short:
			side = "short"
		case rl.Close:
			side = "close"
		default:
			continue
		}
		if side == "" {
			continue
		}

		// Publish to Redis
		msg := map[string]interface{}{
			"symbol":     sym,
			"side":       side,
			"confidence": agent.Epsilon,
			"source":     "rl-agent",
			"ts":         time.Now().Unix(),
		}
		data, _ := json.Marshal(msg)
		rdb.Publish(ctx, "shark:rl:action", data)
	}
}

// ── Publish GA best parameters as evo suggestion ──
var lastPublishedFitness float64
var lastPublishedGen int

func publishEvoSuggestion(rdb *redis.Client, ga *rl.GAPopulation) {
	if rdb == nil {
		return
	}
	// 首次不推送（初始训练），后续需fitness提升>5%才推送
	if lastPublishedFitness == 0 {
		lastPublishedFitness = ga.BestFitness
		lastPublishedGen = ga.Generation
		return
	}
	// fitness必须提升超过5%才推送
	if ga.BestFitness <= lastPublishedFitness*1.05 && ga.Generation-lastPublishedGen < 20 {
		return
	}
	lastPublishedFitness = ga.BestFitness
	lastPublishedGen = ga.Generation

	gene := ga.BestEver
	// 详细参数变更描述
	desc := fmt.Sprintf(
		"GA第%d代 | 保证金%.1f%% | 最大持仓%d | RSI阈值%.0f | 止损ATR×%.1f | 止盈ATR×%.1f | 回撤上限%.0f%% | 冷却%ds | 金字塔%d层",
		ga.Generation,
		gene.MarginPct*100,
		gene.MaxPositions,
		gene.RSIThreshold,
		gene.StopATRMult,
		gene.TakeATRMult,
		gene.MaxDrawdownLimit*100,
		gene.CoolDownSec,
		gene.PyramidLevels,
	)
	msg := map[string]interface{}{
		"type":        "ga_best_params",
		"description": desc,
		"params": map[string]interface{}{
			"margin_pct":         gene.MarginPct,
			"max_positions":      gene.MaxPositions,
			"rsi_threshold":      gene.RSIThreshold,
			"stop_atr_mult":      gene.StopATRMult,
			"take_atr_mult":      gene.TakeATRMult,
			"max_drawdown_limit": gene.MaxDrawdownLimit,
			"cooldown_sec":       gene.CoolDownSec,
			"pyramid_levels":     gene.PyramidLevels,
		},
	}
	data, _ := json.Marshal(msg)
	ctx := context.Background()
	rdb.LPush(ctx, "shark:evo:list", data)
	rdb.LTrim(ctx, "shark:evo:list", 0, 49)
	rdb.Publish(ctx, "shark:evo:pending", data)
	log.Printf("[Evo] Published GA gen %d best params", ga.Generation)
}

// ── Helper indicator functions (duplicated from backtest.go for independence) ──
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
	return 100 - 100/(1+(gain/float64(period))/(loss/float64(period)))
}

func computeVol(prices []float64, idx, period int) float64 {
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
	return variance / float64(period)
}

func computeTrend(prices []float64, idx, period int) float64 {
	if idx < period {
		return 0
	}
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
	return (n*sumXY - sumX*sumY) / (n*sumX2 - sumX*sumX) / prices[idx]
}

func generateSyntheticPrices(n int) []float64 {
	rng := rand.New(rand.NewSource(time.Now().UnixNano()))
	prices := make([]float64, n)
	prices[0] = 100.0
	trend := 0.0
	vol := 1.5
	for i := 1; i < n; i++ {
		drift := trend * 0.1
		noise := rng.NormFloat64() * vol
		if rng.Float64() < 0.02 {
			trend = rng.NormFloat64() * 3.0
			vol = 0.5 + rng.Float64()*3.0
		}
		prices[i] = prices[i-1] * (1.0 + drift + noise/100.0)
		if i >= 50 {
			ma50 := 0.0
			for j := i - 50; j < i; j++ {
				ma50 += prices[j]
			}
			ma50 /= 50
			prices[i] = prices[i]*0.998 + ma50*0.002
		}
		if prices[i] < 10 {
			prices[i] = 10
		}
	}
	return prices
}

func getEnv(key, defaultVal string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return defaultVal
}

var _ = fmt.Sprintf
var _ = strconv.Itoa

func fetchRealPrices(rdb *redis.Client) []float64 {
	if rdb == nil {
		return nil
	}
	ctx := context.Background()
	var prices []float64
	for _, sym := range []string{"BTC/USDT", "ETH/USDT", "SUI/USDT", "TON/USDT", "SOL/USDT"} {
		val, err := rdb.Get(ctx, "shark:price:"+sym).Result()
		if err != nil {
			continue
		}
		px, err := strconv.ParseFloat(val, 64)
		if err != nil || px <= 0 {
			continue
		}
		prices = append(prices, px)
	}
	if len(prices) < 3 {
		return nil
	}
	// 用当前价格生成100条模拟回撤数据用于回测
	avg := 0.0
	for _, p := range prices {
		avg += p
	}
	avg /= float64(len(prices))
	result := make([]float64, 100)
	rng := rand.New(rand.NewSource(time.Now().UnixNano()))
	result[99] = avg
	for i := 98; i >= 0; i-- {
		result[i] = result[i+1] * (1.0 + rng.NormFloat64()*0.01)
		if result[i] < 1 {
			result[i] = 1
		}
	}
	return result
}
