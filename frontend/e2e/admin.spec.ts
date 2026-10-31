import { expect, test } from '@playwright/test'
import { loginAs } from './helpers'

const suffix = Date.now().toString().slice(-6)

test.beforeEach(async ({ page }) => {
  await loginAs(page, 'T-0004')
})

test('пользователи: список, поиск и назначение роли по табельному номеру', async ({ page }) => {
  await page.getByRole('link', { name: 'Администрирование' }).click()
  await expect(page.getByRole('heading', { name: 'Пользователи и группы' })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'T-0001' })).toBeVisible()

  await page.getByLabel('Поиск пользователя').fill('аналитик')
  await expect(page.getByRole('cell', { name: 'T-0002' })).toBeVisible()
  await expect(page.getByRole('cell', { name: 'T-0001' })).toHaveCount(0)
  await page.getByLabel('Поиск пользователя').fill('')

  const number = '7' + suffix
  await page.getByRole('button', { name: 'Назначить роль' }).click()
  const dlg = page.getByRole('dialog', { name: 'Назначить роль' })
  await dlg.getByLabel('Табельный номер').fill(number)
  await dlg.getByLabel('ФИО').fill('Новиков Пётр Ильич')
  await dlg.getByLabel('Аналитик', { exact: true }).check()
  await dlg.getByRole('button', { name: 'Назначить' }).click()
  await expect(dlg).toBeHidden()
  await page.getByLabel('Поиск пользователя').fill(number)
  const row = page.getByRole('row', { name: new RegExp(number) })
  await expect(row).toContainText('Новиков Пётр Ильич')
  await expect(row).toContainText('Пользователь, Аналитик')
  await expect(row).toContainText('Внешний')
})

test('группы: создать группу с ролью и участником', async ({ page }) => {
  await page.goto('/admin/users?tab=groups')
  await page.getByRole('button', { name: 'Новая группа' }).click()
  const dlg = page.getByRole('dialog', { name: 'Новая группа' })
  await dlg.getByLabel('Название').fill('Отдел ОТ и ПБ ' + suffix)
  await dlg.getByLabel('Аналитик', { exact: true }).check()
  await dlg.getByLabel('Найти пользователя').fill('T-0003')
  await dlg.getByRole('button', { name: /T-0003/ }).click()
  await expect(dlg.getByLabel(/Тест: без доступа/)).toBeChecked()
  await dlg.getByRole('button', { name: 'Сохранить' }).click()
  await expect(dlg).toBeHidden()
  const row = page.getByRole('row', { name: new RegExp('Отдел ОТ и ПБ ' + suffix) })
  await expect(row).toContainText('Аналитик')
  await expect(row.getByRole('cell').nth(3)).toHaveText('1')
})

test('роли: новая роль, галочка права, сохранение и удаление', async ({ page }) => {
  await page.goto('/admin/roles')
  await expect(page.getByRole('heading', { name: 'Роли и права' })).toBeVisible()

  // Встроенные ограничения видны сразу: гостю нельзя выдать просмотр документов.
  await expect(page.getByLabel('Гость: Просмотр документов по правам источника')).toBeDisabled()
  await expect(page.getByLabel('Администратор: Журнал аудита')).toBeChecked()
  await expect(page.getByLabel('Администратор: Журнал аудита')).toBeDisabled()

  const name = 'Специалист ОТ ' + suffix
  await page.getByRole('button', { name: 'Новая роль' }).click()
  const dlg = page.getByRole('dialog', { name: 'Новая роль' })
  await dlg.getByLabel('Название').fill(name)
  await dlg.getByRole('button', { name: 'Создать' }).click()
  await expect(dlg).toBeHidden()

  const box = page.getByLabel(`${name}: Просмотр противоречий`)
  await box.check()
  await page.getByRole('button', { name: 'Сохранить' }).click()
  await expect(page.getByText('Права сохранены')).toBeVisible()
  await page.reload()
  await expect(page.getByLabel(`${name}: Просмотр противоречий`)).toBeChecked()

  await page.getByRole('button', { name }).click()
  const edit = page.getByRole('dialog', { name: 'Роль' })
  await edit.getByRole('button', { name: 'Удалить роль' }).click()
  await edit.getByRole('button', { name: 'Точно удалить?' }).click()
  await expect(edit).toBeHidden()
  await expect(page.getByRole('button', { name })).toHaveCount(0)
})
