import { create } from 'zustand'

export interface Position {
  symbol: string; side: string; size: number; entry_price: number;
  leverage: number; margin: number; unrealized_pnl: number; pnl_pct: number;
  current_price?: number;
}

export interface LivePrice {
  price: number; change: number;
}

interface Status {
  equity: number; balance: number; positions: number;
  realized_pnl: number; win_rate: number;
  safety_blocked: boolean; mode: string;
  position_list: Position[];
  live_prices: Record<string, LivePrice>;
  total_fees: number;
  total_slippage: number;
  trade_history: any[];
  margin_locked: number;
}

interface Store {
  status: Status;
  connected: boolean;
  setStatus: (s: Partial<Status>) => void;
  setConnected: (c: boolean) => void;
}

export const useStore = create<Store>((set) => ({
  status: {
    equity: 100, balance: 100, positions: 0,
    realized_pnl: 0, win_rate: 0,
    safety_blocked: false, mode: 'Paper',
    position_list: [],
    live_prices: {},
    total_fees: 0,
    total_slippage: 0,
    trade_history: [],
    margin_locked: 0,
  },
  connected: false,
  setStatus: (s) => set((st) => ({ status: { ...st.status, ...s } })),
  setConnected: (c) => set({ connected: c }),
}))
