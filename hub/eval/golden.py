"""골든 코퍼스 + 골든 질의 (M5 §2).

키워드가 문서별로 구분되도록 통제한 소형 코퍼스. 질의어는 `unicode61` 이 토큰으로 끊는
**독립 토큰**을 쓴다(조사 결합 질의의 한계는 harness/테스트에서 별도로 문서화).
"""

from __future__ import annotations


def _adr(id, title, decision, alt, extra=""):
    return (
        f"---\nid: {id}\ntype: adr\ntitle: {title}\nstatus: accepted\n"
        "created: 2026-06-01\n---\n\n"
        "# 배경\n\n결정이 필요한 맥락.\n\n"
        f"# 결정\n\n{decision}\n\n"
        "# 근거\n\n이 선택의 트레이드오프.\n\n"
        f"# 대안\n\n{alt}\n\n"
        f"# 결과\n\n{extra or '후속 영향이 정리된다.'}\n"
    )


# ── 코퍼스 (키워드 통제) ─────────────────────────────────────────────
CORPUS = [
    _adr("adr-0001", "색인은 FTS5",
         "색인 엔진으로 SQLite FTS5 를 채택하고 BM25 랭킹을 쓴다.",
         "Elasticsearch 는 운영 부담으로 기각.",
         "색인은 index 파일 하나로 관리된다."),
    _adr("adr-0002", "이력은 자체 delta",
         "저장 시 스냅샷 diff 로 자체 delta 를 만든다.",
         "git 이력은 종속성 때문에 기각.",
         "이력 파일에 delta 가 append 된다."),
    _adr("adr-0003", "앵커 규칙",
         "앵커 규칙은 헤더 슬러그로 정하고 유일화한다.",
         "문단 해시 매칭은 취약해 기각.",
         "diff 가 앵커 단위로 분할된다."),
    # 비-adr(필수 섹션 없음) 문서들 — 서로 다른 토픽의 distractor.
    "---\nid: guide-0001\ntype: guide\ntitle: 배포 가이드\nstatus: accepted\ncreated: 2026-06-01\n---\n\n"
    "# 개요\n\n서버 배포 방법을 정리한다.\n\n# 절차\n\nuvicorn 으로 컨테이너에서 구동한다.\n",
    "---\nid: note-0001\ntype: note\ntitle: 캐시 회의 노트\nstatus: accepted\ncreated: 2026-06-01\n---\n\n"
    "# 회의 노트\n\n캐시 계층으로 Redis 를 검토했다. 성능 이슈를 다뤘다.\n",
    "---\nid: ref-0001\ntype: reference\ntitle: 권한 모델 참고\nstatus: accepted\ncreated: 2026-06-01\n---\n\n"
    "# 권한 모델 참고\n\n권한 은 토큰 기반 ACL 로 관리한다.\n",
]

# ── 골든 질의 (query → relevant doc ids) ─────────────────────────────
GOLDEN = [
    {"query": "FTS5", "relevant": ["adr-0001"]},
    {"query": "BM25", "relevant": ["adr-0001"]},
    {"query": "delta", "relevant": ["adr-0002"]},
    {"query": "스냅샷", "relevant": ["adr-0002"]},
    {"query": "앵커", "relevant": ["adr-0003"]},
    {"query": "배포", "relevant": ["guide-0001"]},
    {"query": "Redis", "relevant": ["note-0001"]},
    {"query": "권한", "relevant": ["ref-0001"]},
]

# 조사 결합 질의(한계 문서화용) — unicode61 이 "앵커를"·"배포를" 을 통짜 토큰으로 봐 under-match.
HARD_QUERIES = [
    {"query": "앵커를", "relevant": ["adr-0003"]},
]


def seed_corpus(service, *, actor: str = "eval") -> None:
    """골든 코퍼스를 save 루틴 경유로 임시 store 에 시드한다."""
    for md in CORPUS:
        service.save_document(md, actor=actor, now="2026-07-14T10:00:00")
