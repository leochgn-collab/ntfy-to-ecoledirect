# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NTFY_ED_DONNEES=/data \
    PORT=8080 \
    TZ=Europe/Paris

# Utilisateur sans privilèges : le service ne tourne jamais en root.
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin app

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY ntfy_ecoledirecte ./ntfy_ecoledirecte

RUN mkdir /data && chown app:app /data && chmod 700 /data
USER app

EXPOSE 8080
VOLUME ["/data"]
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/sante', timeout=4)"]

CMD ["python", "-m", "ntfy_ecoledirecte"]
