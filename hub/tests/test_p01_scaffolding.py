"""P01 검증 — 스캐폴딩 & 기반.

수용 기준 (구현스펙-M1-M4-Phase.md P01):
- 패키지 import 성공
- paths 가 올바른 경로 반환
- config 파일 로드
- pytest 실행됨 (이 파일이 도는 것 자체로 충족)
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from hub import config as config_mod
from hub.config import Config, LLMConfig
from hub.models import (
    CHANGE_TYPES,
    DOC_STATUSES,
    DOC_TYPES,
    Anchor,
    DiffHunk,
    Document,
    HistoryEntry,
    Hit,
    SaveResult,
)
from hub.store import paths


# ── 패키지 import 성공 ────────────────────────────────────────────────
STORE_MODULES = [
    "normalize", "lint", "anchors", "diffing", "history", "snapshots",
    "index_fts", "locking", "journal", "save", "reconcile", "paths",
]


@pytest.mark.parametrize("name", STORE_MODULES)
def test_store_modules_import(name):
    mod = importlib.import_module(f"hub.store.{name}")
    assert mod is not None


def test_top_level_modules_import():
    for name in ("hub", "hub.models", "hub.config", "hub.store"):
        assert importlib.import_module(name) is not None


# ── 데이터 모델은 순수 값 객체 ────────────────────────────────────────
def test_document_model():
    d = Document(id="adr-0001", type="adr", title="t", status="accepted",
                 created="2026-07-14")
    assert d.tags == [] and d.related == [] and d.supersedes is None


def test_all_models_constructible():
    Anchor(level=2, text="결정", slug="결정", path="결정", occurrence=1,
           line_range=(0, 3))
    DiffHunk(anchor="결정", added=["+ a"], removed=["- b"])
    HistoryEntry(ts="2026-07-14T10:00:00", actor="alice", type="revision",
                 anchor="결정", summary="s")
    SaveResult(id="adr-0001", change_type="created")
    Hit(doc_id="adr-0001", anchor="결정", text="...", score=1.0)


def test_enums_present():
    assert "adr" in DOC_TYPES
    assert "accepted" in DOC_STATUSES
    assert set(CHANGE_TYPES) == {
        "created", "revision", "deprecation", "supersede", "ingest"
    }


# ── paths: 올바른 경로 반환, I/O 없음 ─────────────────────────────────
def test_paths_layout():
    base = "/tmp/kh"
    assert paths.doc_path(base, "adr-0007", "adr") == Path(
        "/tmp/kh/docs/adr/adr-0007.md")
    assert paths.snapshot_path(base, "adr-0007") == Path(
        "/tmp/kh/.snapshots/adr-0007.md")
    assert paths.snapshot_hash_path(base, "adr-0007") == Path(
        "/tmp/kh/.snapshots/adr-0007.sha256")
    assert paths.docs_diff_path(base, "adr-0007", "2026-07-14") == Path(
        "/tmp/kh/docs-diff/adr-0007.2026-07-14.md")
    assert paths.history_path(base, "adr-0007") == Path(
        "/tmp/kh/history/adr-0007.history.md")
    assert paths.index_path(base) == Path("/tmp/kh/index.sqlite")
    assert paths.lock_path(base, "adr-0007") == Path(
        "/tmp/kh/.locks/adr-0007.lock")
    assert paths.journal_path(base, "adr-0007") == Path(
        "/tmp/kh/.journal/adr-0007.json")


def test_paths_no_io(tmp_path):
    # 경로 함수 호출은 디렉토리를 만들지 않는다 (I/O 없음).
    paths.doc_path(tmp_path, "adr-0001", "adr")
    paths.all_dirs(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_all_dirs_count():
    assert len(paths.all_dirs("/tmp/kh")) == 6


# ── config: 기본값 + 파일 로드 ────────────────────────────────────────
def test_config_defaults():
    cfg = Config()
    assert cfg.repo_root == Path("knowledge")
    assert cfg.lock_timeout == 10.0
    assert cfg.id_pattern("adr") == r"^[a-z]+-[0-9]{4}$"
    # ADR 필수 섹션 (CLAUDE.md §2-5)
    assert cfg.adr_required_sections == ("배경", "결정", "근거", "대안", "결과")


def test_config_load_missing_returns_default():
    assert Config.load(None).repo_root == Path("knowledge")
    assert Config.load("/nonexistent/xyz.toml").repo_root == Path("knowledge")


def test_config_load_from_file(tmp_path):
    p = tmp_path / "cfg.toml"
    p.write_text(
        'repo_root = "/data/kh"\n'
        "lock_timeout = 3.5\n"
        "[id_patterns]\n"
        'adr = "^adr-[0-9]{4}$"\n'
        "[llm]\n"
        'complete_url = "http://localhost:9000/complete"\n'
        'stream_url = "http://localhost:9000/stream"\n'
        'effort = "low"\n'
        "max_tokens = 1024\n",
        encoding="utf-8",
    )
    cfg = Config.load(p)
    assert cfg.repo_root == Path("/data/kh")
    assert cfg.lock_timeout == 3.5
    assert cfg.id_pattern("adr") == r"^adr-[0-9]{4}$"
    # 미정의 타입은 기본 패턴 폴백
    assert cfg.id_pattern("guide") == r"^[a-z]+-[0-9]{4}$"
    assert isinstance(cfg.llm, LLMConfig)
    assert cfg.llm.complete_url == "http://localhost:9000/complete"
    assert cfg.llm.stream_url == "http://localhost:9000/stream"
    assert cfg.llm.effort == "low"
    assert cfg.llm.max_tokens == 1024


def test_example_config_loads():
    # 리포에 커밋된 예시 설정이 실제로 파싱되는지 (문서-코드 드리프트 방지).
    example = Path(config_mod.__file__).resolve().parents[1] / "config.example.toml"
    cfg = Config.load(example)
    assert cfg.repo_root == Path("knowledge")
    # LLM 엔드포인트 URL 은 시크릿성이라 커밋 config 에 없다(env 로 주입) — 튜닝값만 존재.
    assert cfg.llm.complete_url is None and cfg.llm.stream_url is None
    assert cfg.llm.effort in ("low", "high") and cfg.llm.max_tokens > 0


def test_load_default_layers_llm_urls_from_env(tmp_path, monkeypatch):
    # 엔드포인트 URL 은 환경변수로 주입 — load_default 가 파일값 위에 덮어쓴다(env 우선).
    p = tmp_path / "cfg.toml"
    p.write_text("[llm]\neffort = \"high\"\n", encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_HUB_CONFIG", str(p))
    monkeypatch.setenv("KNOWLEDGE_HUB_LLM_COMPLETE_URL", "https://c.example/")
    monkeypatch.setenv("KNOWLEDGE_HUB_LLM_STREAM_URL", "https://s.example/")
    cfg = Config.load_default()
    assert cfg.llm.complete_url == "https://c.example/"
    assert cfg.llm.stream_url == "https://s.example/"


def test_load_default_no_env_leaves_urls_unset(tmp_path, monkeypatch):
    # env 미설정 + 파일에 URL 없음 → 미구성(graceful skip 대상).
    p = tmp_path / "cfg.toml"
    p.write_text("[llm]\neffort = \"low\"\n", encoding="utf-8")
    monkeypatch.setenv("KNOWLEDGE_HUB_CONFIG", str(p))
    monkeypatch.delenv("KNOWLEDGE_HUB_LLM_COMPLETE_URL", raising=False)
    monkeypatch.delenv("KNOWLEDGE_HUB_LLM_STREAM_URL", raising=False)
    cfg = Config.load_default()
    assert cfg.llm.complete_url is None and cfg.llm.stream_url is None
