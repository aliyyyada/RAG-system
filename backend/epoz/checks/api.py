"""API проверки ЕПоЗ для пользователя: `/api/checks`.

Правила:
- нужно право `skill.epoz_check.use` и доступ к самому ЕПоЗ: результат цитирует его пункты, поэтому
  человеку без доступа к комплекту проверку запускать нельзя (иначе он прочитал бы закрытые пункты);
- проверку видит только её автор: чужая отдаёт 404, даже администратору;
- у пользователя одна текущая проверка: новая заменяет прежнюю (старые данные удаляются).
"""

from __future__ import annotations

import json
import shutil
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.access.filters import visible
from app.access.service import resolve_access
from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, client_ip, require_permission
from app.auth.rbac import Principal
from app.auth.service import write_audit
from app.ingest.files import EXTENSIONS, detect_type
from app.models import Document
from epoz import loader
from epoz.checks import runner, service
from epoz.checks.roles import ROLES, guess_role
from epoz.models import CheckFile, CheckRun, EpozPart

router = APIRouter(prefix="/api/checks", tags=["проверка ЕПоЗ"])

CheckUser = Annotated[Principal, Depends(require_permission(perms.skill_permission("epoz_check")))]


def _err(code: int, key: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message})


async def _epoz_state(
    session: AsyncSession, principal: Principal, settings: Any
) -> tuple[bool, str | None]:
    """Можно ли запускать проверку: есть действующий ЕПоЗ и человеку открыт весь комплект."""
    ed = await loader.get_active(session)
    if ed is None:
        return False, "ЕПоЗ ещё не загружен: обратитесь к администратору"
    access = await resolve_access(session, principal, settings)
    doc_ids = list(
        await session.scalars(select(EpozPart.document_id).where(EpozPart.edition_id == ed.id))
    )
    seen = set(
        await session.scalars(select(Document.id).where(Document.id.in_(doc_ids), visible(access)))
    )
    if not seen or len(seen) < len(doc_ids):
        return (
            False,
            "У вас нет доступа к ЕПоЗ: обратитесь к администратору (доступ выдаётся группе)",
        )
    return True, None


async def _own_run(session: AsyncSession, principal: Principal, run_id: int) -> CheckRun:
    run = await session.get(CheckRun, run_id)
    if run is None or principal.user is None or run.user_id != principal.user.id:
        raise _err(
            404, "not_found", "Проверка не найдена"
        )  # чужая проверка неотличима от несуществующей
    return run


@router.get("/config")
async def config(
    principal: CheckUser, session: SessionDep, settings: SettingsDep
) -> dict[str, Any]:
    ready, reason = await _epoz_state(session, principal, settings)
    criteria = await service.ensure_criteria(session)
    await session.commit()
    return {
        "ready": ready,
        "reason": reason,
        "roles": [{"key": k, "label": v} for k, v in ROLES.items()],
        "criteria": [
            {"code": c.code, "block": c.block, "block_title": c.block_title, "title": c.title}
            for c in criteria
            if c.enabled
        ],
        "customer_group": await service.customer_group(session),
        "limits": {
            "max_files": settings.check_max_files,
            "max_file_mb": settings.check_max_file_mb,
        },
        "guess": {},
    }


@router.post("/guess-roles")
async def guess_roles(_: CheckUser, names: Annotated[str, Form()]) -> dict[str, str]:
    """Роли по именам файлов (для формы: подсказка, пользователь может поменять)."""
    try:
        items = json.loads(names)
    except ValueError:
        raise _err(422, "bad_names", "Ожидается список имён файлов") from None
    return {str(n): guess_role(str(n)) for n in items[:100]}


@router.post("", status_code=status.HTTP_201_CREATED)
async def start_check(
    principal: CheckUser,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
    files: Annotated[list[UploadFile], File(description="Файлы пакета")],
    roles: Annotated[list[str], Form(description="Роль каждого файла в том же порядке")],
    winner: Annotated[int, Form(description="Номер файла-победителя КП (с нуля) или -1")] = -1,
) -> dict[str, Any]:
    user = principal.user
    assert user is not None
    ready, reason = await _epoz_state(session, principal, settings)
    if not ready:
        raise _err(
            409 if "не загружен" in (reason or "") else 403,
            "epoz_unavailable",
            reason or "ЕПоЗ недоступен",
        )
    if not files or len(files) > settings.check_max_files:
        raise _err(422, "bad_files", f"Загрузите от 1 до {settings.check_max_files} файлов")
    if len(roles) != len(files) or any(r not in ROLES for r in roles):
        raise _err(422, "bad_roles", "У каждого файла должна быть роль из списка")
    if winner != -1 and (not 0 <= winner < len(files) or roles[winner] != "kp"):
        raise _err(
            422,
            "bad_winner",
            "Победителем можно отметить только файл с ролью «Коммерческое предложение»",
        )

    # Одна текущая проверка: прежние данные пользователя удаляются.
    await service.delete_user_runs(session, settings, user.id)
    run = CheckRun(
        user_id=user.id,
        session_id=principal.session_id,
        status="queued",
        params={"customer_group": await service.customer_group(session)},
        epoz_edition_id=(await loader.get_active(session)).id,  # type: ignore[union-attr]
    )
    session.add(run)
    await session.flush()
    folder = service.run_dir(settings, run.id)
    folder.mkdir(parents=True, exist_ok=True)
    limit = settings.check_max_file_mb * 1024 * 1024
    try:
        for i, (f, role) in enumerate(zip(files, roles, strict=True)):
            name = (f.filename or f"file{i}").replace("/", "_").replace("\\", "_")[:500]
            tmp = folder / f"{i}.part"
            size = 0
            with tmp.open("wb") as out:
                while chunk := await f.read(1 << 16):
                    size += len(chunk)
                    if size > limit:
                        raise _err(
                            413,
                            "too_large",
                            f"Файл «{name}» больше {settings.check_max_file_mb} МБ",
                        )
                    out.write(chunk)
            if size == 0:
                raise _err(422, "empty", f"Файл «{name}» пустой")
            kind = detect_type(tmp)
            if kind is None:
                raise _err(
                    422,
                    "bad_type",
                    f"Файл «{name}»: поддерживаются PDF, DOCX, DOC и сканы TIFF, PNG, JPEG",
                )
            path = folder / f"{i}.{EXTENSIONS[kind]}"
            tmp.rename(path)
            session.add(
                CheckFile(
                    run_id=run.id,
                    ord=i,
                    name=name,
                    path=str(path),
                    size=size,
                    role=role,
                    is_winner=(i == winner),
                )
            )
    except BaseException:
        await session.rollback()
        shutil.rmtree(folder, ignore_errors=True)
        raise
    await write_audit(
        session,
        "epoz.check_start",
        actor=user,
        target_type="check_run",
        target_id=run.id,
        details={
            "files": len(files),
            "roles": roles,
        },  # только метаданные: ни имён файлов, ни содержимого
        ip=client_ip(request),
    )
    await runner.enqueue_run(session, run.id)
    await session.commit()
    return await service.serialize_run(session, run)


@router.get("/current")
async def current(principal: CheckUser, session: SessionDep) -> dict[str, Any]:
    """Последняя проверка пользователя (или null). Обращение продлевает её жизнь."""
    user = principal.user
    assert user is not None
    run = await session.scalar(
        select(CheckRun)
        .where(CheckRun.user_id == user.id)
        .order_by(CheckRun.created_at.desc())
        .limit(1)
    )
    if run is None:
        return {"run": None}
    await service.touch(session, run.id)
    data = await service.serialize_run(session, run)
    await session.commit()
    return {"run": data}


@router.get("/{run_id}")
async def get_run(run_id: int, principal: CheckUser, session: SessionDep) -> dict[str, Any]:
    run = await _own_run(session, principal, run_id)
    await service.touch(session, run.id)
    data = await service.serialize_run(session, run)
    await session.commit()
    return data


@router.delete("/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def clear_run(
    run_id: int, principal: CheckUser, session: SessionDep, settings: SettingsDep
) -> None:
    """Кнопка «Очистить»: стирает проверку, документы пакета и загруженные файлы."""
    run = await _own_run(session, principal, run_id)
    await service.delete_run(session, settings, run.id)
    await session.commit()
