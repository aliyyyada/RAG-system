import { fileURLToPath, URL } from 'node:url'
import { readFileSync, existsSync } from 'node:fs'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import tailwindcss from '@tailwindcss/vite'

// Разработка: `npm run dev` поднимает https://localhost:5173 и проксирует /api на backend.
// Адрес backend — переменная API_TARGET (по умолчанию uvicorn на этой же машине).
// HTTPS и в разработке (ТЗ): берём dev-сертификат из certs/, если он уже создан.
const certDir = fileURLToPath(new URL('../certs/', import.meta.url))
const https =
  existsSync(certDir + 'server.crt') && existsSync(certDir + 'server.key')
    ? { cert: readFileSync(certDir + 'server.crt'), key: readFileSync(certDir + 'server.key') }
    : undefined

export default defineConfig({
  plugins: [vue(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
      '@design': fileURLToPath(new URL('../design', import.meta.url)),
    },
  },
  server: {
    https,
    proxy: {
      '/api': { target: process.env.API_TARGET ?? 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
  preview: {
    https,
    proxy: {
      '/api': { target: process.env.API_TARGET ?? 'http://127.0.0.1:8000', changeOrigin: false },
    },
  },
  build: {
    // Всё (шрифты, иконки) кладём в dist: на заводе нет интернета, CDN не используем.
    assetsInlineLimit: 0,
  },
})
