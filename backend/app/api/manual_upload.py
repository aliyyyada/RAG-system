"""Ручная загрузка документа через интерфейс (для отладки), право documents.upload_manual.

Документ попадает в коллекцию «Ручные загрузки». Файл проверяется по содержимому
(PDF или DOCX, не больше MANUAL_UPLOAD_MAX_MB), сохраняется на диск под SHA-256 и
ставится в очередь с наивысшим приоритетом — через несколько секунд он «готов».
Признак «общий» ставит загрузивший. Загрузивший сразу получает доступ к документу, чтобы
проверить разбор; кому ещё он открыт, администратор задаёт в карточке документа (этап 3).
"""

import hashlib
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from sqlalchemy import select

from app.auth import permissions as perms
from app.auth.deps import SessionDep, SettingsDep, client_ip, require_permission
from app.auth.rbac import Principal
from app.auth.service import write_audit
from app.ingest.files import MIME, detect_type
from app.ingest.pipeline import PRIORITY_MANUAL, add_event, enqueue_document
from app.ingest.storage import FileStore
from app.models import Collection, Document, DocumentFile, DocumentGrant

router = APIRouter(prefix="/api/documents", tags=["документы"])

Uploader = Annotated[Principal, Depends(require_permission(perms.DOCUMENTS_UPLOAD_MANUAL))]


def _err(code: int, key: str, message: str) -> HTTPException:
    return HTTPException(status_code=code, detail={"code": key, "message": message})


@router.post("/manual", status_code=status.HTTP_201_CREATED)
async def upload_manual(
    principal: Uploader,
    session: SessionDep,
    settings: SettingsDep,
    request: Request,
    file: Annotated[UploadFile, File(description="PDF или DOCX")],
    title: Annotated[str, Form(min_length=1, max_length=1000)],
    number: Annotated[str | None, Form(max_length=100)] = None,
    doc_type: Annotated[str | None, Form(max_length=100)] = None,
    is_common: Annotated[bool, Form()] = False,
) -> dict[str, int]:
    limit = settings.manual_upload_max_mb * 1024 * 1024
    store = FileStore(settings)
    tmp = store.tmp_path()
    size = 0
    digest = hashlib.sha256()
    try:
        with tmp.open("wb") as out:
            while chunk := await file.read(1 << 16):
                size += len(chunk)
                if size > limit:
                    raise _err(413, "too_large", f"Файл больше {settings.manual_upload_max_mb} МБ")
                digest.update(chunk)
                out.write(chunk)
        if size == 0:
            raise _err(422, "empty", "Файл пустой")
        kind = detect_type(tmp)
        if kind is None:
            raise _err(422, "bad_type", "Поддерживаются только PDF и DOCX")
        sha = digest.hexdigest()
        store.commit(tmp, sha)
    finally:
        tmp.unlink(missing_ok=True)

    coll = await session.scalar(select(Collection).where(Collection.kind == "manual"))
    if coll is None:
        coll = Collection(name="Ручные загрузки", kind="manual")
        session.add(coll)
        await session.flush()
    name = (file.filename or f"document.{kind}").replace("/", "_").replace("\\", "_")[:500]
    doc = Document(
        collection_id=coll.id,
        title=title.strip(),
        number=(number or "").strip() or None,
        doc_type=(doc_type or "").strip() or None,
        is_common=is_common,
        status="queued",
    )
    session.add(doc)
    await session.flush()
    session.add(DocumentFile(document_id=doc.id, name=name, mime=MIME[kind], size=size, sha256=sha))
    if principal.user is not None:
        session.add(DocumentGrant(document_id=doc.id, user_id=principal.user.id))
    actor = principal.user.personnel_number if principal.user else None
    await add_event(session, doc.id, "uploaded", actor=actor, file=name, size=size)
    await enqueue_document(session, doc.id, settings, PRIORITY_MANUAL)
    await write_audit(
        session,
        "document.upload_manual",
        actor=principal.user,
        target_type="document",
        target_id=doc.id,
        details={"title": doc.title, "file": name, "size": size, "is_common": is_common},
        ip=client_ip(request),
    )
    await session.commit()
    return {"id": doc.id}
