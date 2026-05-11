/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 与后端 SHARK_API_TOKEN 一致时，WebSocket 附带 ?token= */
  readonly VITE_SHARK_API_TOKEN?: string
}

interface Window {
  /** 由 /api/bootstrap.js 在运行时注入（与 SHARK_API_TOKEN 一致），优先于 VITE_SHARK_API_TOKEN */
  __SHARK_API_TOKEN__?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}
