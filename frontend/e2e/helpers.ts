import { expect, type Page } from '@playwright/test'

// Пароли тестовых учёток и фейкового сервиса (только разработка, см. backend/app/cli.py).
export const TEST_PASSWORD = 'test-password-1'
export const FAKE_PASSWORD = 'demo-password-1'

export async function loginAs(page: Page, login: string, password = TEST_PASSWORD) {
  await page.goto('/login')
  await page.getByLabel('Табельный номер').fill(login)
  await page.getByLabel('Пароль').fill(password)
  await page.getByRole('button', { name: 'Войти' }).click()
  await expect(page.getByRole('button', { name: 'Меню пользователя' })).toBeVisible()
}
