import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig(({ mode }) => ({
  plugins: [react()],
  base: mode === 'pages' ? '/Buffer/' : '/',
  build: {
    outDir: mode === 'pages' ? 'dist-pages' : 'dist',
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (!id.includes('node_modules')) return undefined
          if (/node_modules\/(react|react-dom|scheduler)\//.test(id)) return 'react'
          if (/node_modules\/(recharts|recharts-scale|victory-vendor|d3-[^/]+|internmap|decimal\.js-light|eventemitter3|lodash|react-smooth|react-transition-group|fast-equals|clsx|tiny-invariant)\//.test(id)) return 'charts'
          return undefined
        },
      },
    },
  },
  server: { port: 5173, proxy: { '/v1': 'http://127.0.0.1:8000', '/docs': 'http://127.0.0.1:8000', '/openapi.json': 'http://127.0.0.1:8000' } },
}))
