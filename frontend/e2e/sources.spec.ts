import { expect, test } from '@playwright/test'
import { loginAs } from './helpers'

// Нужен запущенный worker и подключённый фейковый источник:
//   python -m app.cli seed-fake-source --url https://localhost:8443/source
//   python -m app.worker.main
// (так и делает CI). Первая синхронизация загружает 8 документов, 2 из них — с ошибкой.

test.beforeEach(async ({ page }) => {
  await loginAs(page, 'T-0004')
})

test('источник: отчёт о полноте, ошибки и повтор', async ({ page }) => {
  test.setTimeout(150_000)
  await page.goto('/admin/sources')
  await expect(page.getByRole('heading', { name: 'Источники' })).toBeVisible()
  await page.getByRole('link', { name: /Документооборот/ }).click()

  // Ждём окончания первичной индексации (worker обрабатывает документы в фоне).
  await expect(page.getByText('очередь пуста')).toBeVisible({ timeout: 90_000 })
  await expect(page.getByText('Источник отвечает')).toBeVisible()
  const tiles = page.locator('.tiles')
  await expect(tiles.locator('.tile').nth(0)).toContainText('8')
  await expect(tiles.locator('.tile').nth(1)).toContainText('6')
  await expect(tiles.locator('.tile').nth(3)).toContainText('2')

  const errors = page.locator('.card', { has: page.getByRole('heading', { name: 'Документы с ошибкой' }) })
  await expect(errors.getByRole('row', { name: /Приказ № 97/ })).toContainText('пустой (0 байт)')
  await expect(errors.getByRole('row', { name: /Журнал регистрации инструктажей/ })).toContainText(
    'ожидалось 148 строк, пришло 120',
  )
  await expect(page.getByRole('row', { name: /Полная сверка/ }).first()).toContainText('Успешно')

  await errors.getByRole('row', { name: /Приказ № 97/ }).getByRole('button', { name: 'Повторить' }).click()
  await expect(page.getByText('Документ поставлен в очередь')).toBeVisible()
})

test('карточка документа: фрагменты с разделом и страницей, распознанный текст, история', async ({ page }) => {
  await page.goto('/admin/documents')
  await expect(page.getByRole('heading', { name: 'Коллекции и документы' })).toBeVisible()
  await page.getByLabel('Поиск документа').fill('О допуске')
  await page.getByRole('link', { name: /Приказ № 112 от 14\.03\.2024/ }).click()

  await expect(page.getByRole('heading', { name: 'Приказ № 112 от 14.03.2024' })).toBeVisible()
  await expect(page.getByText('«Ознакомлены» — 46 строк')).toBeVisible()
  const p42 = page.locator('.chunk', { hasText: '4.2. Допуск к работам на высоте' })
  await expect(p42.locator('.chunk-h')).toContainText('стр. 2')
  await expect(p42.locator('.chunk-h')).toContainText('раздел 4. Порядок допуска')
  await expect(page.locator('.chunk', { hasText: '| Группа | Кто | Периодичность |' })).toBeVisible()

  await page.getByRole('tab', { name: 'Распознанный текст' }).click()
  await expect(page.getByText('--- стр. 2 ---', { exact: false })).toBeVisible()
  await page.getByRole('tab', { name: /История/ }).click()
  await expect(page.getByRole('cell', { name: 'Обработан' }).first()).toBeVisible()

  const excludedBadge = page.locator('.page-h .badge', { hasText: 'Исключён из поиска' })
  await page.getByRole('button', { name: 'Исключить из поиска' }).click()
  await expect(excludedBadge).toBeVisible()
  await page.getByRole('button', { name: 'Вернуть в поиск' }).click()
  await expect(excludedBadge).toBeHidden()
})

test('ручная загрузка DOCX', async ({ page }) => {
  await page.goto('/admin/documents')
  await page.getByRole('button', { name: 'Загрузить документ' }).click()
  const dlg = page.getByRole('dialog', { name: 'Загрузить документ' })
  await dlg.getByLabel('Файл PDF или DOCX').setInputFiles('../backend/fakes/files/polozhenie_suot.docx')
  await dlg.getByLabel('Название').fill('Положение о СУОТ (ручная загрузка)')
  await dlg.getByRole('button', { name: 'Загрузить' }).click()
  await expect(page.getByRole('heading', { name: 'Положение о СУОТ (ручная загрузка)' })).toBeVisible()
  await expect(page.locator('.page-h .badge', { hasText: 'Готово' })).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('.chunk', { hasText: '2.1. Работник обязан соблюдать' })).toBeVisible()
})
