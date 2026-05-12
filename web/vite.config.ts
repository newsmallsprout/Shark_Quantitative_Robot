import { defineConfig, type Plugin } from 'vitest/config'
import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'
import type { IncomingMessage, ServerResponse } from 'node:http'

/** 解析视频目录：优先 web/video，其次 仓库根目录/video（与 main.py 挂载顺序一致） */
function resolveVideoDirs(): string[] {
  const a = path.resolve(__dirname, 'video')
  const b = path.resolve(__dirname, '..', 'video')
  const dirs: string[] = []
  for (const d of [a, b]) {
    try {
      if (fs.existsSync(d) && fs.statSync(d).isDirectory()) dirs.push(d)
    } catch {
      /* ignore */
    }
  }
  return dirs
}

/** 将仓库内 video/*.mp4 暴露为 /video/*（dev 中间件 + build 复制到 dist/video） */
function webVideoDirPlugin(): Plugin {
  const dirs = resolveVideoDirs()
  const primary = dirs[0] ?? path.resolve(__dirname, 'video')

  return {
    name: 'web-video-dir',
    configureServer(server) {
      server.middlewares.use((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        const raw = req.url?.split('?')[0] ?? ''
        if (!raw.startsWith('/video/')) return next()
        const seg = raw.slice('/video/'.length)
        if (!seg || seg.includes('/') || seg.includes('..')) return next()
        if (!/^[a-zA-Z0-9._-]+\.mp4$/i.test(seg)) return next()

        let absPath: string | null = null
        for (const dir of dirs.length > 0 ? dirs : [primary]) {
          const fp = path.join(dir, seg)
          try {
            if (fs.existsSync(fp) && fs.statSync(fp).isFile()) {
              absPath = fp
              break
            }
          } catch {
            /* ignore */
          }
        }
        if (!absPath) return next()

        res.setHeader('Content-Type', 'video/mp4')
        res.setHeader('Accept-Ranges', 'bytes')
        fs.createReadStream(absPath).pipe(res)
      })
    },
    closeBundle() {
      const out = path.resolve(__dirname, 'dist', 'video')
      const scanDirs = dirs.length > 0 ? dirs : [primary]
      let copied = false
      for (const dir of scanDirs) {
        if (!fs.existsSync(dir)) continue
        fs.mkdirSync(out, { recursive: true })
        for (const f of fs.readdirSync(dir)) {
          if (!f.toLowerCase().endsWith('.mp4')) continue
          fs.copyFileSync(path.join(dir, f), path.join(out, f))
          copied = true
        }
      }
      if (!copied && fs.existsSync(primary)) {
        fs.mkdirSync(out, { recursive: true })
        for (const f of fs.readdirSync(primary)) {
          if (!f.toLowerCase().endsWith('.mp4')) continue
          fs.copyFileSync(path.join(primary, f), path.join(out, f))
        }
      }
    },
  }
}

export default defineConfig({
  plugins: [react(), webVideoDirPlugin()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    globals: false,
    passWithNoTests: true,
  },
  build: {
    target: 'es2020',
    minify: 'esbuild',
    rollupOptions: {
      output: {
        manualChunks: {
          'vendor-react': ['react', 'react-dom'],
        },
      },
    },
    chunkSizeWarningLimit: 300,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8002',
      '/ws': { target: 'ws://127.0.0.1:8002', ws: true },
    }
  }
})
