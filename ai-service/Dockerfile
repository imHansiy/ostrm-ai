FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr tesseract-ocr-chi-sim ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY aitags ./aitags
RUN pip install --no-cache-dir '.[tidb]'
ENV AI_DATA_DIR=/data MEDIA_EXPORT_DIR=/exports AI_PROXY_ENABLED=false PYTHONUNBUFFERED=1
VOLUME ["/data", "/exports"]
EXPOSE 8787
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/ai/health',timeout=4)"
CMD ["uvicorn", "aitags.app:app", "--host", "0.0.0.0", "--port", "8787", "--workers", "1", "--no-access-log"]
