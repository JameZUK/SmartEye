FROM python:3.13-slim

WORKDIR /app
COPY pyproject.toml README.md ./
COPY smarteye ./smarteye
COPY scripts ./scripts
RUN pip install --no-cache-dir . && useradd --system --uid 1000 smarteye && mkdir /data && chown smarteye /data

USER smarteye
ENV SMARTEYE_DATA=/data PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8090
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8090/healthz')"
CMD ["uvicorn", "smarteye.server:app", "--host", "0.0.0.0", "--port", "8090", "--log-level", "info"]
