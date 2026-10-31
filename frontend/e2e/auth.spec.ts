import { expect, test } from '@playwright/test'
import { FAKE_PASSWORD, loginAs } from './helpers'

test('гость видит поиск и кнопку «Войти», но не админку', async ({ page }) => {
  await page.goto('/')
  await expect(page.getByText('Гостевой режим')).toBeVisible()
  await expect(page.getByText('Вы вошли как гость')).toBeVisible()
  const nav = page.getByRole('navigation', { name: 'Разделы' })
  await expect(nav.getByRole('link', { name: 'Поиск' })).toBeVisible()
  await expect(nav.getByRole('link', { name: 'Администрирование' })).toHaveCount(0)

  // Прямой переход в админку без входа ведёт на страницу входа.
  await page.goto('/admin/users')
  await expect(page).toHaveURL(/\/login\?next=(%2F|\/)admin(%2F|\/)users/)
})

test('страница входа как в макете', async ({ page }) => {
  await page.goto('/login')
  await expect(page.getByRole('heading', { name: 'Поиск по документам' })).toBeVisible()
  await expect(page.getByRole('heading', { name: 'Вход' })).toBeVisible()
  await expect(page.getByRole('link', { name: 'Продолжить без входа' })).toBeVisible()
  await page.getByRole('link', { name: 'Продолжить без входа' }).click()
  await expect(page).toHaveURL(/\/$/)
})

test('неверный пароль: сообщение и счётчик попыток', async ({ page }) => {
  await page.goto('/login')
  await page.getByLabel('Табельный номер').fill('9' + Date.now().toString().slice(-6))
  await page.getByLabel('Пароль').fill('wrong-password')
  await page.getByRole('button', { name: 'Войти' }).click()
  const alert = page.getByRole('alert')
  await expect(alert).toContainText('Неверный табельный номер или пароль')
  await expect(alert).toContainText(/Осталось \d попытк/)
  await expect(page.getByLabel('Пароль')).toHaveValue('')
})

test('вход сотрудника через внешний сервис и выход', async ({ page }) => {
  await loginAs(page, '10234', FAKE_PASSWORD)
  await expect(page.getByText('Иванова А. С.')).toBeVisible()
  await expect(page.getByText('Служба ОТ и ПБ · 10234')).toBeVisible()

  // Перезагрузка страницы: сессия восстанавливается по refresh-cookie.
  await page.reload()
  await expect(page.getByText('Иванова А. С.')).toBeVisible()

  await page.getByRole('button', { name: 'Меню пользователя' }).click()
  await page.getByRole('menuitem', { name: 'Выйти', exact: true }).click()
  await expect(page.getByText('Гостевой режим')).toBeVisible()
  await page.reload()
  await expect(page.getByText('Гостевой режим')).toBeVisible()
})

test('обычный пользователь не попадает в админку', async ({ page }) => {
  await loginAs(page, 'T-0001')
  const nav = page.getByRole('navigation', { name: 'Разделы' })
  await expect(nav.getByRole('link', { name: 'Администрирование' })).toHaveCount(0)
  await page.goto('/admin/users')
  await expect(page.getByRole('heading', { name: 'Нет доступа к этому разделу' })).toBeVisible()
})
