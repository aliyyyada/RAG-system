import { expect, test, type Page } from '@playwright/test'
import { loginAs } from './helpers'

// Этап 3: документы по правам источника. Нужны worker и фейковый источник (как в CI).
// T-0001 источник разрешает приказы 112, 140 и регламент ТО; общие (видны всем, даже
// гостю) — ИОТ-044 и Положение о СУОТ. Приказ № 45 T-0001 не разрешён.

const rows = (page: Page) => page.locator('table.tbl tbody tr')

/** Ждём окончания первичной индексации: обновляем список, пока не будет n строк. */
async function waitRows(page: Page, n: number) {
  await expect(async () => {
    await page.reload()
    await expect(rows(page)).toHaveCount(n, { timeout: 2_000 })
  }).toPass({ timeout: 120_000 })
}

test('гость видит только общие документы и открывает их', async ({ page }) => {
  test.setTimeout(150_000)
  await page.goto('/documents')
  await expect(page.getByRole('heading', { name: 'Документы' })).toBeVisible()
  await expect(page.getByText('Вы вошли как гость')).toBeVisible()
  await waitRows(page, 2)
  await expect(rows(page).filter({ hasText: 'Инструкция по охране труда для электросварщика' })).toHaveCount(1)
  await expect(rows(page).filter({ hasText: 'Положение о системе управления охраной труда' })).toHaveCount(1)

  await page.getByRole('link', { name: 'Инструкция по охране труда для электросварщика' }).click()
  await expect(page.getByRole('heading', { name: /Инструкция № ИОТ-044/ })).toBeVisible()
  await expect(page.getByLabel('Текст документа')).toContainText('электросвар')
  const download = page.waitForEvent('download')
  await page.getByRole('button', { name: 'Скачать DOCX' }).click()
  expect((await download).suggestedFilename()).toBe('iot_044.docx')
})

test('пользователь видит только разрешённое, чужой документ закрыт по ссылке', async ({ browser }) => {
  test.setTimeout(150_000)
  // Администратор узнаёт адрес приказа № 45 (ему источник разрешает всё).
  const admin = await (await browser.newContext()).newPage()
  await loginAs(admin, 'T-0004')
  await admin.goto('/documents')
  await waitRows(admin, 6)
  await admin.getByRole('link', { name: 'О работах на высоте' }).click()
  await expect(admin.getByLabel('Текст документа')).toBeVisible()
  const url45 = new URL(admin.url()).pathname

  const page = await (await browser.newContext()).newPage()
  await loginAs(page, 'T-0001')
  await page.goto('/documents')
  await expect(page.getByText(/Доступно вам: 5 из 6 в системе/)).toBeVisible()
  await expect(rows(page)).toHaveCount(5)
  await expect(page.getByRole('link', { name: 'О работах на высоте' })).toHaveCount(0)

  await page.getByLabel('Поиск').fill('высот')
  await expect(rows(page)).toHaveCount(1)
  await expect(rows(page)).toContainText('О допуске персонала к работам на высоте')

  await page.goto(url45)
  await expect(page.getByRole('heading', { name: 'Нет доступа к документу' })).toBeVisible()
  await expect(page.getByText(/Источник «Документооборот.*» не выдал вам права на этот документ/)).toBeVisible()

  // Гость по той же ссылке — предложение войти.
  const guest = await (await browser.newContext()).newPage()
  await guest.goto(url45)
  await expect(guest.getByRole('heading', { name: 'Документ доступен только после входа' })).toBeVisible()
})

test('ручная загрузка: администратор открывает документ конкретному человеку', async ({ browser }) => {
  test.setTimeout(120_000)
  const admin = await (await browser.newContext()).newPage()
  await loginAs(admin, 'T-0004')
  await admin.goto('/admin/documents')
  await admin.getByRole('button', { name: 'Загрузить документ' }).click()
  const dlg = admin.getByRole('dialog', { name: 'Загрузить документ' })
  await dlg.getByLabel('Файл PDF или DOCX').setInputFiles('../backend/fakes/files/iot_044.docx')
  await dlg.getByLabel('Название').fill('Памятка электросварщику (для T-0001)')
  await dlg.getByRole('button', { name: 'Загрузить' }).click()
  await expect(admin.locator('.page-h .badge', { hasText: 'Готово' })).toBeVisible({ timeout: 60_000 })

  const user = await (await browser.newContext()).newPage()
  await loginAs(user, 'T-0001')
  await user.goto('/documents?source=manual')
  await expect(user.getByRole('link', { name: 'Памятка электросварщику (для T-0001)' })).toHaveCount(0)

  const card = admin.locator('.card', { has: admin.getByRole('heading', { name: 'Кому доступен' }) })
  await card.getByLabel('Табельный номер').fill('T-0001')
  await card.getByRole('button', { name: 'Добавить' }).click()
  await card.getByRole('button', { name: 'Сохранить доступ' }).click()
  await expect(card.getByText('Сохранено')).toBeVisible()

  await user.reload()
  await user.getByRole('link', { name: 'Памятка электросварщику (для T-0001)' }).click()
  await expect(user.getByLabel('Текст документа')).toContainText('электросвар')
})

test('администратор сбрасывает кэш прав', async ({ page }) => {
  await loginAs(page, 'T-0004')
  await page.goto('/admin/sources')
  await page.getByRole('button', { name: 'Сбросить кэш прав' }).click()
  await expect(page.getByText('Кэш прав сброшен')).toBeVisible()
})
