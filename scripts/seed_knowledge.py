"""도그푸딩 시드 — 이 프로젝트의 첫 실제 ADR 들을 save 루틴으로 저장한다 (CLAUDE.md §9).

여기 담긴 ADR 원문이 **정본**이다. `seed(root)` 는 이 원문들을 `KnowledgeService.save_document`
경유로 저장한다(파일 직접 쓰기 금지 — 불변식 §2-1). 저장 결과 knowledge store(docs/스냅샷/이력/
인덱스)는 생성물이며, 이 스크립트를 다시 돌리면 재생성된다.

실행: `python -m scripts.seed_knowledge [<root>]`  (기본 root = ./knowledge)
"""

from __future__ import annotations

import sys

from hub.service import KnowledgeService

# ── 정본 ADR 원문 (템플릿 골격: 배경/결정/근거/대안/결과) ─────────────
ADR_FTS5 = """---
id: adr-0001
type: adr
title: 인덱스·검색은 SQLite FTS5 유사 RAG로 간다 (벡터 없음)
status: accepted
created: 2026-07-14
tags: [search, index, rag]
---

# 배경

팀 지식 허브는 Why 중심 검색이 필요하다. 범용 벡터 RAG는 Claude Projects가 이미 상용으로 제공하며,
정면 승부하면 "설정 없이 되는 Projects"에 밀린다. 또 벡터DB·임베딩·GPU는 팀 MVP에 과한 인프라다.

# 결정

검색 인덱스로 SQLite FTS5(BM25 내장, WAL)를 채택하고, 유사 RAG(구조/메타 필터 → 전문검색 →
큐레이션)로 간다. 임베딩·벡터DB·리랭커·GPU는 도입하지 않는다.

# 근거

FTS5는 단일 파일이라 별도 서버가 없고 BM25가 내장돼 있으며 WAL로 동시성을 얻는다. 우리의 차별점은
Why·변경 이력·앵커 단위 출처지 벡터 유사도가 아니므로, 검색은 "좋은 후보만" 내고 큐레이션은
에이전트/사람이 맡는다.

# 대안

- 헤비 벡터 RAG(pgvector + 임베딩 + vLLM 리랭커, v0.7 스택): 인프라 과다 + Projects와 정면충돌로 기각.
- ripgrep 순수 파일 검색: 메타 필터·랭킹이 약해 유사 RAG의 후보 품질을 못 맞춰 보류.
- Elasticsearch: 별도 서버 운영 부담이 MVP에 과해 기각.

# 결과

색인은 index.sqlite 하나로 관리된다. 코퍼스가 커져 후보 품질이 떨어지면 **인터페이스는 그대로 두고**
리트리버 내부만 벡터/하이브리드로 교체한다(기획안2). 필터→검색 순서는 고정한다.
"""

ADR_DELTA = """---
id: adr-0002
type: adr
title: 이력은 git 없이 자체 delta로 남긴다
status: accepted
created: 2026-07-14
tags: [history, storage]
related: [adr-0001]
---

# 배경

결정의 변경 이력을 1급 콘텐츠로, 질의 가능하게 유지해야 한다. git 저장소 이력에 기대면 knowledge store가
git에 종속되고, 배포·권한이 git 경계에 묶이며 앵커 단위 질의가 어렵다.

# 결정

knowledge store의 이력은 저장 시 스냅샷과 diff를 떠서 **자체 delta**로 생성·append한다. knowledge
store에는 git을 쓰지 않는다.

# 근거

이력을 사람·에이전트가 읽는 콘텐츠(앵커별 +/- delta + 규칙 기반 요약)로 남길 수 있고, 저장소 배포가
git에 묶이지 않는다. delta(무엇이 바뀌었나)는 항상 정확하므로 요약이 의심되면 delta가 근거가 된다.

# 대안

- git 기반 이력: knowledge store를 git에 종속시키고 앵커 단위 질의·권한 분리가 어려워 기각.
- 이력 미보관: Why의 변경 추적이 불가능해져 프로젝트의 핵심 가치를 잃으므로 기각.

# 결과

history/<id>.history.md에 append-only로 delta가 쌓인다. 파일+인덱스의 비원자성은 저널 + reconcile로
수렴시킨다(원자성 가정 금지).
"""

ADR_ANCHOR = """---
id: adr-0003
type: adr
title: 앵커는 git diff 방식으로 최근접 헤더에 귀속한다
status: accepted
created: 2026-07-14
tags: [anchor, diff]
related: [adr-0002]
---

# 배경

변경을 섹션 단위로 귀속해 이력·검색 출처를 앵커로 표기해야 한다. 앵커가 불안정하면 이력·검색이
깨지므로 안정적이고 유일한 규칙이 필요하다.

# 결정

줄 단위 diff의 각 hunk를 **감싸는 최근접 상위 헤더**에 귀속시킨다(git diff가 hunk 헤더에 함수/섹션명을
붙이는 방식). 앵커 = 헤더 슬러그 + 유일성 보장(동일 슬러그는 `__2`, `__3` …).

# 근거

git diff의 검증된 원리와 동일해 직관적이고, 헤더 기반이라 안정적이며, 유일화로 이력·검색이 앵커에
안전하게 의존할 수 있다.

# 대안

- 문서 전체 단위 이력: 어느 섹션이 바뀌었는지 상실해 앵커 단위 출처를 못 만들어 기각.
- 문단 해시 매칭: 문단 재정렬·미세 편집에 취약해 앵커 안정성을 못 지켜 보류.

# 결과

diffing이 앵커별로 hunk를 분할해 이력 항목을 앵커 단위로 만들고, 검색도 앵커 단위 섹션으로 결과를
반환한다.
"""

SEED_ADRS = [ADR_FTS5, ADR_DELTA, ADR_ANCHOR]


def seed(root, *, actor: str = "dhjang") -> list:
    """정본 ADR들을 save 루틴 경유로 저장한다. SaveResult 목록 반환."""
    svc = KnowledgeService(root)
    try:
        results = []
        for md in SEED_ADRS:
            results.append(svc.save_document(md, actor=actor))
        return results
    finally:
        svc.close()


if __name__ == "__main__":  # pragma: no cover
    target = sys.argv[1] if len(sys.argv) > 1 else "knowledge"
    for res in seed(target):
        print(f"saved {res.id}: {res.change_type}")
