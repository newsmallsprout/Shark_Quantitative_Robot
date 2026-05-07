import { useEffect, useRef } from 'react'
import { createChart, ColorType } from 'lightweight-charts'
import { useStore } from '../store/useStore'

const CHART_SYMBOLS = [
  { symbol: 'BTC/USDT', color: '#f7931a' },
  { symbol: 'ETH/USDT', color: '#627eea' },
  { symbol: 'SOL/USDT', color: '#00d4aa' },
]

const CHART_HEIGHT = 170
const MAX_POINTS = 120

interface ChartEntry {
  chart: ReturnType<typeof createChart>
  series: ReturnType<ReturnType<typeof createChart>['addAreaSeries']>
  data: Array<{ time: any; value: number }>
}

function createDarkChart(container: HTMLElement, color: string): ChartEntry {
  const chart = createChart(container, {
    width: container.clientWidth,
    height: CHART_HEIGHT,
    layout: {
      background: { type: ColorType.Solid, color: 'transparent' },
      textColor: '#3d3d52',
      fontSize: 10,
    },
    grid: {
      vertLines: { color: '#18182a', style: 1 },
      horzLines: { color: '#18182a', style: 1 },
    },
    timeScale: {
      timeVisible: false,
      borderColor: '#18182a',
    },
    rightPriceScale: {
      borderColor: '#18182a',
      entireTextOnly: true,
      scaleMargins: { top: 0.15, bottom: 0.15 },
    },
    crosshair: { mode: 0 },
    handleScroll: false,
    handleScale: false,
  })

  const series = chart.addAreaSeries({
    lineColor: color,
    topColor: `${color}18`,
    bottomColor: `${color}04`,
    lineWidth: 2,
    priceLineVisible: false,
    crosshairMarkerVisible: false,
  })

  return { chart, series, data: [] }
}

export default function ChartPanel() {
  const containerRef = useRef<HTMLDivElement>(null)
  const entriesRef = useRef<Map<string, ChartEntry>>(new Map())
  const livePrices = useStore((s) => s.status.live_prices)

  // Init charts
  useEffect(() => {
    if (!containerRef.current) return
    const container = containerRef.current
    container.innerHTML = ''
    const entries = new Map<string, ChartEntry>()

    CHART_SYMBOLS.forEach(({ symbol, color }) => {
      const wrapper = document.createElement('div')
      wrapper.style.cssText = `height:${CHART_HEIGHT}px;position:relative`
      wrapper.innerHTML = `<div style="position:absolute;top:6px;left:10px;font-size:10px;font-weight:600;color:#6b6b80;letter-spacing:0.04em;z-index:10;text-transform:uppercase">${symbol} <span id="price-${symbol.replace('/', '_')}" style="color:#e4e4ed;font-family:var(--font-mono)">--</span></div>`
      container.appendChild(wrapper)

      const entry = createDarkChart(wrapper, color)
      entries.set(symbol, entry)
    })

    entriesRef.current = entries

    const handleResize = () => {
      entries.forEach(({ chart }) => {
        chart.applyOptions({ width: container.clientWidth })
      })
    }
    window.addEventListener('resize', handleResize)

    return () => {
      entries.forEach(({ chart }) => chart.remove())
      window.removeEventListener('resize', handleResize)
    }
  }, [])

  // Update charts with live prices
  useEffect(() => {
    const now = Math.floor(Date.now() / 1000)
    let updated = false

    CHART_SYMBOLS.forEach(({ symbol }) => {
      const entry = entriesRef.current.get(symbol)
      const lp = livePrices[symbol]
      if (!entry || !lp || lp.price <= 0) return

      // Update price label
      const el = document.getElementById(`price-${symbol.replace('/', '_')}`)
      if (el) {
        el.textContent = `$${lp.price.toFixed(symbol === 'BTC/USDT' ? 1 : symbol === 'ETH/USDT' ? 2 : 4)}`
        el.style.color = lp.change >= 0 ? 'var(--profit)' : 'var(--loss)'
      }

      // Append data point
      entry.data.push({ time: now as any, value: lp.price })
      if (entry.data.length > MAX_POINTS) entry.data.shift()
      entry.series.setData(entry.data)
      updated = true
    })

    if (updated) {
      entriesRef.current.forEach(({ chart }) => {
        chart.timeScale().fitContent()
      })
    }
  }, [livePrices])

  return <div ref={containerRef} style={{ minHeight: CHART_SYMBOLS.length * CHART_HEIGHT }} />
}
