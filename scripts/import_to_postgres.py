"""파일 백엔드(기존 knowledge/) → PostgresStore 일괄 이관 (1회성).

문서·이력·docs-diff·제출을 그대로 옮긴다. 문서는 reflect 로 재색인(스냅샷 baseline 생성) 후
**원본 이력**을 replace_history 로 복원한다(재베이스라인으로 이력이 유실되지 않게).

사용:
    # 소스(파일)·타깃(postgres) config 를 env 로 지정하거나 인자로.
    KNOWLEDGE_HUB_ROOT=knowledge \\
    KNOWLEDGE_HUB_CONFIG=config.deploy.toml \\
    python scripts/import_to_postgres.py

동작: KNOWLEDGE_HUB_CONFIG(=postgres 백엔드)로 타깃 PostgresStore 를, KNOWLEDGE_HUB_ROOT 의
파일 저장소로 소스 FileStore 를 연다. 멱등(재실행 시 upsert).
"""

from __future__ import annotations

import os
import sys

from hub.config import Config
from hub.store.file_store import FileStore
from hub.store.pg_store import PostgresStore


def main() -> int:
    root = os.environ.get("KNOWLEDGE_HUB_ROOT", "knowledge")
    cfg = Config.load_default()
    if cfg.storage != "postgres":
        print("타깃이 postgres 백엔드가 아닙니다. KNOWLEDGE_HUB_CONFIG 로 backend=postgres 지정 필요.")
        return 2

    src_cfg = Config()  # 소스는 파일 백엔드(기본값)
    src_cfg.default_project = cfg.default_project
    src = FileStore(root, src_cfg)
    dst = PostgresStore(cfg)

    try:
        doc_ids = src.all_doc_ids()
        print(f"문서 {len(doc_ids)}건 이관 시작 (root={root})")
        # 1차: id-only 선삽입 → 이관 중 related/supersedes dangling lint 가 순서·순환에
        # 무관하게 통과하도록 모든 문서 id 를 미리 존재시킨다(2차 reflect 가 전체 컬럼 덮어씀).
        dst.precreate_ids(doc_ids)
        for doc_id in doc_ids:
            raw = src.get_raw(doc_id)
            if raw is None:
                continue
            meta = src.get_meta(doc_id) or {}
            # 문서 반영(스냅샷 baseline + 재색인). actor 는 원제출 이력이 곧 덮어씀.
            dst.reflect(raw, actor="import")
            # 원본 이력 복원(파일 백엔드의 delta 타임라인 보존).
            hist = src.read_history(doc_id)
            if hist:
                dst.replace_history(doc_id, hist)
            # 의도된 변경(docs-diff) 복사.
            for d in src.read_docs_diff(doc_id):
                dst.put_docs_diff(doc_id, d["date"], d["content"])
            print(f"  ✓ {doc_id} (project={meta.get('project')}, history={len(hist)})")

        subs = src.list_submissions()
        print(f"제출 {len(subs)}건 이관 (원본 id·상태 보존, 재실행 멱등)")
        for s in subs:
            dst.import_submission(s)  # 원본 id·status·reviewer 그대로 upsert
        print("이관 완료.")
        return 0
    finally:
        src.close()
        dst.close()


if __name__ == "__main__":
    sys.exit(main())
