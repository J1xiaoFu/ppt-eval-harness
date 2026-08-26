FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PPT_EVAL_DATA_DIR=/var/lib/ppt-eval

RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice-impress fonts-noto-cjk git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir ".[api,worker,storage]"

EXPOSE 8000
CMD ["uvicorn", "ppt_eval.api:app", "--host", "0.0.0.0", "--port", "8000"]

