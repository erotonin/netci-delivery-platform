import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

// Where the Portal's /api requests go. An environment variable because the lab and a
// developer's compose stack listen on different ports, and hardcoding one means the
// other silently talks to nothing.
const apiTarget = process.env.NETCI_API_URL ?? 'http://127.0.0.1:8000'
const apiProxy = {
  '/api': {
    target: apiTarget,
    changeOrigin: true,
    rewrite: (path: string) => path.replace(/^\/api/, ''),
  },
}

export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    clearMocks: true,
    maxWorkers: 1,
    // Component tests only. The e2e/ specs are Playwright's: they need a browser and a
    // running API, and vitest picking them up makes `npm test` fail for a reason that
    // has nothing to do with the code under test.
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
  server: { port: 5173, proxy: apiProxy },
  // The same proxy for `vite preview`, which serves the production build. Without it the
  // browser tests would exercise a bundle that cannot reach the API -- and the one thing
  // they exist to check is that the Portal and the API agree.
  preview: { proxy: apiProxy },
})
