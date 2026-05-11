import { useEffect, useRef, useState } from 'react'
import type { CharacterEvent } from '../store/useStore'
import { usePetState, type PetPlaybackState } from '../hooks/usePetState'
import styles from './PetVideoPlayer.module.css'

const rawBase = import.meta.env.BASE_URL || '/'
const URL_BASE = rawBase.endsWith('/') ? rawBase : `${rawBase}/`

const VIDEO_MAP: Record<PetPlaybackState, string> = {
  idle: `${URL_BASE}video/idle.mp4`,
  profit: `${URL_BASE}video/profit.mp4`,
  loss: `${URL_BASE}video/loss.mp4`,
  stoploss: `${URL_BASE}video/stoploss.mp4`,
  boring: `${URL_BASE}video/boring.mp4`,
}

/** 亏损态轮换多条素材（可与 loss2.mp4 搭配） */
const LOSS_CLIP_URLS = [`${URL_BASE}video/loss.mp4`, `${URL_BASE}video/loss2.mp4`]

function resolveVideoSrc(playbackState: PetPlaybackState, clipNonce: number): string {
  if (playbackState === 'loss') {
    return LOSS_CLIP_URLS[clipNonce % LOSS_CLIP_URLS.length]
  }
  return VIDEO_MAP[playbackState]
}

export type PetVideoPlayerProps = {
  characterEvent?: CharacterEvent | null
  volatility?: number
  /** 黑底/绿幕素材：与父级正常合成时用 screen 去除暗部 */
  chromaScreen?: boolean
}

function tryPlay(el: HTMLVideoElement) {
  el.muted = true
  el.setAttribute('muted', '')
  el.defaultMuted = true
  void el.play().catch(() => {})
}

export default function PetVideoPlayer({
  characterEvent,
  volatility,
  chromaScreen = false,
}: PetVideoPlayerProps) {
  const videoRef = useRef<HTMLVideoElement>(null)
  const [mediaFailed, setMediaFailed] = useState(false)
  const { playbackState, shouldLoop, clipNonce, onClipEnded, bubbleText, bubbleVisible } = usePetState({
    characterEvent,
    volatility,
  })

  const src = resolveVideoSrc(playbackState, clipNonce)

  useEffect(() => {
    setMediaFailed(false)
  }, [src, clipNonce])

  useEffect(() => {
    const el = videoRef.current
    if (!el) return

    el.loop = shouldLoop
    el.muted = true
    el.setAttribute('muted', '')
    el.defaultMuted = true
    el.playsInline = true
    el.setAttribute('playsInline', '')
    el.setAttribute('webkit-playsinline', '')

    el.src = src
    el.load()

    const onMeta = () => {
      setMediaFailed(false)
      tryPlay(el)
    }
    const onVis = () => {
      if (!shouldLoop && el.ended) return
      if (document.visibilityState === 'visible') tryPlay(el)
    }
    const onErr = () => setMediaFailed(true)

    el.addEventListener('loadeddata', onMeta)
    el.addEventListener('canplay', onMeta)
    el.addEventListener('error', onErr)
    document.addEventListener('visibilitychange', onVis)
    tryPlay(el)

    return () => {
      el.removeEventListener('loadeddata', onMeta)
      el.removeEventListener('canplay', onMeta)
      el.removeEventListener('error', onErr)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [src, shouldLoop, clipNonce])

  return (
    <div className={styles.wrap}>
      {mediaFailed ? (
        <div className={`${styles.fallback} pet-video-fallback`}>
          <div>
            <div style={{ marginBottom: 8, color: 'var(--text-primary)' }}>未加载到视频</div>
            请将 5 个文件放入仓库 <code>web/video/</code> 并命名为
            <br />
            <code>idle · profit · loss · stoploss · boring</code>
            <br />
            扩展名为 <code>.mp4</code>（亏损态可加 <code>loss2.mp4</code> 轮换）。<br />
            用 Python 打开时<strong>重启 main.py</strong>；本地开发可 <code>npm run dev</code>。
          </div>
        </div>
      ) : null}
      {bubbleVisible && bubbleText ? (
        <div className={`${styles.speechBubble} speech-bubble`} role="status">
          {bubbleText}
        </div>
      ) : null}
      <video
        ref={videoRef}
        className={`${styles.video} ${chromaScreen ? styles.videoChroma : ''}`}
        src={src}
        loop={shouldLoop}
        muted
        playsInline
        autoPlay
        preload="auto"
        onEnded={onClipEnded}
      />
    </div>
  )
}
