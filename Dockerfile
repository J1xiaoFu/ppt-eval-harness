FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32 AS ui-builder

WORKDIR /ui
RUN corepack enable
COPY ui/package.json ui/pnpm-lock.yaml ./
RUN pnpm install --frozen-lockfile
COPY ui/index.html ui/tsconfig.json ui/vite.config.ts ./
COPY ui/src ./src
RUN pnpm build

FROM python:3.11-slim@sha256:be1575ed968de893bd54f4c56315ff7c4736ce522c1bca08fd521731aafc0d76

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
RUN python -m pip install --no-cache-dir --no-build-isolation \
    --constraint constraints/docker-py311-linux.txt ".[api]" \
    && python -m pip check

EXPOSE 8000
CMD ["uvicorn", "ppt_eval.api:app", "--host", "0.0.0.0", "--port", "8000"]

