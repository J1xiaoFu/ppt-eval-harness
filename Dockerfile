FROM node:22-alpine AS ui-builder

WORKDIR /ui
RUN corepack enable
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/index.html ui/tsconfig.json ui/vite.config.ts ./
COPY ui/src ./src
RUN pnpm build

FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PPT_EVAL_DATA_DIR=/var/lib/ppt-eval

RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' \
    /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=5 update \
    && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    libreoffice-impress poppler-utils fonts-noto-cjk git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
COPY --from=ui-builder /ui/dist /app/ui/dist
RUN pip install --no-cache-dir ".[api]"

EXPOSE 8000
CMD ["uvicorn", "ppt_eval.api:app", "--host", "0.0.0.0", "--port", "8000"]

