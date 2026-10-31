"""Каталог прав (permissions) системы.

Право — атомарное действие. Роли собираются администратором из этих «галочек».
Каталог живёт в коде: права проверяет код, а новый навык (app/skills) приносит своё
право `skill.<имя>.use` вместе со своим кодом.

Пример: кнопка «Противоречия» в шапке видна, если у пользователя есть
`contradictions.view`, но настоящая защита — проверка того же права на бэкенде
(`require_permission("contradictions.view")`), скрытая кнопка защитой не считается.
"""

from dataclasses import dataclass

from app.skills.registry import SKILLS


@dataclass(frozen=True)
class Permission:
    code: str
    title: str
    group: str
    # Можно ли выдать это право гостю (без входа). Гостю доступны только общие документы,
    # поэтому таких прав ровно одно — search.common.
    guest_allowed: bool = False
    # Пометка «скоро» для навыков, которые ещё не реализованы.
    coming_soon: bool = False


GROUP_SEARCH = "Поиск и документы"
GROUP_CONTRA = "Противоречия"
GROUP_ADMIN = "Администрирование"
GROUP_SKILLS = "Навыки"

SEARCH_COMMON = "search.common"
DOCUMENTS_VIEW = "documents.view"
DOCUMENTS_UPLOAD_MANUAL = "documents.upload_manual"
CONTRADICTIONS_VIEW = "contradictions.view"
CONTRADICTIONS_MANAGE = "contradictions.manage"
ADMIN_USERS = "admin.users"
ADMIN_ROLES = "admin.roles"
ADMIN_INTEGRATIONS = "admin.integrations"
AUDIT_VIEW = "audit.view"

_BASE: tuple[Permission, ...] = (
    Permission(SEARCH_COMMON, "Поиск и вопросы по общим документам", GROUP_SEARCH, True),
    Permission(DOCUMENTS_VIEW, "Просмотр документов по правам источника", GROUP_SEARCH),
    Permission(DOCUMENTS_UPLOAD_MANUAL, "Ручная загрузка документов", GROUP_SEARCH),
    Permission(CONTRADICTIONS_VIEW, "Просмотр противоречий", GROUP_CONTRA),
    Permission(CONTRADICTIONS_MANAGE, "Подтверждение и отклонение", GROUP_CONTRA),
    Permission(ADMIN_USERS, "Пользователи и группы", GROUP_ADMIN),
    Permission(ADMIN_ROLES, "Роли и права", GROUP_ADMIN),
    Permission(ADMIN_INTEGRATIONS, "Источники и синхронизация", GROUP_ADMIN),
    Permission(AUDIT_VIEW, "Журнал аудита", GROUP_ADMIN),
)


def skill_permission(name: str) -> str:
    return f"skill.{name}.use"


def catalog() -> tuple[Permission, ...]:
    skills = tuple(
        Permission(skill_permission(s.name), s.title, GROUP_SKILLS, coming_soon=not s.available)
        for s in SKILLS
    )
    return _BASE + skills


def all_codes() -> frozenset[str]:
    return frozenset(p.code for p in catalog())


def guest_allowed_codes() -> frozenset[str]:
    return frozenset(p.code for p in catalog() if p.guest_allowed)


# Права, которые открывают раздел «Администрирование» в шапке.
ADMIN_AREA = frozenset({ADMIN_USERS, ADMIN_ROLES, ADMIN_INTEGRATIONS, AUDIT_VIEW})
