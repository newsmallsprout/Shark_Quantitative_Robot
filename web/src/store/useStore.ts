import { create } from 'zustand'

export interface Position {
  symbol: string; side: string; size: number; entry_price: number;
  leverage: number; margin: number; unrealized_pnl: number; pnl_pct: number;
  current_price?: number;
  entry_risk_tag?: string;
}

export interface LivePrice {
  price: number; change: number;
}

/** 与 main.py `_state["live"]` 对齐；纸面模式下通常不含此字段 */
export interface LiveStatus {
  active: boolean
  trading_enabled?: boolean
  balance?: number
  positions?: number
  order_errors?: number
  consecutive_errors?: number
  last_sync?: number
}

export interface PaperStatus {
  active: boolean
  trading_enabled: boolean
}

export interface CharacterEvent {
  Event_Type: string;
  Action_Code: string;
  Facial_Expression: string;
  Emotion_Index: number;
  Speech_Text: string;
  Evolution_Log?: string;
  symbol?: string;
  side?: string;
  pnl?: number;
  pnl_pct?: number;
  /** 内部：异步 LLM 回写台词时对齐最新事件 */
  _seq?: number;
}

export interface Status {
  equity: number; balance: number; free_cash: number; positions: number;
  realized_pnl: number; win_rate: number;
  safety_blocked: boolean; mode: string;
  fuse_reason?: string
  live_api_ok?: boolean
  last_tick_block?: { code: string; detail: string; ts?: number } | null
  initial_capital: number;
  unrealized_pnl: number;
  position_list: Position[];
  live_prices: Record<string, LivePrice>;
  total_fees: number;
  total_slippage: number;
  trade_history: any[];
  margin_locked: number;
  character_event?: CharacterEvent;
  volatility?: number;
  /** Gate 实盘引擎状态；未启用 live 时为 undefined */
  live?: LiveStatus
  /** 模拟盘状态 */
  paper?: PaperStatus
  /** 与进程内 SHARK_MODE / set_runtime_mode 对齐 */
  shark_mode: 'paper' | 'live'
  /** 待审批进化修改 */
  evo_pending?: EvoChange[]
  /** Gate 接口动态筛出的高波动山寨池 */
  dynamic_high_vol_alts?: string[]
  /** 当前双轨策略配置摘要 */
  strategy_profile?: StrategyProfile
  /** Go 计划服务当前重规划状态 */
  planning_status?: PlanningStatus
}

export interface PlanningStatus {
  active?: boolean
  phase?: string
  symbol?: string
  message?: string
  done?: number
  total?: number
  ts?: number
}

export interface StrategyProfile {
  stable_capital_pct?: number
  alt_capital_pct?: number
  stable_profile?: string
  alt_profile?: string
  alt_plan_ttl_sec?: number
}

export interface EvoChange {
  id: number
  type: string
  description: string
  params: Record<string, unknown>
  created_at: number
}

interface Store {
  status: Status;
  connected: boolean;
  setStatus: (s: Partial<Status>) => void;
  setConnected: (c: boolean) => void;
}

export const useStore = create<Store>((set) => ({
  status: {
    equity: 100, balance: 100, free_cash: 100, positions: 0,
    initial_capital: 100, unrealized_pnl: 0,
    realized_pnl: 0, win_rate: 0,
    safety_blocked: false,
    fuse_reason: '',
    live_api_ok: true,
    last_tick_block: null,
    mode: 'Paper',
    position_list: [],
    live_prices: {},
    total_fees: 0,
    total_slippage: 0,
    trade_history: [],
    margin_locked: 0,
    live: undefined,
    paper: { active: true, trading_enabled: false },
    shark_mode: 'paper',
    dynamic_high_vol_alts: [],
    planning_status: { active: false, phase: 'idle', message: '等待计划刷新', done: 0, total: 0 },
    strategy_profile: undefined,
  },
  connected: false,
  setStatus: (s) => set((st) => ({ status: { ...st.status, ...s } })),
  setConnected: (c) => set({ connected: c }),
}))
