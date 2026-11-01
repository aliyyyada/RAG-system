import { expect, test, type Page } from '@playwright/test'
import { loginAs } from './helpers'

// Этап 5: граф знаний. Связи по правилам строятся сразу после загрузки документов:
// приказ № 112 отменяет приказ № 45, приказ № 140 изменяет приказ № 112.
// T-0001 приказ № 45 не видит, T-0004 (администратор) по правам источника видит всё.

async function openDocument(page: Page, title: string) {
  await expect(async () => {
    await page.goto('/documents')
    await page.getByRole('link', { name: title }).click({ timeout: 2_000 })
  }).toPass({ timeout: 120_000 })
  await expect(page.getByLabel('Текст документа')).toBeVisible()
}

test('связи документа видны с учётом прав', async ({ browser }) => {
  test.setTimeout(180_000)
  const admin = await (await browser.newContext()).newPage()
  await loginAs(admin, 'T-0004')
  await openDocument(admin, 'О допуске персонала к работам на высоте')
  const links = admin.getByTestId('doc-links')
  await expect(links).toBeVisible({ timeout: 30_000 })
  await expect(links.getByRole('link', { name: /Отменяет.*Отменён.*Приказ № 45/ })).toBeVisible()
  await expect(links.getByRole('link', { name: /Изменён документом.*Приказ № 140/ })).toBeVisible()
  // Статус по связи «изменяет»: «Действует» и «Есть изменения».
  await expect(admin.locator('.doc-toolbar').getByText('Есть изменения')).toBeVisible()
  await links.getByRole('link', { name: /Отменяет/ }).click()
  await expect(admin.getByRole('heading', { level: 1, name: /Приказ № 45/ })).toBeVisible()
  await expect(admin.locator('.doc-toolbar').getByText('Отменён')).toBeVisible()

  // T-0001: приказ № 45 ему не виден — в связях только реквизиты из текста, без ссылки на документ.
  const user = await (await browser.newContext()).newPage()
  await loginAs(user, 'T-0001')
  await openDocument(user, 'О допуске персонала к работам на высоте')
  const own = user.getByTestId('doc-links')
  await expect(own).toBeVisible({ timeout: 30_000 })
  await expect(own.getByRole('link', { name: /Отменяет.*недоступен.*Приказ № 45/ })).toBeVisible()
  await own.getByRole('link', { name: /Отменяет/ }).click()
  // Ссылка ведёт на место в самом приказе № 112, а не в приказ № 45.
  await expect(user.getByRole('heading', { level: 1, name: /Приказ № 112/ })).toBeVisible()
  await expect(user.locator('.chunk-view.hl')).toContainText('признать утратившим силу')
})

test('режим поиска переключается и запоминается', async ({ page }) => {
  await loginAs(page, 'T-0002')
  await page.goto('/')
  const mode = page.getByTestId('search-mode')
  await expect(mode).toHaveValue('mixed')
  await mode.selectOption('graph')
  await page.reload()
  await expect(page.getByTestId('search-mode')).toHaveValue('graph')
  await page.getByTestId('search-mode').selectOption('mixed')
})

test('администратор видит граф знаний и прогноз', async ({ page }) => {
  await loginAs(page, 'T-0004')
  await page.goto('/admin/graph')
  await expect(page.getByRole('heading', { name: 'Граф знаний' })).toBeVisible()
  await expect(page.getByText('Шаг 1. Связи по правилам')).toBeVisible()
  await expect(page.getByText('Шаг 2. Сущности и связи моделью')).toBeVisible()
  await expect(page.getByText('Осталось моделью')).toBeVisible()
  await expect(page.getByTestId('graph-llm-toggle')).toBeVisible()
})
