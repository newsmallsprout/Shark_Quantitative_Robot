import { useCallback, useEffect, useRef, useState } from 'react'
import type { CharacterEvent } from '../store/useStore'
import { usePetState, type PetPlaybackState } from '../hooks/usePetState'
import styles from './PetVideoPlayer.module.css'

const rawBase = import.meta.env.BASE_URL || '/'
const URL_BASE = rawBase.endsWith('/') ? rawBase : `${rawBase}/`

/**
 * web/video 下 Kling 真人素材（文件名须与此处一致）。
 * 分段：0–1 待机/无聊循环；2–5 盈利/开仓庆祝；6–11 亏损轮换；12–13 止损。
 */
const KLING_CLIPS: string[] = [
  'kling_20260517_作品_A_young_Ea_2707_0.mp4',
  'kling_20260517_作品_A_young_Ea_2710_0.mp4',
  'kling_20260517_作品_A_young_Ea_2717_0.mp4',
  'kling_20260517_作品_A_young_Ea_2720_0.mp4',
  'kling_20260517_作品_A_young_Ea_2723_0.mp4',
  'kling_20260517_作品_A_young_Ea_2725_0.mp4',
  'kling_20260517_作品_A_young_Ea_2726_0.mp4',
  'kling_20260517_作品_A_young_Ea_2727_0.mp4',
  'kling_20260517_作品_A_young_Ea_2728_0.mp4',
  'kling_20260517_作品_A_young_Ea_2731_0.mp4',
  'kling_20260517_作品_A_young_Ea_2731_0_1.mp4',
  'kling_20260517_作品_A_young_Ea_2733_0.mp4',
  'kling_20260517_作品_A_young_Ea_2734_0.mp4',
  'kling_20260517_作品_A_young_Ea_2744_0.mp4',
]

const IDLE_INDEX = 0
const BORING_INDEX = 1
const PROFIT_START = 2
const PROFIT_COUNT = 4
const LOSS_START = 6
const LOSS_COUNT = 6
const STOPLOSS_START = 12
const STOPLOSS_COUNT = 2

const SCENE_LABELS: Record<PetPlaybackState, string> = {
  idle: '待机扫描',
  boring: '行情观测',
  profit: '正向反馈',
  loss: '风险复盘',
  stoploss: '风控触发',
}

function clipUrl(index: number): string {
  const name = KLING_CLIPS[Math.max(0, Math.min(index, KLING_CLIPS.length - 1))]
  return `${URL_BASE}video/${encodeURIComponent(name)}`
}

function resolveVideoSrc(playbackState: PetPlaybackState, clipNonce: number): string {
  switch (playbackState) {
    case 'idle':
      return clipUrl(IDLE_INDEX)
    case 'boring':
      return clipUrl(BORING_INDEX)
    case 'profit':
      return clipUrl(PROFIT_START + (clipNonce % PROFIT_COUNT))
    case 'loss':
      return clipUrl(LOSS_START + (clipNonce % LOSS_COUNT))
    case 'stoploss':
      return clipUrl(STOPLOSS_START + (clipNonce % STOPLOSS_COUNT))
    default:
      return clipUrl(IDLE_INDEX)
  }
}

export type PetVideoPlayerProps = {
  characterEvent?: CharacterEvent | null
  volatility?: number
  chromaScreen?: boolean
}

function armVideo(el: HTMLVideoElement) {
  el.muted = true
  el.setAttribute('muted', '')
  el.defaultMuted = true
  el.playsInline = true
  el.setAttribute('playsInline', '')
  el.setAttribute('webkit-playsinline', '')
}

function tryPlay(el: HTMLVideoElement) {
  armVideo(el)
  void el.play().catch(() => {})
}

export default function PetVideoPlayer({
  characterEvent,
  volatility,
  chromaScreen = false,
}: PetVideoPlayerProps) {
  const videoA = useRef<HTMLVideoElement>(null)
  const videoB = useRef<HTMLVideoElement>(null)
  /** 当前视觉上优先的一层（opacity 1） */
  const [frontIsA, setFrontIsA] = useState(true)
  const [mediaFailed, setMediaFailed] = useState(false)
  const [sceneLabel, setSceneLabel] = useState(SCENE_LABELS.idle)

  const { playbackState, shouldLoop, clipNonce, onClipEnded, bubbleText, bubbleVisible } = usePetState({
    characterEvent,
    volatility,
  })

  const layerSrc = useRef<{ a: string; b: string }>({
    a: clipUrl(IDLE_INDEX),
    b: clipUrl(IDLE_INDEX),
  })
  const activeLayerRef = useRef<'a' | 'b'>('a')
  const transitionRef = useRef(0)

  const targetSrc = resolveVideoSrc(playbackState, clipNonce)
  const videoClass = `${styles.video} ${chromaScreen ? styles.videoChroma : ''}`

  const handleEnded = useCallback(
    (e: React.SyntheticEvent<HTMLVideoElement>) => {
      const el = e.currentTarget
      const active = activeLayerRef.current
      const activeEl = active === 'a' ? videoA.current : videoB.current
      if (el !== activeEl) return
      onClipEnded()
    },
    [onClipEnded],
  )

  useEffect(() => {
    setSceneLabel(SCENE_LABELS[playbackState] ?? SCENE_LABELS.idle)
  }, [playbackState])

  useEffect(() => {
    const boot = clipUrl(IDLE_INDEX)
    const a = videoA.current
    const b = videoB.current
    if (!a || !b) return
    armVideo(a)
    armVideo(b)
    a.src = boot
    b.src = boot
    layerSrc.current = { a: boot, b: boot }
    a.loop = true
    b.loop = true
    void a.load()
    void b.load()
    const kick = () => {
      setMediaFailed(false)
      tryPlay(a)
    }
    a.addEventListener('canplay', kick, { once: true })
    return () => a.removeEventListener('canplay', kick)
  }, [])

  useEffect(() => {
    const a = videoA.current
    const b = videoB.current
    if (!a || !b) return

    const active = activeLayerRef.current
    const activeEl = active === 'a' ? a : b
    const backEl = active === 'a' ? b : a
    const currentActiveSrc = layerSrc.current[active]

    if (targetSrc === currentActiveSrc) {
      activeEl.loop = shouldLoop
      return
    }

    transitionRef.current += 1
    const tid = transitionRef.current

    backEl.loop = shouldLoop
    backEl.src = targetSrc
    backEl.load()

    const onReady = () => {
      if (tid !== transitionRef.current) return
      tryPlay(backEl)
      activeEl.pause()

      const nextFront = active === 'a' ? false : true
      activeLayerRef.current = nextFront ? 'a' : 'b'
      layerSrc.current[nextFront ? 'a' : 'b'] = targetSrc
      setFrontIsA(nextFront)
    }

    const onErr = () => setMediaFailed(true)

    backEl.addEventListener('canplay', onReady, { once: true })
    backEl.addEventListener('error', onErr, { once: true })

    return () => {
      backEl.removeEventListener('canplay', onReady)
      backEl.removeEventListener('error', onErr)
    }
  }, [targetSrc, shouldLoop, playbackState, clipNonce])

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState !== 'visible') return
      const el = frontIsA ? videoA.current : videoB.current
      if (el && (shouldLoop || !el.ended)) tryPlay(el)
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [frontIsA, shouldLoop])

  /** 预加载「下一个可能片段」，减少切场景时的首帧等待（不改变当前播放） */
  useEffect(() => {
    const idle = clipUrl(IDLE_INDEX)
    const preloadUrls = [idle, clipUrl(PROFIT_START), clipUrl(LOSS_START)]
    const links: HTMLLinkElement[] = []
    for (const href of preloadUrls) {
      const link = document.createElement('link')
      link.rel = 'preload'
      link.as = 'video'
      link.href = href
      document.head.appendChild(link)
      links.push(link)
    }
    return () => {
      for (const l of links) l.remove()
    }
  }, [])

  return (
    <div className={styles.wrap}>
      {mediaFailed ? (
        <div className={`${styles.fallback} pet-video-fallback`}>
          <div>
            <div style={{ marginBottom: 8, color: 'var(--text-primary)' }}>未加载到视频</div>
            请将 Kling 导出的 <code>kling_*.mp4</code> 放入 <code>web/video/</code>，文件名与{' '}
            <code>PetVideoPlayer.tsx</code> 中 <code>KLING_CLIPS</code> 一致。
            <br />
            本地可 <code>npm run dev</code>；Docker 内需重新构建前端并重启 <code>shark2</code>。
          </div>
        </div>
      ) : null}

      <div className={styles.sceneHud} aria-hidden>
        <span className={styles.sceneHudPulse} />
        <span>{sceneLabel}</span>
        <span style={{ opacity: 0.5 }}>·</span>
        <span style={{ opacity: 0.65 }}>scene sync</span>
      </div>

      {bubbleVisible && bubbleText ? (
        <div className={`${styles.speechBubble} speech-bubble`} role="status">
          {bubbleText}
        </div>
      ) : null}

      <div className={styles.layers}>
        <div className={`${styles.videoLayer} ${frontIsA ? styles.videoLayerFront : ''}`}>
          <video
            ref={videoA}
            className={videoClass}
            muted
            playsInline
            autoPlay
            preload="auto"
            onEnded={handleEnded}
          />
        </div>
        <div className={`${styles.videoLayer} ${!frontIsA ? styles.videoLayerFront : ''}`}>
          <video
            ref={videoB}
            className={videoClass}
            muted
            playsInline
            autoPlay
            preload="auto"
            onEnded={handleEnded}
          />
        </div>
      </div>
    </div>
  )
}
