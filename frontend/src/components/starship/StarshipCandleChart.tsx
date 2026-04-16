import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CandlestickSeries,
  ColorType,
  HistogramSeries,
  createChart,
} from 'lightweight-charts';
import type {
  CandlestickData,
  HistogramData,
  IChartApi,
  ISeriesApi,
  UTCTimestamp,
} from 'lightweight-charts';
import { apiFetch } from '../../apiClient';
import { useStore } from '../../store/useStore';
import { CandlestickChart, RefreshCw } from 'lucide-react';

type IntervalOpt = { label: string; value: string };

const INTERVALS: IntervalOpt[] = [
  { label: '1m', value: '1m' },
  { label: '5m', value: '5m' },
  { label: '15m', value: '15m' },
  { label: '1h', value: '1h' },
  { label: '4h', value: '4h' },
];

interface ApiCandle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
  volume?: number;
}

function normalizeCandles(raw: unknown[]): { candles: CandlestickData[]; volumes: HistogramData[] } {
  const rows: ApiCandle[] = [];
  for (const x of raw) {
    if (!x || typeof x !== 'object') continue;
    const o = x as Record<string, unknown>;
    const t = Number(o.time ?? o.t ?? 0);
    if (!Number.isFinite(t) || t <= 0) continue;
    const ts = (t > 2e10 ? Math.floor(t / 1000) : t) as UTCTimestamp;
    const open = Number(o.open ?? o.o);
    const high = Number(o.high ?? o.h);
    const low = Number(o.low ?? o.l);
    const close = Number(o.close ?? o.c);
    const volume = Number(o.volume ?? o.v ?? 0);
    if (![open, high, low, close].every(Number.isFinite)) continue;
    rows.push({ time: ts, open, high, low, close, volume: Number.isFinite(volume) ? volume : 0 });
  }
  rows.sort((a, b) => a.time - b.time);
  const candles: CandlestickData[] = [];
  const volumes: HistogramData[] = [];
  let lastT = 0;
  for (const r of rows) {
    const t = r.time as UTCTimestamp;
    if (t === lastT) continue;
    lastT = t;
    candles.push({ time: t, open: r.open, high: r.high, low: r.low, close: r.close });
    const up = r.close >= r.open;
    volumes.push({
      time: t,
      value: r.volume ?? 0,
      color: up ? 'rgba(231, 177, 90, 0.45)' : 'rgba(239, 83, 80, 0.45)',
    });
  }
  return { candles, volumes };
}

export const StarshipCandleChart: React.FC = () => {
  const activeSymbol = useStore((s) => s.activeSymbol);
  const latestTick = useStore((s) => s.latestTick);
  const containerRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const candleRef = useRef<ISeriesApi<'Candlestick'> | null>(null);
  const volumeRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  const lastCandleRef = useRef<CandlestickData | null>(null);
  const [interval, setInterval] = useState('15m');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [lastUpd, setLastUpd] = useState<number | null>(null);

  const bucketSeconds = useCallback((v: string): number => {
    switch (v) {
      case '1m':
        return 60;
      case '5m':
        return 300;
      case '15m':
        return 900;
      case '1h':
        return 3600;
      case '4h':
        return 14400;
      default:
        return 900;
    }
  }, []);

  const load = useCallback(async () => {
    if (!activeSymbol) return;
    setLoading(true);
    setError(null);
    try {
      const q = new URLSearchParams({
        symbol: activeSymbol,
        interval,
        limit: '300',
      });
      const res = await apiFetch(`/api/candles?${q}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = (await res.json()) as unknown[];
      if (!Array.isArray(data) || data.length === 0) {
        setError('暂无 K 线（后端未就绪或合约无数据）');
        return;
      }
      const { candles, volumes } = normalizeCandles(data);
      const chart = chartRef.current;
      const cs = candleRef.current;
      const vs = volumeRef.current;
      if (!chart || !cs || !vs) return;
      cs.setData(candles);
      vs.setData(volumes);
      lastCandleRef.current = candles.length ? candles[candles.length - 1] : null;
      chart.timeScale().fitContent();
      setLastUpd(Date.now());
    } catch (e) {
      setError(e instanceof Error ? e.message : '加载失败');
    } finally {
      setLoading(false);
    }
  }, [activeSymbol, interval]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      layout: {
        background: { type: ColorType.Solid, color: '#161412' },
        textColor: '#a8a29e',
        fontSize: 11,
      },
      grid: {
        vertLines: { color: 'rgba(255,255,255,0.04)' },
        horzLines: { color: 'rgba(255,255,255,0.04)' },
      },
      crosshair: {
        vertLine: { color: 'rgba(231, 177, 90, 0.35)', labelBackgroundColor: '#3d3428' },
        horzLine: { color: 'rgba(231, 177, 90, 0.35)', labelBackgroundColor: '#3d3428' },
      },
      rightPriceScale: { borderColor: 'rgba(255,255,255,0.08)' },
      timeScale: { borderColor: 'rgba(255,255,255,0.08)', timeVisible: true, secondsVisible: false },
      width: el.clientWidth,
      height: Math.max(260, Math.min(380, Math.floor(window.innerHeight * 0.28))),
    });

    const cs = chart.addSeries(CandlestickSeries, {
      upColor: '#c9a227',
      downColor: '#c45c4a',
      borderUpColor: '#e7b15a',
      borderDownColor: '#e0786a',
      wickUpColor: '#d4a84b',
      wickDownColor: '#c45c4a',
    });
    const vs = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: '',
    });
    vs.priceScale().applyOptions({
      scaleMargins: { top: 0.82, bottom: 0 },
    });

    chartRef.current = chart;
    candleRef.current = cs;
    volumeRef.current = vs;

    const ro = new ResizeObserver(() => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({
        width: containerRef.current.clientWidth,
        height: Math.max(260, Math.min(380, Math.floor(window.innerHeight * 0.28))),
      });
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      candleRef.current = null;
      volumeRef.current = null;
    };
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const t = window.setInterval(() => void load(), 45_000);
    return () => window.clearInterval(t);
  }, [load]);

  useEffect(() => {
    if (!latestTick || latestTick.symbol !== activeSymbol) return;
    const cs = candleRef.current;
    if (!cs) return;

    const bucket = bucketSeconds(interval);
    const tickTs = Math.floor(Number(latestTick.time) || Date.now() / 1000);
    const candleTime = (Math.floor(tickTs / bucket) * bucket) as UTCTimestamp;
    const price = Number(latestTick.price || 0);
    if (!Number.isFinite(price) || price <= 0) return;

    const prev = lastCandleRef.current;
    let next: CandlestickData;
    if (!prev) {
      next = {
        time: candleTime,
        open: price,
        high: price,
        low: price,
        close: price,
      };
    } else if ((prev.time as number) === candleTime) {
      next = {
        time: candleTime,
        open: prev.open,
        high: Math.max(prev.high, price),
        low: Math.min(prev.low, price),
        close: price,
      };
    } else if ((prev.time as number) < candleTime) {
      next = {
        time: candleTime,
        open: prev.close,
        high: Math.max(prev.close, price),
        low: Math.min(prev.close, price),
        close: price,
      };
    } else {
      return;
    }

    cs.update(next);
    lastCandleRef.current = next;
    setLastUpd(Date.now());
  }, [activeSymbol, bucketSeconds, interval, latestTick]);

  return (
    <div className="ti-glass rounded-lg border border-white/[0.07] flex flex-col min-h-[280px] h-full overflow-hidden">
      <div className="shrink-0 flex flex-wrap items-center justify-between gap-2 px-2.5 py-1.5 border-b border-white/[0.06]">
        <div className="flex items-center gap-2 min-w-0">
          <CandlestickChart className="w-4 h-4 text-[#e7b15a]/90 shrink-0" />
          <span className="text-[11px] font-mono text-[#ebe8e3] truncate">
            K 线 · <span className="text-[#e7b15a]">{activeSymbol || '—'}</span>
          </span>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap">
          {INTERVALS.map((it) => (
            <button
              key={it.value}
              type="button"
              onClick={() => setInterval(it.value)}
              className={`px-2 py-0.5 rounded text-[10px] font-mono uppercase tracking-wide transition-colors ${
                interval === it.value
                  ? 'bg-[#e7b15a]/22 text-[#e7b15a] border border-[#e7b15a]/35'
                  : 'text-[#8a8580] hover:text-[#d4cfc7] border border-transparent'
              }`}
            >
              {it.label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => void load()}
            disabled={loading}
            className="p-1 rounded border border-white/[0.08] text-[#a8a29e] hover:text-[#e7b15a] disabled:opacity-40"
            title="刷新"
          >
            <RefreshCw className={`w-3.5 h-3.5 ${loading ? 'animate-spin' : ''}`} />
          </button>
        </div>
      </div>
      {error && (
        <div className="shrink-0 px-2.5 py-1 text-[10px] text-amber-200/90 bg-amber-500/10 border-b border-amber-500/20">
          {error}
        </div>
      )}
      <div ref={containerRef} className="flex-1 min-h-[240px] w-full min-w-0" />
      {lastUpd && !error && (
        <div className="shrink-0 px-2 py-0.5 text-[9px] text-[#6b6560] font-mono text-right border-t border-white/[0.04]">
          更新 {new Date(lastUpd).toLocaleTimeString('zh-CN', { hour12: false })}
        </div>
      )}
    </div>
  );
};
