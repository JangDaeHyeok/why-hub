"""승인 대기 제출 큐 store — .submissions/<sub_id>.json (구현스펙-승인워크플로우.md).

**지식 콘텐츠가 아니다.** 승인 전 임시 워크플로우 메타이므로 knowledge store(delta 이력)와
분리된 별도 store 다(CLAUDE.md §2-3). 순수 큐 store — 정규화/lint/인덱스를 모른다.
쓰기는 temp+rename 로 원자적(부분 파일 노출 방지). 상태 전이는 호출측이 제출 잠금으로 직렬화한다.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

from . import paths

# 상태 전이(approve/reject)를 직렬화하는 sentinel 락 id (문서 id 와 충돌하지 않음).
LOCK_ID = "__submissions__"

STATUSES = ("pending", "approved", "rejected")


def new_id(now: str) -> str:
    """`sub-<YYYYMMDDHHMMSS>-<6hex>`. now(ISO)에서 날짜부, uuid 로 유일성."""
    digits = "".join(c for c in now if c.isdigit())[:14]
    return f"sub-{digits}-{uuid.uuid4().hex[:6]}"


def create(
    root,
    *,
    op: str,
    doc_id: str,
    raw_markdown: str,
    intended_diff: str | None,
    actor: str,
    prelint: dict,
    now: str,
    change_type: str | None = None,
    project: str | None = None,
    base_hash: str | None = None,
) -> dict:
    """새 제출을 pending 으로 큐에 넣는다. 반환: 제출 dict."""
    sub = {
        "id": new_id(now),
        "op": op,
        "doc_id": doc_id,
        "raw_markdown": raw_markdown,
        "intended_diff": intended_diff,
        "change_type": change_type,  # 승인 시 store.save_document 에 그대로 전달(ingest 등 프로버넌스 보존)
        "project": project,  # 어느 프로젝트에 속하는 제출인지(승인함 필터용)
        "base_hash": base_hash,  # 제출 시점 문서 body_hash(없으면 None) — 승인 시 낙관적 충돌 검사
        "actor": actor,
        "status": "pending",
        "prelint": prelint,
        "created": now,
        "reviewer": None,
        "reviewed_at": None,
        "note": None,
    }
    _write(root, sub)
    return sub


def read(root, sub_id: str) -> dict | None:
    p = paths.submission_path(root, sub_id)
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def list(  # noqa: A001 - 큐 조회 관례명
    root, status: str | None = None, *, project: str | None = None
) -> list[dict]:
    """제출 목록(created 역순 — 최신 먼저). status·project 지정 시 필터."""
    d = paths.submissions_dir(root)
    if not d.exists():
        return []
    out: list[dict] = []
    for f in d.glob("sub-*.json"):
        try:
            sub = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if status is not None and sub.get("status") != status:
            continue
        if project is not None and sub.get("project") != project:
            continue
        out.append(sub)
    out.sort(key=lambda s: s.get("created", ""), reverse=True)
    return out


def set_status(
    root, sub_id: str, *, status: str, reviewer: str, note: str | None = None, now: str
) -> dict:
    """상태 전이(approve/reject). 호출측이 LOCK_ID 로 직렬화한다. 없으면 KeyError."""
    sub = read(root, sub_id)
    if sub is None:
        raise KeyError(f"제출 없음: {sub_id}")
    sub["status"] = status
    sub["reviewer"] = reviewer
    sub["reviewed_at"] = now
    sub["note"] = note
    _write(root, sub)
    return sub


def _write(root, sub: dict) -> None:
    p = paths.submission_path(root, sub["id"])
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sub, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)  # 원자적 교체
