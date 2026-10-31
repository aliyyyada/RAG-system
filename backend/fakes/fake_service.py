"""Фейковый внешний сервис завода — только для разработки и тестов.

Изображает систему, где уже заведены сотрудники: отвечает на проверку логина и пароля
(тот же формат, что ждёт ExternalCheckProvider). На этапе 2 сюда добавятся тестовые
документы, на этапе 3 — списки доступных id документов.

Запуск в docker compose — сервис `fake-service` (профиль dev), по HTTPS с dev-сертификатом.
В prod не запускается (ENABLE_TEST_USERS=false и профиль dev выключен).
"""

import hmac
import os

from fastapi import FastAPI
from pydantic import BaseModel

# Пароль у всех сотрудников фейкового сервиса одинаковый, его можно переопределить.
FAKE_PASSWORD = os.environ.get("FAKE_SERVICE_PASSWORD", "demo-password-1")


class Employee(BaseModel):
    personnel_number: str
    full_name: str
    department: str


EMPLOYEES: dict[str, Employee] = {
    e.personnel_number: e
    for e in (
        Employee(
            personnel_number="10234",
            full_name="Иванова Анна Сергеевна",
            department="Служба ОТ и ПБ",
        ),
        Employee(
            personnel_number="00017", full_name="Сидоров Кирилл Павлович", department="Отдел ИТ"
        ),
        Employee(
            personnel_number="20871",
            full_name="Петров Виктор Николаевич",
            department="Литейный цех № 2",
        ),
        Employee(
            personnel_number="31455",
            full_name="Орлова Мария Ильинична",
            department="Сборочный цех",
        ),
    )
}


class CheckRequest(BaseModel):
    login: str
    password: str


app = FastAPI(title="Фейковый внешний сервис (разработка)", docs_url="/docs")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/auth/check")
async def check(body: CheckRequest) -> dict[str, object]:
    employee = EMPLOYEES.get(body.login.strip())
    ok = employee is not None and hmac.compare_digest(body.password, FAKE_PASSWORD)
    if not ok or employee is None:
        return {"ok": False}
    return {"ok": True, "full_name": employee.full_name, "department": employee.department}
