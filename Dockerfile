FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# curl は HEALTHCHECK で /health を叩くために必要
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# マイグレーション実行用（DBのスキーマ変更は alembic upgrade head で行う。
# Base.metadata.create_all() は既存テーブルの定義を更新しないため必須）
RUN pip install --no-cache-dir "alembic>=1.13.0"

COPY . .

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8501

# /health は JSON を返す実APIエンドポイント（/ は SPA の HTML を返すため使用しない）
HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8501/health || exit 1

CMD ["uvicorn", "retro_radio.server:app", "--host", "0.0.0.0", "--port", "8501"]
