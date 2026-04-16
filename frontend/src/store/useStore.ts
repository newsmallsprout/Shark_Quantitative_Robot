import { create } from 'zustand';
import { apiFetch } from '../apiClient';

export type SystemMode = 'NEUTRAL' | 'ATTACK' | 'BERSERKER' | 'HALTED';

export interface Position {
  symbol: string;
  side: 'long' | 'short';
  size: number;
  entryPrice: number;
  unrealizedPnl: number;
  /** 相对初始保证金的收益率 %（ROE） */
  pnlPercent: number;
  /** 相对开仓名义的收益率 %（与「名义」列同一分母） */
  pnlPercentNotional?: number;
  leverage?: number;
  margin_mode?: string;
  contractSize?: number;
  notionalOpenUsdt?: number;
  /** 开仓占用初始保证金 ≈ 名义÷杠杆（USDT） */
  initialMarginUsdt?: number;
  /** 括号限价止盈 / 模拟止损（无挂单时止损可能来自 active_sl） */
  takeProfitPrice?: number;
  stopLossPrice?: number;
  /** Taker 进 + Maker 平 近似回本价（雷达金色虚线） */
  breakEvenPrice?: number;
}

export interface AIInsight {
  regime: 'OSCILLATING' | 'TRENDING_UP' | 'TRENDING_DOWN' | 'CHAOTIC';
  score: number;
  reason: string;
  timestamp: number;
}

export interface ResonanceMetrics {
  obi: number;
  tech_indicator: number;
  tech_signal: 'bullish' | 'bearish' | 'neutral';
  ai_score: number;
  ai_regime: string;
  /** LLM 一句简报（与当前品种异步研究员同步） */
  ai_reason: string;
  atr_pct: number;
  target_leverage: number;
  /** 狂暴模式该品种档位上限（BTC/ETH 200 等），与 Kelly 目标杠杆无关 */
  berserker_max_leverage: number;
  current_risk_exposure: number;
  ws_latency: number;
  ws_reconnects: number;
  kill_switch_progress: {
    active: boolean;
    executed_chunks: number;
    total_chunks: number;
    avg_slippage: number;
  };
  adaptation: {
    probe_mode: boolean;
    adaptation_level: number;
    adaptation_label: string;
    window_trades: number;
    window_win_rate: number;
    consecutive_losses: number;
    recovery_win_rate: number;
    live_attack_ai_threshold: number;
    live_neutral_ai_threshold: number;
    live_funding_signal_weight: number;
    live_attack_align_bps: number;
    live_margin_cap_usdt: number;
    strongest_symbol: string;
    strongest_symbol_win_rate: number;
    strongest_symbol_trades: number;
    weakest_symbol: string;
    weakest_symbol_win_rate: number;
    weakest_symbol_trades: number;
    dominant_win_reason: string;
    dominant_win_reason_count: number;
    dominant_loss_reason: string;
    dominant_loss_reason_count: number;
    strongest_strategy: string;
    strongest_strategy_win_rate: number;
    strongest_strategy_trades: number;
    strongest_scene: {
      regime?: string;
      symbol?: string;
      side?: string;
      strategy?: string;
      quadrant?: string;
      ai_bucket?: string;
      wr?: number;
      n?: number;
      net?: number;
    };
    weakest_strategy: string;
    weakest_strategy_win_rate: number;
    weakest_strategy_trades: number;
    symbol_boosts: Record<string, { max_leverage?: number; berserker_obi_threshold?: number; win_rate?: number; trades?: number; net?: number }>;
  };
  beta_neutral_hf: {
    enabled: boolean;
    anchor_symbol: string;
    configured_leverage: number;
    tracked_symbols: string[];
    active_pairs: Array<{
      pair_id: string;
      alt: string;
      status: string;
      entry_zscore: number;
      live_zscore: number;
      beta: number;
      corr: number;
      effective_leverage: number;
      net_pnl_usdt: number;
      dynamic_cost_threshold_usdt: number;
      dynamic_take_profit_usdt: number;
      dynamic_stop_loss_usdt: number;
      profit_lock_floor_usdt: number;
      close_reason: string;
    }>;
    candidate_pairs: Array<{
      alt: string;
      zscore: number;
      beta: number;
      corr: number;
      score: number;
      status: string;
      direction: string;
    }>;
    recent_closed: Array<{
      pair_id: string;
      alt: string;
      anchor: string;
      entry_zscore: number;
      beta: number;
      reason: string;
      net_pnl: number;
      closed_at: number;
    }>;
    anchor_target_contracts: number;
    anchor_actual_contracts: number;
  };
}

export interface ContractSpecRow {
  symbol: string;
  last_price?: number;
  best_bid?: number;
  best_ask?: number;
  /** 24h 涨跌幅 %（Gate ticker change_percentage） */
  change_24h_pct?: number;
  funding_rate: number;
  mark_price: number;
  index_price: number;
  spread: number;
  next_funding_time?: number;
  volume_24h?: number;
  /** RiskEngine + ATR suggested grinder entry leverage (flat book). */
  target_leverage?: number;
  atr_pct_snapshot?: number;
}

interface FeedMeta {
  dataFeed: string;
  sandboxExecution: boolean;
}

/** 猎犬 OBI：最小跳变幅度 + 同标的冷却，避免 WS 每帧刷屏 */
const HOUND_MAX = 48;
const HOUND_OBI_MIN_DELTA = 0.18;
const HOUND_OBI_MIN_INTERVAL_MS = 2800;
const TRAIL_MAX = 40;

let houndObiLogState: { symbol: string; obi: number; t: number } | null = null;

interface AppState {
  equity: number;
  dailyPnl: number;
  dailyPnlPercent: number;
  mode: SystemMode;
  isRunning: boolean;
  positions: Position[];
  aiInsight: AIInsight | null;
  resonanceMetrics: ResonanceMetrics;
  activeSymbol: string;
  availableSymbols: string[];
  latestTick: { symbol: string; price: number; time: number } | null;
  contractSpecs: Record<string, ContractSpecRow>;
  feedMeta: FeedMeta;
  /** 各标的最近成交价轨迹（用于微型波动率 Sparkline） */
  symbolPriceTrail: Record<string, number[]>;
  /** 猎犬轨迹 / 本地战报流水 */
  houndTraces: string[];

  updateMarketData: () => void;
  fetchResonanceMetrics: () => Promise<void>;
  fetchContractSpecs: () => Promise<void>;
  /** Restore dashboard from REST after hard refresh (before / parallel to WebSocket). */
  hydrateDashboard: () => Promise<void>;
  setMode: (mode: SystemMode) => void;
  setActiveSymbol: (symbol: string) => void;
  initWebSocket: () => void;
  killSwitch: () => void;
  toggleEngine: () => void;
  pushHoundTrace: (line: string) => void;
}

let marketWs: WebSocket | null = null;

function mergeContractFromPayload(
  prev: Record<string, ContractSpecRow> | null,
  symbol: string,
  d: Record<string, unknown>
): Record<string, ContractSpecRow> {
  const base = { ...(prev || {}) };
  const p = base[symbol] || ({} as ContractSpecRow);
  const cs = (d.contract_specs || {}) as Record<string, number>;

  base[symbol] = {
    symbol,
    last_price: typeof d.last_price === 'number' ? d.last_price : p.last_price,
    best_bid: typeof d.best_bid === 'number' ? d.best_bid : p.best_bid,
    best_ask: typeof d.best_ask === 'number' ? d.best_ask : p.best_ask,
    change_24h_pct:
      typeof d.change_24h_pct === 'number' && !Number.isNaN(d.change_24h_pct)
        ? d.change_24h_pct
        : typeof cs.change_24h_pct === 'number'
          ? cs.change_24h_pct
          : p.change_24h_pct,
    spread: typeof d.spread === 'number' ? d.spread : p.spread,
    funding_rate: cs.funding_rate ?? p.funding_rate ?? 0,
    mark_price: cs.mark_price ?? p.mark_price ?? 0,
    index_price: cs.index_price ?? p.index_price ?? 0,
    volume_24h: cs.volume_24h ?? p.volume_24h ?? 0,
    next_funding_time: p.next_funding_time,
    target_leverage:
      typeof d.target_leverage === 'number' && !Number.isNaN(d.target_leverage)
        ? d.target_leverage
        : p.target_leverage,
    atr_pct_snapshot:
      typeof d.atr_pct_snapshot === 'number' && !Number.isNaN(d.atr_pct_snapshot)
        ? d.atr_pct_snapshot
        : p.atr_pct_snapshot,
  };
  return base;
}

function mapAccountPosition(p: Record<string, unknown>): Position {
  const sideRaw = String(p.side || 'long').toLowerCase();
  const bep = p.break_even_price ?? p.breakEvenPrice;
  return {
    symbol: String(p.symbol ?? ''),
    side: sideRaw === 'short' ? 'short' : 'long',
    size: Number(p.size ?? 0),
    entryPrice: Number(p.entry_price ?? p.entryPrice ?? 0),
    unrealizedPnl: Number(p.unrealized_pnl ?? p.unrealizedPnl ?? 0),
    pnlPercent: Number(p.pnl_percent ?? p.pnlPercent ?? 0),
    pnlPercentNotional:
      p.pnl_percent_notional != null || p.pnlPercentNotional != null
        ? Number(p.pnl_percent_notional ?? p.pnlPercentNotional)
        : undefined,
    leverage: p.leverage != null ? Number(p.leverage) : undefined,
    margin_mode: p.margin_mode != null ? String(p.margin_mode) : undefined,
    contractSize:
      p.contract_size != null || p.contractSize != null
        ? Number(p.contract_size ?? p.contractSize)
        : undefined,
    notionalOpenUsdt:
      p.notional_open_usdt != null || p.notionalOpenUsdt != null
        ? Number(p.notional_open_usdt ?? p.notionalOpenUsdt)
        : undefined,
    initialMarginUsdt: (() => {
      const v = p.initial_margin_usdt ?? p.initialMarginUsdt;
      if (v == null || v === '') return undefined;
      const n = Number(v);
      return Number.isFinite(n) && n >= 0 ? n : undefined;
    })(),
    takeProfitPrice: (() => {
      const v = p.take_profit_price ?? p.takeProfitPrice;
      if (v == null || v === '') return undefined;
      const n = Number(v);
      return Number.isFinite(n) && n > 0 ? n : undefined;
    })(),
    stopLossPrice: (() => {
      const v = p.stop_loss_price ?? p.stopLossPrice;
      if (v == null || v === '') return undefined;
      const n = Number(v);
      return Number.isFinite(n) && n > 0 ? n : undefined;
    })(),
    breakEvenPrice: bep != null && Number.isFinite(Number(bep)) ? Number(bep) : undefined,
  };
}

export const useStore = create<AppState>((set, get) => ({
  equity: 0,
  dailyPnl: 0,
  dailyPnlPercent: 0,
  mode: 'NEUTRAL',
  isRunning: true,
  positions: [],
  aiInsight: null,
  resonanceMetrics: {
    obi: 0.0,
    tech_indicator: 0.0,
    tech_signal: 'neutral',
    ai_score: 50.0,
    ai_regime: 'OSCILLATING',
    ai_reason: '',
    atr_pct: 0.02,
    target_leverage: 1.0,
    berserker_max_leverage: 100,
    current_risk_exposure: 0.0,
    ws_latency: 0,
    ws_reconnects: 0,
    kill_switch_progress: {
      active: false,
      executed_chunks: 0,
      total_chunks: 5,
      avg_slippage: 0.0,
    },
    adaptation: {
      probe_mode: false,
      adaptation_level: 0,
      adaptation_label: 'NORMAL',
      window_trades: 0,
      window_win_rate: 1.0,
      consecutive_losses: 0,
      recovery_win_rate: 1.0,
      live_attack_ai_threshold: 0,
      live_neutral_ai_threshold: 0,
      live_funding_signal_weight: 0,
      live_attack_align_bps: 0,
      live_margin_cap_usdt: 0,
      strongest_symbol: '',
      strongest_symbol_win_rate: 0,
      strongest_symbol_trades: 0,
      weakest_symbol: '',
      weakest_symbol_win_rate: 1,
      weakest_symbol_trades: 0,
      dominant_win_reason: '',
      dominant_win_reason_count: 0,
      dominant_loss_reason: '',
      dominant_loss_reason_count: 0,
      strongest_strategy: '',
      strongest_strategy_win_rate: 0,
      strongest_strategy_trades: 0,
      strongest_scene: {},
      weakest_strategy: '',
      weakest_strategy_win_rate: 1,
      weakest_strategy_trades: 0,
      symbol_boosts: {},
    },
    beta_neutral_hf: {
      enabled: false,
      anchor_symbol: 'BTC/USDT',
      configured_leverage: 0,
      tracked_symbols: [],
      active_pairs: [],
      candidate_pairs: [],
      recent_closed: [],
      anchor_target_contracts: 0,
      anchor_actual_contracts: 0,
    },
  },

  activeSymbol: 'BTC/USDT',
  availableSymbols: ['BTC/USDT'],
  latestTick: null,
  contractSpecs: {},
  feedMeta: { dataFeed: '', sandboxExecution: false },
  symbolPriceTrail: {},
  houndTraces: [],

  pushHoundTrace: (line: string) => {
    const t = new Date().toISOString().replace('T', ' ').slice(0, 23);
    const entry = `${t} | ${line}`;
    set((s) => ({ houndTraces: [entry, ...s.houndTraces].slice(0, HOUND_MAX) }));
  },

  updateMarketData: () => {
    if (!get().isRunning) return;
    set((state) => {
      const noise = (Math.random() - 0.5) * 10;
      const newPnl = state.dailyPnl + noise;
      const newEquity = 10350 + newPnl;
      const newPositions = state.positions.map((p) => {
        const pnlNoise = (Math.random() - 0.5) * 5;
        return {
          ...p,
          unrealizedPnl: p.unrealizedPnl + pnlNoise,
          pnlPercent: p.pnlPercent + (pnlNoise / (p.size * p.entryPrice)) * 100,
        };
      });
      return {
        equity: newEquity,
        dailyPnl: newPnl,
        dailyPnlPercent: (newPnl / 10350) * 100,
        positions: newPositions,
      };
    });
  },

  fetchResonanceMetrics: async () => {},

  fetchContractSpecs: async () => {
    try {
      const response = await apiFetch('/api/contract_specs');
      const data = await response.json();
      if (!data || typeof data !== 'object') return;
      const merged: Record<string, ContractSpecRow> = { ...(get().contractSpecs || {}) };
      for (const sym of Object.keys(data)) {
        const row = data[sym] as Record<string, number>;
        merged[sym] = {
          symbol: sym,
          change_24h_pct: row.change_24h_pct ?? merged[sym]?.change_24h_pct,
          funding_rate: row.funding_rate ?? merged[sym]?.funding_rate ?? 0,
          mark_price: row.mark_price ?? merged[sym]?.mark_price ?? 0,
          index_price: row.index_price ?? merged[sym]?.index_price ?? 0,
          spread: row.spread ?? merged[sym]?.spread ?? 0,
          last_price: row.last_price ?? merged[sym]?.last_price,
          volume_24h: row.volume_24h ?? merged[sym]?.volume_24h ?? 0,
          next_funding_time: row.next_funding_time ?? merged[sym]?.next_funding_time,
        };
      }
      set({ contractSpecs: merged });
    } catch (error) {
      console.error('Failed to fetch contract specs', error);
    }
  },

  hydrateDashboard: async () => {
    try {
      const activeSym = encodeURIComponent(get().activeSymbol);
      const [cfgRes, accRes, specRes, resRes, statusRes, maRes] = await Promise.all([
        apiFetch('/api/config'),
        apiFetch('/api/account_info'),
        apiFetch('/api/contract_specs'),
        apiFetch(`/api/resonance_metrics?symbol=${activeSym}`),
        apiFetch('/api/status'),
        apiFetch(`/api/market_analysis?symbol=${activeSym}`),
      ]);

      const cfg = cfgRes.ok ? await cfgRes.json() : null;
      const acc = accRes.ok ? await accRes.json() : null;
      const specs = specRes.ok ? await specRes.json() : null;
      const reso = resRes.ok ? await resRes.json() : null;
      const st = statusRes.ok ? await statusRes.json() : null;
      const ma = maRes.ok ? await maRes.json() : null;

      if (cfg?.strategy?.symbols?.length) {
        const syms = cfg.strategy.symbols as string[];
        const cur = get().activeSymbol;
        set({
          availableSymbols: syms,
          ...(syms.includes(cur) ? {} : { activeSymbol: syms[0] }),
        });
      }

      if (acc && typeof acc === 'object') {
        const raw = (acc as { positions?: unknown[] }).positions;
        const list = Array.isArray(raw) ? raw.map((p) => mapAccountPosition(p as Record<string, unknown>)) : [];
        const bal = Number((acc as { balance?: number }).balance ?? 0);
        const dp = Number((acc as { display_daily_pnl?: number; daily_pnl?: number }).display_daily_pnl ?? (acc as { daily_pnl?: number }).daily_pnl ?? 0);
        const startBal = Number((acc as { session_start_balance?: number }).session_start_balance ?? bal);
        set({
          positions: list,
          equity: bal,
          dailyPnl: dp,
          dailyPnlPercent: startBal > 0 ? (dp / startBal) * 100 : 0,
        });
      }

      if (specs && typeof specs === 'object') {
        const merged: Record<string, ContractSpecRow> = { ...(get().contractSpecs || {}) };
        for (const sym of Object.keys(specs)) {
          const row = specs[sym] as Record<string, number>;
          merged[sym] = {
            symbol: sym,
            change_24h_pct: row.change_24h_pct ?? merged[sym]?.change_24h_pct,
            funding_rate: row.funding_rate ?? merged[sym]?.funding_rate ?? 0,
            mark_price: row.mark_price ?? merged[sym]?.mark_price ?? 0,
            index_price: row.index_price ?? merged[sym]?.index_price ?? 0,
            spread: row.spread ?? merged[sym]?.spread ?? 0,
            last_price: row.last_price ?? merged[sym]?.last_price,
            volume_24h: row.volume_24h ?? merged[sym]?.volume_24h ?? 0,
            next_funding_time: row.next_funding_time ?? merged[sym]?.next_funding_time,
          };
        }
        set({ contractSpecs: merged });
      }

      const active = get().activeSymbol;
      const sp = get().contractSpecs?.[active];
      if (sp?.last_price && sp.last_price > 0) {
        const candleT = Math.floor(Date.now() / 1000 / 60) * 60;
        set({ latestTick: { symbol: active, price: sp.last_price, time: candleT } });
      }

      if (reso && typeof reso === 'object') {
        const r = reso as Record<string, unknown>;
        const ks = r.kill_switch_progress as Record<string, unknown> | undefined;
        const ad = r.adaptation as Record<string, unknown> | undefined;
        const bn = r.beta_neutral_hf as Record<string, unknown> | undefined;
        set((state) => ({
          resonanceMetrics: {
            ...state.resonanceMetrics,
            obi: Number(r.obi ?? state.resonanceMetrics.obi),
            tech_indicator: Number(r.tech_indicator ?? state.resonanceMetrics.tech_indicator),
            tech_signal: (r.tech_signal as ResonanceMetrics['tech_signal']) ?? state.resonanceMetrics.tech_signal,
            ai_score: Number(r.ai_score ?? state.resonanceMetrics.ai_score),
            ai_regime: String(r.ai_regime ?? state.resonanceMetrics.ai_regime),
            ai_reason: String(r.ai_reason ?? state.resonanceMetrics.ai_reason),
            atr_pct: Number(r.atr_pct ?? state.resonanceMetrics.atr_pct),
            target_leverage: Number(r.target_leverage ?? state.resonanceMetrics.target_leverage),
            berserker_max_leverage: Number(
              r.berserker_max_leverage ?? state.resonanceMetrics.berserker_max_leverage
            ),
            current_risk_exposure: Number(r.current_risk_exposure ?? state.resonanceMetrics.current_risk_exposure),
            ws_latency: Number(r.ws_latency ?? state.resonanceMetrics.ws_latency),
            ws_reconnects: Number(r.ws_reconnects ?? state.resonanceMetrics.ws_reconnects),
            kill_switch_progress: ks
              ? {
                  active: Boolean(ks.active),
                  executed_chunks: Number(ks.executed_chunks ?? 0),
                  total_chunks: Number(ks.total_chunks ?? 5),
                  avg_slippage: Number(ks.avg_slippage ?? 0),
                }
              : state.resonanceMetrics.kill_switch_progress,
            adaptation: ad
              ? {
                  probe_mode: Boolean(ad.probe_mode),
                  adaptation_level: Number(ad.adaptation_level ?? 0),
                  adaptation_label: String(ad.adaptation_label ?? 'NORMAL'),
                  window_trades: Number(ad.window_trades ?? 0),
                  window_win_rate: Number(ad.window_win_rate ?? 1),
                  consecutive_losses: Number(ad.consecutive_losses ?? 0),
                  recovery_win_rate: Number(ad.recovery_win_rate ?? 1),
                  live_attack_ai_threshold: Number(ad.live_attack_ai_threshold ?? 0),
                  live_neutral_ai_threshold: Number(ad.live_neutral_ai_threshold ?? 0),
                  live_funding_signal_weight: Number(ad.live_funding_signal_weight ?? 0),
                  live_attack_align_bps: Number(ad.live_attack_align_bps ?? 0),
                  live_margin_cap_usdt: Number(ad.live_margin_cap_usdt ?? 0),
                  strongest_symbol: String(ad.strongest_symbol ?? ''),
                  strongest_symbol_win_rate: Number(ad.strongest_symbol_win_rate ?? 0),
                  strongest_symbol_trades: Number(ad.strongest_symbol_trades ?? 0),
                  weakest_symbol: String(ad.weakest_symbol ?? ''),
                  weakest_symbol_win_rate: Number(ad.weakest_symbol_win_rate ?? 1),
                  weakest_symbol_trades: Number(ad.weakest_symbol_trades ?? 0),
                  dominant_win_reason: String(ad.dominant_win_reason ?? ''),
                  dominant_win_reason_count: Number(ad.dominant_win_reason_count ?? 0),
                  dominant_loss_reason: String(ad.dominant_loss_reason ?? ''),
                  dominant_loss_reason_count: Number(ad.dominant_loss_reason_count ?? 0),
                  strongest_strategy: String(ad.strongest_strategy ?? ''),
                  strongest_strategy_win_rate: Number(ad.strongest_strategy_win_rate ?? 0),
                  strongest_strategy_trades: Number(ad.strongest_strategy_trades ?? 0),
                  strongest_scene:
                    ad.strongest_scene && typeof ad.strongest_scene === 'object'
                      ? (ad.strongest_scene as {
                          regime?: string;
                          symbol?: string;
                          side?: string;
                          strategy?: string;
                          quadrant?: string;
                          ai_bucket?: string;
                          wr?: number;
                          n?: number;
                          net?: number;
                        })
                      : {},
                  weakest_strategy: String(ad.weakest_strategy ?? ''),
                  weakest_strategy_win_rate: Number(ad.weakest_strategy_win_rate ?? 1),
                  weakest_strategy_trades: Number(ad.weakest_strategy_trades ?? 0),
                  symbol_boosts:
                    ad.symbol_boosts && typeof ad.symbol_boosts === 'object'
                      ? (ad.symbol_boosts as Record<string, { max_leverage?: number; berserker_obi_threshold?: number; win_rate?: number; trades?: number; net?: number }>)
                      : {},
                }
              : state.resonanceMetrics.adaptation,
            beta_neutral_hf: bn
              ? {
                  enabled: Boolean(bn.enabled),
                  anchor_symbol: String(bn.anchor_symbol ?? 'BTC/USDT'),
                  configured_leverage: Number(bn.configured_leverage ?? 0),
                  tracked_symbols: Array.isArray(bn.tracked_symbols) ? (bn.tracked_symbols as string[]) : [],
                  active_pairs: Array.isArray(bn.active_pairs)
                    ? (bn.active_pairs as ResonanceMetrics['beta_neutral_hf']['active_pairs'])
                    : [],
                  candidate_pairs: Array.isArray(bn.candidate_pairs)
                    ? (bn.candidate_pairs as ResonanceMetrics['beta_neutral_hf']['candidate_pairs'])
                    : [],
                  recent_closed: Array.isArray(bn.recent_closed)
                    ? (bn.recent_closed as ResonanceMetrics['beta_neutral_hf']['recent_closed'])
                    : [],
                  anchor_target_contracts: Number(bn.anchor_target_contracts ?? 0),
                  anchor_actual_contracts: Number(bn.anchor_actual_contracts ?? 0),
                }
              : state.resonanceMetrics.beta_neutral_hf,
          },
        }));
      }

      if (st && typeof st === 'object') {
        const so = st as { status?: string; ui_mode?: string };
        const patch: Partial<Pick<AppState, 'isRunning' | 'mode'>> = {
          isRunning: so.status === 'running',
        };
        if (
          so.ui_mode === 'ATTACK' ||
          so.ui_mode === 'NEUTRAL' ||
          so.ui_mode === 'BERSERKER' ||
          so.ui_mode === 'HALTED'
        ) {
          patch.mode = so.ui_mode;
        }
        set(patch);
      }

      if (ma && typeof ma === 'object' && (ma as { symbol?: string }).symbol && (ma as { symbol?: string }).symbol !== '-') {
        const m = ma as {
          symbol: string;
          regime: string;
          ai_score: number;
          timestamp?: number;
          reason?: string;
        };
        const regime = m.regime as AIInsight['regime'];
        const allowed: AIInsight['regime'][] = ['OSCILLATING', 'TRENDING_UP', 'TRENDING_DOWN', 'CHAOTIC'];
        set({
          aiInsight: {
            regime: allowed.includes(regime) ? regime : 'OSCILLATING',
            score: Number(m.ai_score),
            reason: typeof m.reason === 'string' && m.reason.length > 0 ? m.reason : `品种 ${m.symbol}（LLM 周期更新）`,
            timestamp: m.timestamp ? m.timestamp : Date.now(),
          },
        });
      }
    } catch (e) {
      console.error('hydrateDashboard failed', e);
    }
  },

  setMode: (mode: SystemMode) => set({ mode }),

  setActiveSymbol: (symbol: string) => {
    houndObiLogState = null;
    set({ activeSymbol: symbol, latestTick: null });
    const qs = encodeURIComponent(symbol);
    void Promise.all([
      fetch(`/api/resonance_metrics?symbol=${qs}`).then((r) => (r.ok ? r.json() : null)),
      fetch(`/api/market_analysis?symbol=${qs}`).then((r) => (r.ok ? r.json() : null)),
    ])
      .then(([reso, ma]) => {
        if (reso && typeof reso === 'object') {
          const r = reso as Record<string, unknown>;
          const ad = r.adaptation as Record<string, unknown> | undefined;
          set((state) => ({
            resonanceMetrics: {
              ...state.resonanceMetrics,
              obi: Number(r.obi ?? state.resonanceMetrics.obi),
              tech_indicator: Number(r.tech_indicator ?? state.resonanceMetrics.tech_indicator),
              tech_signal: (r.tech_signal as ResonanceMetrics['tech_signal']) ?? state.resonanceMetrics.tech_signal,
              ai_score: Number(r.ai_score ?? state.resonanceMetrics.ai_score),
              ai_regime: String(r.ai_regime ?? state.resonanceMetrics.ai_regime),
              ai_reason: String(r.ai_reason ?? state.resonanceMetrics.ai_reason),
              atr_pct: Number(r.atr_pct ?? state.resonanceMetrics.atr_pct),
              target_leverage: Number(r.target_leverage ?? state.resonanceMetrics.target_leverage),
              berserker_max_leverage: Number(
                r.berserker_max_leverage ?? state.resonanceMetrics.berserker_max_leverage
              ),
              adaptation: ad
                ? {
                    probe_mode: Boolean(ad.probe_mode),
                    adaptation_level: Number(ad.adaptation_level ?? 0),
                    adaptation_label: String(ad.adaptation_label ?? 'NORMAL'),
                    window_trades: Number(ad.window_trades ?? 0),
                    window_win_rate: Number(ad.window_win_rate ?? 1),
                    consecutive_losses: Number(ad.consecutive_losses ?? 0),
                    recovery_win_rate: Number(ad.recovery_win_rate ?? 1),
                    live_attack_ai_threshold: Number(ad.live_attack_ai_threshold ?? 0),
                    live_neutral_ai_threshold: Number(ad.live_neutral_ai_threshold ?? 0),
                    live_funding_signal_weight: Number(ad.live_funding_signal_weight ?? 0),
                    live_attack_align_bps: Number(ad.live_attack_align_bps ?? 0),
                    live_margin_cap_usdt: Number(ad.live_margin_cap_usdt ?? 0),
                    strongest_symbol: String(ad.strongest_symbol ?? ''),
                    strongest_symbol_win_rate: Number(ad.strongest_symbol_win_rate ?? 0),
                    strongest_symbol_trades: Number(ad.strongest_symbol_trades ?? 0),
                    weakest_symbol: String(ad.weakest_symbol ?? ''),
                    weakest_symbol_win_rate: Number(ad.weakest_symbol_win_rate ?? 1),
                    weakest_symbol_trades: Number(ad.weakest_symbol_trades ?? 0),
                    dominant_win_reason: String(ad.dominant_win_reason ?? ''),
                    dominant_win_reason_count: Number(ad.dominant_win_reason_count ?? 0),
                    dominant_loss_reason: String(ad.dominant_loss_reason ?? ''),
                    dominant_loss_reason_count: Number(ad.dominant_loss_reason_count ?? 0),
                    strongest_strategy: String(ad.strongest_strategy ?? ''),
                    strongest_strategy_win_rate: Number(ad.strongest_strategy_win_rate ?? 0),
                    strongest_strategy_trades: Number(ad.strongest_strategy_trades ?? 0),
                    strongest_scene:
                      ad.strongest_scene && typeof ad.strongest_scene === 'object'
                        ? (ad.strongest_scene as {
                            regime?: string;
                            symbol?: string;
                            side?: string;
                            strategy?: string;
                            quadrant?: string;
                            ai_bucket?: string;
                            wr?: number;
                            n?: number;
                            net?: number;
                          })
                        : {},
                    weakest_strategy: String(ad.weakest_strategy ?? ''),
                    weakest_strategy_win_rate: Number(ad.weakest_strategy_win_rate ?? 1),
                    weakest_strategy_trades: Number(ad.weakest_strategy_trades ?? 0),
                    symbol_boosts:
                      ad.symbol_boosts && typeof ad.symbol_boosts === 'object'
                        ? (ad.symbol_boosts as Record<string, { max_leverage?: number; berserker_obi_threshold?: number; win_rate?: number; trades?: number; net?: number }>)
                        : {},
                  }
                : state.resonanceMetrics.adaptation,
            },
          }));
        }
        if (ma && typeof ma === 'object' && (ma as { symbol?: string }).symbol && (ma as { symbol?: string }).symbol !== '-') {
          const m = ma as {
            symbol: string;
            regime: string;
            ai_score: number;
            timestamp?: number;
            reason?: string;
          };
          const reg = m.regime as AIInsight['regime'];
          const allowed: AIInsight['regime'][] = ['OSCILLATING', 'TRENDING_UP', 'TRENDING_DOWN', 'CHAOTIC'];
          set({
            aiInsight: {
              regime: allowed.includes(reg) ? reg : 'OSCILLATING',
              score: Number(m.ai_score),
              reason: typeof m.reason === 'string' && m.reason.length > 0 ? m.reason : `品种 ${m.symbol}（LLM 周期更新）`,
              timestamp: m.timestamp ? m.timestamp : Date.now(),
            },
          });
        }
      })
      .catch(() => {});
  },

  initWebSocket: () => {
    if (marketWs && (marketWs.readyState === WebSocket.OPEN || marketWs.readyState === WebSocket.CONNECTING)) {
      return;
    }

    (async () => {
      try {
        const response = await apiFetch('/api/config');
        const cfg = await response.json();
        const syms = cfg?.strategy?.symbols as string[] | undefined;
        if (syms?.length) {
          const cur = get().activeSymbol;
          set({
            availableSymbols: syms,
            ...(syms.includes(cur) ? {} : { activeSymbol: syms[0] }),
          });
        }
      } catch {
        /* backend may not be up yet */
      }
    })();

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const tok = (import.meta.env.VITE_SHARK_API_TOKEN as string | undefined)?.trim();
    const qs = tok ? `?token=${encodeURIComponent(tok)}` : '';
    const ws = new WebSocket(`${protocol}//${host}/ws/market_data${qs}`);
    marketWs = ws;

    ws.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data);
        if (payload.type !== 'MARKET_UPDATE') return;

        set({
          feedMeta: {
            dataFeed: payload.data_feed || '',
            sandboxExecution: !!payload.sandbox_execution,
          },
        });

        const bot = payload.bot as { ui_mode?: string } | undefined;
        if (
          bot?.ui_mode === 'ATTACK' ||
          bot?.ui_mode === 'NEUTRAL' ||
          bot?.ui_mode === 'BERSERKER' ||
          bot?.ui_mode === 'HALTED'
        ) {
          set({ mode: bot.ui_mode as SystemMode });
        }

        const data = payload.data as Record<string, Record<string, unknown>>;
        let nextSpecs = get().contractSpecs ?? {};
        for (const sym of Object.keys(data || {})) {
          nextSpecs = mergeContractFromPayload(nextSpecs, sym, data[sym]);
        }

        const prevObi = get().resonanceMetrics.obi;
        const prevTrail = { ...get().symbolPriceTrail };
        for (const sym of Object.keys(nextSpecs)) {
          const lp = nextSpecs[sym]?.last_price;
          if (typeof lp === 'number' && lp > 0 && Number.isFinite(lp)) {
            const arr = [...(prevTrail[sym] ?? [])];
            arr.push(lp);
            while (arr.length > TRAIL_MAX) arr.shift();
            prevTrail[sym] = arr;
          }
        }

        const { activeSymbol } = get();
        const symData = data?.[activeSymbol];
        let latestTick: AppState['latestTick'] = get().latestTick;
        const kt = symData?.kline_tick as { time?: number; price?: number } | undefined;
        if (kt && typeof kt.time === 'number' && typeof kt.price === 'number' && kt.price > 0) {
          latestTick = { symbol: activeSymbol, price: kt.price, time: kt.time };
        }

        if (symData) {
          const nextObi = (symData.obi as number) ?? get().resonanceMetrics.obi;
          if (typeof symData.obi === 'number') {
            const now = Date.now();
            const sameSym = houndObiLogState?.symbol === activeSymbol;
            const intervalOk =
              !sameSym ||
              now - (houndObiLogState?.t ?? 0) >= HOUND_OBI_MIN_INTERVAL_MS;
            const baseObi = sameSym ? houndObiLogState!.obi : prevObi;
            if (
              intervalOk &&
              Math.abs(nextObi - baseObi) >= HOUND_OBI_MIN_DELTA
            ) {
              houndObiLogState = {
                symbol: activeSymbol,
                obi: nextObi,
                t: now,
              };
              get().pushHoundTrace(
                `OBI ${baseObi.toFixed(2)}→${nextObi.toFixed(2)} · ${activeSymbol}`
              );
            }
          }
          set((state) => ({
            contractSpecs: nextSpecs,
            latestTick,
            symbolPriceTrail: prevTrail,
            resonanceMetrics: {
              ...state.resonanceMetrics,
              obi: nextObi,
              ai_score: (symData.ai_score as number) ?? state.resonanceMetrics.ai_score,
              ai_regime: String(symData.ai_regime ?? state.resonanceMetrics.ai_regime),
              ai_reason: String(symData.ai_reason ?? state.resonanceMetrics.ai_reason),
              atr_pct:
                typeof symData.atr_pct_snapshot === 'number'
                  ? symData.atr_pct_snapshot
                  : state.resonanceMetrics.atr_pct,
              target_leverage:
                typeof symData.target_leverage === 'number'
                  ? symData.target_leverage
                  : state.resonanceMetrics.target_leverage,
              berserker_max_leverage:
                typeof symData.berserker_max_leverage === 'number'
                  ? symData.berserker_max_leverage
                  : state.resonanceMetrics.berserker_max_leverage,
            },
          }));
        } else {
          set({
            contractSpecs: nextSpecs,
            latestTick,
            symbolPriceTrail: prevTrail,
          });
        }

        if (payload.account) {
          const acc = payload.account as {
            equity: number;
            daily_pnl: number;
            display_daily_pnl?: number;
            display_daily_pnl_percent?: number;
            session_start_balance?: number;
            positions: Record<string, unknown>[];
          };
          const eq = acc.equity ?? 0;
          const dp = acc.display_daily_pnl ?? acc.daily_pnl ?? 0;
          const startBal = acc.session_start_balance ?? eq;
          set({
            equity: eq,
            dailyPnl: dp,
            dailyPnlPercent:
              typeof acc.display_daily_pnl_percent === 'number'
                ? acc.display_daily_pnl_percent
                : startBal > 0
                  ? (dp / startBal) * 100
                  : 0,
            positions: Array.isArray(acc.positions) ? acc.positions.map((p) => mapAccountPosition(p)) : [],
          });
        }

        const ghu = payload.gate_hot_universe as
          | { enabled?: boolean; symbols?: string[] }
          | undefined;
        if (ghu?.enabled && Array.isArray(ghu.symbols) && ghu.symbols.length > 0) {
          set((state) => ({
            availableSymbols: ghu.symbols as string[],
            activeSymbol: (ghu.symbols as string[]).includes(state.activeSymbol)
              ? state.activeSymbol
              : (ghu.symbols as string[])[0],
          }));
        }
      } catch (e) {
        console.error('WS Parse Error', e);
      }
    };

    ws.onclose = () => {
      marketWs = null;
      console.warn('WS Disconnected, attempting reconnect in 3s');
      setTimeout(() => get().initWebSocket(), 3000);
    };
  },

  killSwitch: async () => {
    try {
      set((state) => ({
        isRunning: false,
        mode: 'HALTED',
        resonanceMetrics: {
          ...state.resonanceMetrics,
          kill_switch_progress: {
            ...state.resonanceMetrics.kill_switch_progress,
            active: true,
            executed_chunks: 0,
          },
        },
      }));

      const response = await apiFetch('/api/control', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'KILL_SWITCH' }),
      });

      const data = (await response.json().catch(() => ({}))) as { status?: string; message?: string };

      if (response.ok && data.status === 'success') {
        set((state) => ({
          positions: [],
          resonanceMetrics: {
            ...state.resonanceMetrics,
            kill_switch_progress: {
              ...state.resonanceMetrics.kill_switch_progress,
              active: false,
              executed_chunks: state.resonanceMetrics.kill_switch_progress.total_chunks,
            },
          },
        }));
        console.warn('KILL SWITCH:', data.message || 'ok');
        return;
      }

      set((state) => ({
        resonanceMetrics: {
          ...state.resonanceMetrics,
          kill_switch_progress: {
            ...state.resonanceMetrics.kill_switch_progress,
            active: false,
          },
        },
      }));
      console.error('KILL_SWITCH failed', response.status, data);
    } catch (error) {
      console.error('Failed to execute kill switch', error);
    }
  },

  toggleEngine: () => set((state) => ({ isRunning: !state.isRunning })),
}));
