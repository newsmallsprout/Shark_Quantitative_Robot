/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 与后端 SHARK_API_TOKEN 一致时，WebSocket 附带 ?token= */
  readonly VITE_SHARK_API_TOKEN?: string
  /** 本地 dev 且启用设备锁 MAC 时，与 SHARK_ALLOWED_MAC 一致 */
  readonly VITE_SHARK_CLIENT_MAC?: string
}

interface Window {
  /** 由 /api/bootstrap.js 在运行时注入（与 SHARK_API_TOKEN 一致），优先于 VITE_SHARK_API_TOKEN */
  __SHARK_API_TOKEN__?: string
  /** 设备锁启用且配置 SHARK_ALLOWED_MAC 时由 bootstrap 注入，供 fetch / WS 携带 */
  __SHARK_CLIENT_MAC__?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
