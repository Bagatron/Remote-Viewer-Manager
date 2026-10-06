FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    MPLBACKEND=Agg \
    MPLCONFIGDIR=/tmp/mpl \
    RV_DATA_DIR=/data

RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin rv \
 && mkdir -p /data && chown rv:rv /data

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"
VOLUME ["/data"]

# Single worker on purpose: analysis runs on an in-process thread pool and the
# default SQLite DB lives on a ReadWriteOnce volume.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
