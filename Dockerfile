# Python 3.12 on Debian slim: every dependency ships as a prebuilt wheel for amd64 and arm64,
# so no system packages or compilers are needed.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --create-home --uid 10001 app
WORKDIR /app

EXPOSE 8501
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health', timeout=4)"
CMD ["streamlit", "run", "app.py", "--server.port=8501", "--server.address=0.0.0.0"]


# Full pipeline (OCR + LLM extraction):  docker build --target full -t invoice2erp:full .
# CPU-only PyTorch keeps the image small and runs on any host.
FROM base AS full
COPY requirements.txt requirements-full.txt ./
RUN pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements-full.txt
COPY --chown=app:app . .
USER app


# Read-only showcase (default target):  docker build -t invoice2erp .
FROM base AS showcase
COPY requirements.txt ./
RUN pip install -r requirements.txt
COPY --chown=app:app . .
USER app
