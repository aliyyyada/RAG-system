import { defineConfig, devices } from '@playwright/test'

// E2E-тесты основных сценариев интерфейса. Нужен запущенный стек:
// backend (uvicorn) + фейковый внешний сервис + `npm run preview` (HTTPS, прокси /api).
// В CI всё поднимается в .github/workflows/ci.yml (задача frontend).
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: process.env.CI ? 1 : 0,
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: process.env.E2E_BASE_URL ?? 'https://localhost:4173',
    ignoreHTTPSErrors: true, // самоподписанный dev-сертификат
    locale: 'ru-RU',
    viewport: { width: 1440, height: 900 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
        launchOptions: process.env.PW_CHROMIUM_PATH
          ? { executablePath: process.env.PW_CHROMIUM_PATH }
          : {},
      },
    },
  ],
})
