"""API администрирования ЕПоЗ: редакции, загрузка файлов, группа доступа, поиск пункта.

Все эндпоинты — только по праву `epoz.manage`. Подробный порядок работы — в `epoz/loader.py`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, client_ip, require_permission
from app.auth.rbac import Principal
from app.auth.service import write_audit
from app.core.flags import get_flag, set_flag
from app.ingest.storage import FileStore
from app.models import Group
from epoz import loader, okpd2
from epoz.models import EpozEdition

router = APIRouter(prefix="/api/admin/epoz", tags=["epoz"])

EpozAdmin = Annotated[Principal, Depends(require_permission(perms.EPOZ_MANAGE))]


def _err(code: int, key: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message})


def _who(principal: Principal) -> str | None:
    return principal.user.personnel_number if principal.user else None


async def _status(session: Any) -> dict[str, Any]:
    active = await loader.get_active(session)
    draft = await loader.get_draft(session)
    groups = (await session.execute(select(Group.id, Group.name).order_by(Group.name))).all()
    return {
        "active": (
            {
                "id": active.id,
                "title": active.title,
                "activated_at": active.activated_at.isoformat() if active.activated_at else None,
                "activated_by": active.activated_by,
                "report": await loader.edition_report(session, active),
            }
            if active
            else None
        ),
        "draft": (
            {
                "id": draft.id,
                "created_by": draft.created_by,
                "created_at": draft.created_at.isoformat(),
                "report": await loader.edition_report(session, draft),
            }
            if draft
            else None
        ),
        "groups": [{"id": i, "name": n} for i, n in groups],
        "group_ids": await loader.get_groups(session),
        "customer_group": await get_flag(session, loader.CUSTOMER_GROUP_FLAG, None),
        "part_titles": loader.PARTS,
    }


@router.get("/status")
async def get_status(_: EpozAdmin, session: SessionDep) -> dict[str, Any]:
    return await _status(session)


async def _save_upload(file: UploadFile, settings: Any) -> tuple[Any, int]:
    limit = settings.manual_upload_max_mb * 1024 * 1024
    store = FileStore(settings)
    tmp = store.tmp_path()
    size = 0
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1 << 16):
                size += len(chunk)
                if size > limit:
                    raise _err(413, "too_large", f"Файл больше {settings.manual_upload_max_mb} МБ")
                out.write(chunk)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    if size == 0:
        tmp.unlink(missing_ok=True)
        raise _err(422, "empty", "Файл пустой")
    return tmp, size


@router.post("/draft/files")
async def upload_files(
    principal: EpozAdmin,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
    files: Annotated[list[UploadFile], File(description="Word-файлы Положения и приложений")],
    part: Annotated[str | None, Form()] = None,
) -> dict[str, Any]:
    """Загрузка одного или нескольких файлов в черновик. Часть комплекта определяется по имени
    файла («Приложение 5_…» → app5); для одного файла её можно указать явно (`part`).
    «Перечень изменений» не загружается."""
    by = _who(principal)
    draft = await loader.create_draft(session, by)
    await session.commit()
    results: list[dict[str, Any]] = []
    for f in files:
        name = f.filename or "file.docx"
        key = part if (part and len(files) == 1) else loader.detect_part(name)
        item: dict[str, Any] = {"file": name, "part": key if key != loader.IGNORED else None}
        if key == loader.IGNORED:
            item.update(
                ok=False,
                message="Перечень изменений не загружается: Положение уже в новой редакции",
            )
            results.append(item)
            continue
        if key is None:
            item.update(ok=False, message="Не удалось определить часть комплекта по имени файла")
            results.append(item)
            continue
        try:
            tmp, _size = await _save_upload(f, settings)
        except HTTPException as exc:
            item.update(ok=False, message=exc.detail["message"])
            results.append(item)
            continue
        try:
            draft = await loader.get_draft(session) or draft
            p = await loader.add_part_file(session, settings, draft, key, tmp, name, by)
            await write_audit(
                session,
                "epoz.upload_part",
                actor=principal.user,
                target_type="epoz_edition",
                target_id=draft.id,
                details={"part": key, "file": name},
                ip=client_ip(request),
            )
            await session.commit()
            item.update(
                ok=True,
                status=(p.report or {}).get("status"),
                messages=(p.report or {}).get("messages", []),
            )
        except loader.LoaderError as exc:
            await session.rollback()
            item.update(ok=False, message=exc.message)
        finally:
            tmp.unlink(missing_ok=True)
        results.append(item)
    return {"results": results, **await _status(session)}


@router.delete("/draft", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_draft(principal: EpozAdmin, session: SessionDep, request: Request) -> None:
    if not await loader.cancel_draft(session):
        raise _err(404, "no_draft", "Черновика нет")
    await write_audit(
        session,
        "epoz.cancel_draft",
        actor=principal.user,
        target_type="epoz_edition",
        ip=client_ip(request),
    )
    await session.commit()


class ActivateIn(BaseModel):
    force: bool = False


@router.post("/draft/activate")
async def activate_draft(
    body: ActivateIn, principal: EpozAdmin, session: SessionDep, request: Request
) -> dict[str, Any]:
    draft = await loader.get_draft(session)
    if draft is None:
        raise _err(404, "no_draft", "Черновика нет")
    try:
        await loader.activate(
            session,
            draft,
            _who(principal),
            force=body.force,
            ip=client_ip(request),
            actor=principal.user,
        )
    except loader.LoaderError as exc:
        raise _err(409, exc.code, exc.message) from None
    await session.commit()
    return await _status(session)


class GroupsIn(BaseModel):
    group_ids: list[int] = Field(default_factory=list, max_length=100)


@router.put("/groups")
async def set_groups(
    body: GroupsIn, principal: EpozAdmin, session: SessionDep, request: Request
) -> dict[str, Any]:
    """Группы, которым открыт весь действующий комплект ЕПоЗ (одним действием на 15 документов)."""
    try:
        ids = await loader.set_groups(session, body.group_ids, _who(principal))
    except loader.LoaderError as exc:
        raise _err(422, exc.code, exc.message) from None
    await write_audit(
        session,
        "epoz.set_groups",
        actor=principal.user,
        target_type="epoz",
        details={"group_ids": ids},
        ip=client_ip(request),
    )
    await session.commit()
    return await _status(session)


class SettingsIn(BaseModel):
    customer_group: str | None = Field(default=None, pattern="^(I|II)$")


@router.put("/settings")
async def set_settings(
    body: SettingsIn, principal: EpozAdmin, session: SessionDep, request: Request
) -> dict[str, Any]:
    """Группа заказчика организации (I или II): от неё зависят, например, сроки оплаты."""
    await set_flag(session, loader.CUSTOMER_GROUP_FLAG, body.customer_group, _who(principal))
    await write_audit(
        session,
        "epoz.set_customer_group",
        actor=principal.user,
        target_type="epoz",
        details={"customer_group": body.customer_group},
        ip=client_ip(request),
    )
    await session.commit()
    return await _status(session)


@router.get("/clauses")
async def find_clause(
    _: EpozAdmin,
    session: SessionDep,
    part: Annotated[str, Query(description="main, app1 … app14")],
    label: Annotated[str, Query(min_length=1, max_length=64, description="например, 10.3 или 6.6")],
    draft: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Пункт и его подпункты по номеру: так администратор проверяет, что нумерация верна."""
    edition: EpozEdition | None = await (
        loader.get_draft(session) if draft else loader.get_active(session)
    )
    if edition is None:
        raise _err(404, "no_edition", "Редакции нет")
    rows = await loader.get_clauses(session, part, label.strip(), edition_id=edition.id)
    return {
        "part": part,
        "label": label,
        "found": bool(rows),
        "clauses": [
            {"label": c.label, "level": c.level, "is_heading": c.is_heading, "text": c.text}
            for c in rows[:200]
        ],
        "truncated": len(rows) > 200,
    }


# ---------------------------------------------------------------------------
# Справочник ОКПД2
# ---------------------------------------------------------------------------

okpd_router = APIRouter(prefix="/api/admin/okpd2", tags=["epoz"])


@okpd_router.get("")
async def okpd2_status(_: EpozAdmin, session: SessionDep) -> dict[str, Any]:
    ed = await okpd2.active_edition(session)
    return {"active": okpd2.describe(ed) if ed else None}


@okpd_router.post("/import")
async def okpd2_import(
    principal: EpozAdmin,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
    file: Annotated[UploadFile, File(description="Excel (.xlsx) со справочником ОКПД2")],
) -> dict[str, Any]:
    """Загрузка справочника из Excel: вся редакция одной транзакцией или ничего."""
    tmp, _size = await _save_upload(file, settings)
    try:
        try:
            report = await okpd2.import_okpd2(session, tmp, _who(principal))
        except okpd2.Okpd2Error as exc:
            await session.rollback()
            raise _err(422, exc.code, exc.message) from None
        await write_audit(
            session,
            "okpd2.import",
            actor=principal.user,
            target_type="okpd2_edition",
            target_id=report["edition_id"],
            details={
                "records": report["records"],
                "status": report["status"],
                "file": file.filename,
            },
            ip=client_ip(request),
        )
        await session.commit()
    finally:
        tmp.unlink(missing_ok=True)
    return report


@okpd_router.get("/lookup")
async def okpd2_lookup(
    _: EpozAdmin,
    session: SessionDep,
    code: Annotated[str | None, Query(max_length=32)] = None,
    q: Annotated[str | None, Query(max_length=300)] = None,
) -> dict[str, Any]:
    """Код с родителями («что означает каждый уровень») или поиск кодов по словам названия."""
    if code:
        path = await okpd2.chain(session, code)
        return {
            "found": bool(path),
            "chain": [{"code": r.code, "name": r.name, "level": r.level} for r in path],
        }
    if q:
        hits = await okpd2.search(session, q, limit=15)
        return {
            "found": bool(hits),
            "hits": [{"code": o.code, "name": o.name, "score": round(r, 3)} for o, r in hits],
        }
    raise _err(422, "empty_query", "Укажите код или слова названия")
