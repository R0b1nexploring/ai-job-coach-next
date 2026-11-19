#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p dist
COPYFILE_DISABLE=1 tar -czf dist/ai-job-coach-next-linux.tar.gz \
  --exclude='__pycache__' --exclude='*.pyc' \
  Dockerfile compose.yaml .dockerignore .env.docker.example .env.example \
  pyproject.toml uv.lock .python-version alembic.ini alembic app assets scripts \
  README.md LICENSE CONTENT-NOTICES.md tests .github .gitignore
chmod 644 dist/ai-job-coach-next-linux.tar.gz
echo '已生成 dist/ai-job-coach-next-linux.tar.gz；不含 .env、数据库、私有资料或 Git 历史。'
