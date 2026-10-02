# Goalie gear room: production container image.

# Small official Python image. Pinned to a minor version so builds are repeatable.
FROM python:3.13-slim

# Don't write .pyc files, and print logs straight away (CloudWatch reads stdout later).
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Pick up Debian security fixes released since the base image was published.
# Trivy flagged an OS library (libpcre2) with a fix available that the base image didn't have yet.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

# A normal user with no password and no login shell. The app never runs as root.
RUN groupadd --system app && useradd --system --gid app --no-create-home --shell /usr/sbin/nologin app

# Install packages first, in their own layer, so code changes don't reinstall them.
COPY requirements.txt .
# Then remove pip itself: the running app never installs anything, and pip bundles its own
# copies of urllib3, msgpack and setuptools that Trivy flagged. Less software, fewer holes.
RUN pip install -r requirements.txt \
    && pip uninstall -y pip

# Copy the app. Files belong to root, so the running app can't rewrite its own code.
COPY . .

# The only writable place is /app/data. The app expects gear.db next to app.py and
# photos in /app/uploads, so point both there with links instead of changing app code.
RUN mkdir -p /app/data/uploads \
    && ln -s /app/data/gear.db /app/gear.db \
    && ln -s /app/data/uploads /app/uploads \
    && chown -R app:app /app/data

USER app

# Mount a volume here to keep the database and photos when the container is replaced.
VOLUME ["/app/data"]

EXPOSE 8000

# Lets Docker (and later the load balancer) tell if the app is answering.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/', timeout=4)" || exit 1

# gunicorn is a production web server. 2 workers keeps SQLite writes calm.
# --no-control-socket: gunicorn 26 tries to make an admin socket in the home folder,
# which our user does not have. We do not need it, so it is switched off.
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "2", "--access-logfile", "-", "--no-control-socket", "app:app"]
