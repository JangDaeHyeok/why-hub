# 지식 허브 이미지 — 관리 서버(web/HTTP·UI)와 MCP 서버 공용.
FROM python:3.11-slim

WORKDIR /app

# 의존성 먼저(레이어 캐시).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 앱 소스.
COPY hub ./hub
COPY web ./web
COPY templates ./templates
COPY scripts ./scripts
COPY config.example.toml ./

# 배포 기본: 컨테이너는 0.0.0.0 바인드. 실제 config 는 KNOWLEDGE_HUB_CONFIG 로 주입.
# PYTHONPATH 로 `python scripts/import_to_postgres.py` 에서 hub 를 import 가능하게.
ENV HOST=0.0.0.0 PORT=8000 PYTHONPATH=/app

# 기본 커맨드는 관리 서버(web). MCP 서버는 compose 에서 command 로 오버라이드.
CMD ["python", "-m", "hub.interfaces.web"]
