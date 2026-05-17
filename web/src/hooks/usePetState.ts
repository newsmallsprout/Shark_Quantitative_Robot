import { useCallback, useEffect, useRef, useState } from 'react'
import type { CharacterEvent } from '../store/useStore'

export type PetPlaybackState = 'idle' | 'boring' | 'profit' | 'loss' | 'stoploss'

export type UsePetStateOptions = {
  characterEvent?: CharacterEvent | null
  volatility?: number
  /** 绝对涨跌幅低于此值视为「够无聊」才进入 boring（%） */
  boringVolatilityThreshold?: number
  /** idle 多久后尝试切入 boring（ms） */
  idleToBoringMs?: number
}

function eventSignature(ev: CharacterEvent): string {
  const seq = ev._seq
  if (seq !== undefined && seq !== null) return `seq:${seq}`
  return [
    ev.Event_Type ?? '',
    ev.Speech_Text ?? '',
    ev.Action_Code ?? '',
    ev.pnl ?? '',
    ev.symbol ?? '',
  ].join('|')
}

function mapCharacterEventToState(ev: CharacterEvent): PetPlaybackState {
  const t = ev.Event_Type ?? ''
  const code = ev.Action_Code ?? ''
  if (t.includes('止损')) return 'stoploss'
  if (t.includes('止盈') || /fist|coin/i.test(code)) return 'profit'
  if (/shield|glasses/i.test(code)) return 'loss'
  if (t.includes('横盘') || t.includes('闲聊')) return 'boring'
  if (t.includes('开仓') || /sword|hammer|net/i.test(code)) return 'profit'
  if (t.includes('平仓') && (t.includes('亏') || ev.pnl != null && Number(ev.pnl) < 0)) return 'loss'
  return 'profit'
}

export function usePetState(opts: UsePetStateOptions) {
  const [playbackState, setPlaybackState] = useState<PetPlaybackState>('idle')
  const [clipNonce, setClipNonce] = useState(0)
  const [bubbleText, setBubbleText] = useState('')
  const [bubbleVisible, setBubbleVisible] = useState(false)

  const lastSigRef = useRef<string | null>(null)
  const bubbleTimerRef = useRef<ReturnType<typeof setTimeout>>()
  const boringTimerRef = useRef<ReturnType<typeof setTimeout>>()

  const volTh = opts.boringVolatilityThreshold ?? 1.2
  const idleMs = opts.idleToBoringMs ?? 45000

  useEffect(() => {
    const ev = opts.characterEvent
    if (!ev) return

    const sig = eventSignature(ev)
    if (sig === lastSigRef.current) return
    lastSigRef.current = sig

    const next = mapCharacterEventToState(ev)
    setClipNonce((n) => n + 1)
    setPlaybackState(next)

    const txt = (ev.Speech_Text ?? '').trim()
    if (txt) {
      setBubbleText(txt)
      setBubbleVisible(true)
      if (bubbleTimerRef.current) clearTimeout(bubbleTimerRef.current)
      bubbleTimerRef.current = setTimeout(() => setBubbleVisible(false), 3000)
    } else {
      setBubbleVisible(false)
    }
  }, [opts.characterEvent])

  useEffect(() => {
    return () => {
      if (bubbleTimerRef.current) clearTimeout(bubbleTimerRef.current)
      if (boringTimerRef.current) clearTimeout(boringTimerRef.current)
    }
  }, [])

  const onClipEnded = useCallback(() => {
    setPlaybackState((prev) => {
      if (prev === 'profit' || prev === 'loss' || prev === 'stoploss') return 'idle'
      return prev
    })
  }, [])

  useEffect(() => {
    if (boringTimerRef.current) clearTimeout(boringTimerRef.current)
    if (playbackState !== 'idle') return

    boringTimerRef.current = setTimeout(() => {
      const v = opts.volatility
      const calm = v === undefined || Math.abs(v) < volTh
      setPlaybackState((p) => (p === 'idle' && calm ? 'boring' : p))
    }, idleMs)

    return () => {
      if (boringTimerRef.current) clearTimeout(boringTimerRef.current)
    }
  }, [playbackState, opts.volatility, volTh, idleMs])

  const shouldLoop = playbackState === 'idle' || playbackState === 'boring'

  return {
    playbackState,
    shouldLoop,
    clipNonce,
    onClipEnded,
    bubbleText,
    bubbleVisible,
  }
}
