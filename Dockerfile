# TVBox 自建源 —— Unraid / Docker 镜像 (纯标准库, 无需额外依赖)
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    DATA_DIR=/data \
    PORT=8787 \
    UPDATE_INTERVAL_HOURS=24 \
    RUN_ON_START=1

WORKDIR /app
COPY update.py app.py sources.txt ./

RUN mkdir -p /data
VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
  CMD python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8787/status.json',timeout=5)" || exit 1

CMD ["python3", "app.py"]
