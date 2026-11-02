import { expect, test } from '@playwright/test'
import { loginAs } from './helpers'

// Этап 6: противоречия. В CI поиск включён командой `python -m app.cli contra on`.
// Положение о СУОТ (общее): обучение по работам на высоте «не реже одного раза в год»,
// приказ № 112: «1 раз в 3 года» — активное противоречие. Приказ № 140 изменяет срок
// наряда-допуска в приказе № 112 — расхождение есть, но оно не активно.

test('противоречия: просмотр, неактивные, подтверждение и права', async ({ browser }) => {
  test.setTimeout(240_000)

  // Аналитик: право «Просмотр противоречий», без права подтверждать.
  const analyst = await (await browser.newContext()).newPage()
  await loginAs(analyst, 'T-0002')
  await expect(async () => {
    await analyst.goto('/contradictions')
    await expect(analyst.getByTestId('contra-list')).toBeVisible({ timeout: 2_000 })
  }).toPass({ timeout: 180_000 })
  const card = analyst.locator('.cpair').filter({ hasText: 'Положение о системе управления охраной труда' })
  await expect(card).toContainText('Прямое противоречие')
  await expect(card.locator('mark').first()).toBeVisible()
  await expect(card.getByRole('button', { name: 'Подтвердить' })).toHaveCount(0)
  await expect(analyst.getByTestId('contra-count')).toHaveText('1')

  // Неактивные: приказ № 140 изменяет приказ № 112.
  await analyst.getByTestId('contra-active').selectOption('inactive')
  await expect(analyst.getByTestId('contra-inactive').first()).toContainText('изменяет Приказ № 112')
  await analyst.getByTestId('contra-active').selectOption('active')

  // Ссылка открывает документ на месте фрагмента.
  await card.getByRole('link', { name: /Приказ № 112/ }).click()
  await expect(analyst.locator('.chunk-view.hl')).toContainText('1 раз в 3 года')

  // Администратор подтверждает — пара уходит во вкладку «Подтверждённые».
  const admin = await (await browser.newContext()).newPage()
  await loginAs(admin, 'T-0004')
  await admin.goto('/contradictions')
  const own = admin.locator('.cpair').filter({ hasText: 'Положение о системе управления охраной труда' })
  await own.getByRole('button', { name: 'Подтвердить' }).click()
  await expect(admin.getByText('Новых противоречий в доступных вам документах нет')).toBeVisible()
  await admin.getByRole('tab', { name: /Подтверждённые/ }).click()
  await expect(admin.locator('.cpair')).toContainText('Подтверждено')
  await admin.locator('.cpair').getByRole('button', { name: 'Вернуть в новые' }).click()
  await expect(admin.getByText('В этом разделе пока пусто.')).toBeVisible()

  // Обычный пользователь: права нет — нет пункта в шапке и страница закрыта.
  const user = await (await browser.newContext()).newPage()
  await loginAs(user, 'T-0001')
  await expect(user.getByRole('navigation', { name: 'Разделы' }).getByRole('link', { name: /Противоречия/ })).toHaveCount(0)
  await user.goto('/contradictions')
  await expect(user).toHaveURL(/forbidden/)
})

test('администратор видит поиск противоречий и прогноз', async ({ page }) => {
  await loginAs(page, 'T-0004')
  await page.goto('/admin/contradictions')
  await expect(page.getByRole('heading', { name: 'Поиск противоречий' })).toBeVisible()
  await expect(page.getByTestId('contra-toggle')).toHaveText(/Выключить/)
  await expect(page.getByText('Проверка моделью')).toBeVisible()
})
