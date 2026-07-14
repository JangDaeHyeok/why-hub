"""검색 평가 CLI — 임시 store 에 골든 코퍼스를 시드하고 지표를 리포트한다 (M5).

실행: `python -m scripts.eval_search`
격리된 임시 디렉토리를 쓰므로 실제 knowledge store 를 건드리지 않는다.
"""

from __future__ import annotations

import tempfile

from hub.eval import golden
from hub.eval.harness import evaluate, format_report
from hub.service import KnowledgeService


def main() -> None:
    root = tempfile.mkdtemp(prefix="kh-eval-")
    svc = KnowledgeService(root)
    try:
        golden.seed_corpus(svc)
        result = evaluate(svc, golden.GOLDEN, k=5)
        print("== 골든 질의 ==")
        print(format_report(result))

        hard = evaluate(svc, golden.HARD_QUERIES, k=5)
        print("\n== 조사 결합 질의 (unicode61 한계 — under-match 기대) ==")
        print(format_report(hard))
    finally:
        svc.close()


if __name__ == "__main__":  # pragma: no cover
    main()
