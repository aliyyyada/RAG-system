import { expect, test, type Page } from '@playwright/test'
import { loginAs } from './helpers'

// Этап 4: вопрос → ответ потоком со ссылками на источники → документ на нужном месте.
// Нужны worker, фейковый источник и фейковая Ollama (как в CI). Права те же, что в
// documents.spec.ts: гостю — только общие документы (ИОТ-044, Положение о СУОТ),
// T-0001 — ещё приказы 112, 140 и регламент ТО, но не приказ № 45.

const box = (page: Page) => page.getByRole('textbox', { name: 'Вопрос', exact: true })

async function ask(page: Page, question: string) {
  await box(page).fill(question)
  await box(page).press('Enter')
}

/** Документы могут ещё индексироваться: спрашиваем, пока ответ не сошлётся на источник. */
async function askUntilCited(page: Page, question: string) {
  await expect(async () => {
    await page.getByRole('button', { name: 'Новый вопрос' }).click()
    await ask(page, question)
    await expect(page.locator('.a-foot').last()).toBeVisible({ timeout: 15_000 })
    await expect(page.locator('.a-body button.cite').first()).toBeVisible({ timeout: 1_000 })
  }).toPass({ timeout: 120_000 })
}

test('гость получает ответ по общим документам и открывает источник', async ({ page }) => {
  test.setTimeout(180_000)
  await page.goto('/')
  await expect(page.getByText('Вы вошли как гость')).toBeVisible()
  await askUntilCited(page, 'Сколько пар рукавиц выдают электросварщику?')

  const answer = page.locator('.a-body').last()
  await expect(answer).toContainText(/рукавиц/i)
  const source = page.getByRole('complementary', { name: 'Источники ответа' }).locator('.src').first()
  await expect(source).toContainText('ИОТ-044')

  // Номер [1] в ответе подсвечивает источник, клик по источнику открывает документ на месте.
  await answer.getByRole('button', { name: 'Источник 1' }).click()
  await expect(source).toHaveClass(/on/)
  await source.click()
  await expect(page).toHaveURL(/\/documents\/\d+\?chunk=\d+/)
  await expect(page.locator('.chunk-view.hl')).toContainText(/рукавиц/i)

  await page.getByRole('button', { name: 'К ответу' }).click()
  await expect(page.locator('.a-body').last()).toContainText(/рукавиц/i)
})

test('пользователь видит ответ только по своим документам, чат сохраняется в истории', async ({ page }) => {
  test.setTimeout(180_000)
  await loginAs(page, 'T-0001')
  await page.goto('/')
  await askUntilCited(page, 'Кто подписывает наряд-допуск на работы на высоте?')

  await expect(page.locator('.a-body').last()).toContainText('ответственный руководитель работ')
  const sources = page.getByRole('complementary', { name: 'Источники ответа' })
  await expect(sources).toContainText('Приказ № 112')
  // Приказ № 45 «О работах на высоте» T-0001 не разрешён — его нет даже среди найденного.
  const all = sources.getByRole('button', { name: /Показать все найденные/ })
  if (await all.isVisible()) await all.click()
  await expect(sources.locator('.src-t')).not.toHaveCount(0)
  await expect(sources.locator('.src-t').filter({ hasText: 'Приказ № 45 от' })).toHaveCount(0)

  // Уточняющий вопрос в том же чате, затем перезагрузка — диалог из истории.
  await ask(page, 'А на какой срок он выдаётся?')
  await expect(page.locator('.a-body').nth(1)).toContainText('15 календарных дней')
  await expect(page).toHaveURL(/\?chat=\d+/)
  await page.reload()
  await expect(page.locator('.q')).toHaveCount(2)
  await expect(page.getByRole('complementary', { name: 'История чатов' })).toContainText('Кто подписывает наряд-допуск')

  // Вопрос не по документам — честный отказ без источников.
  await ask(page, 'Сколько стоит абонемент в бассейн?')
  await expect(page.locator('.no-answer')).toContainText('ответа на этот вопрос нет')
})
