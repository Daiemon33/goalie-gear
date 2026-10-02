# Goalie Gear Room

[![Build and security scan](https://github.com/Daiemon33/goalie-gear/actions/workflows/security.yml/badge.svg?branch=main)](https://github.com/Daiemon33/goalie-gear/actions/workflows/security.yml)

A web app that tracks a hockey club's shared goalie equipment: what the club owns, who has it, and what needs repair. I'm using it as the base for a hands-on DevOps project: containerizing it, deploying it to AWS with Terraform, and wrapping it in a secure CI/CD pipeline.

**Stack:** Python, Flask, SQLite, Pillow, gunicorn, Docker. Planned: AWS (ECS Fargate, ALB, S3, RDS), Terraform, GitHub Actions, Trivy, Checkov, CloudWatch.

## What the app does

- Add gear with a name, category, size, condition, notes and a photo.
- Check gear out to a person with a contact phone number, then return it. Flag damaged gear for repair.
- See everything currently checked out, oldest first, and filter the list by category or status.
- Phone numbers are masked (`***-***-1234`) for everyone except a logged-in admin. The masking happens on the server, so the full number never reaches an anonymous browser.

## Architecture

Today it runs as a single container:

```
Browser ──► gunicorn (2 workers) ──► Flask app ──► SQLite file  (/app/data/gear.db)
                                              └──► photo files  (/app/data/uploads)
```

Target architecture on AWS:

```
Browser ──► Application Load Balancer (HTTPS)
               └──► ECS Fargate tasks (this container, non-root)
                        ├──► Managed database (RDS) for gear and checkouts
                        ├──► S3 bucket (private) for photos
                        ├──► Secrets Manager / SSM for SECRET_KEY and ADMIN_PASSWORD
                        └──► CloudWatch Logs, metrics and alarms
```

The key lesson of the move: a container's disk is temporary. When ECS replaces a task, anything written inside it is gone, so the database and photos have to move to managed services.

## DevOps roadmap

| Step | Status |
|---|---|
| Containerize with Docker (non-root user, gunicorn, health check) | Done |
| Public repo with secrets and data kept out by `.gitignore` | Done |
| Terraform: VPC, ECS Fargate, ALB, S3, managed database | Planned |
| Security scans in GitHub Actions: Trivy (image and dependencies), Checkov (Dockerfile, workflows, secrets, Terraform) | Done |
| GitHub Actions deploy to AWS with OIDC (no stored AWS keys) | Planned |
| CloudWatch logs, metrics and alarms | Planned |
| Map security controls to NIST SP 800-53 | Planned |

## Run it locally

You need Python 3.10 or newer.

Windows (PowerShell):

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

Mac or Linux:

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:5000. The database (`gear.db`) and the `uploads` folder are created on first run.

## Run it in Docker

```bash
docker build -t goalie-gear .
docker run -d --name goalie-gear -p 8000:8000 \
  -e SECRET_KEY="a-long-random-string" \
  -e ADMIN_PASSWORD="choose-a-password" \
  -v goalie-gear-data:/app/data \
  goalie-gear
```

Open http://localhost:8000. The named volume `goalie-gear-data` keeps the database and photos when the container is replaced.

### Environment variables

| Variable | Purpose | If not set |
|---|---|---|
| `SECRET_KEY` | Signs the session cookie | Falls back to a dev-only value, and admin login is refused |
| `ADMIN_PASSWORD` | Password for the admin login | Nobody can log in, and phone numbers stay masked for everyone |

Secrets are never written in the code or the image. Locally they come from `-e` flags; on AWS they will come from Secrets Manager.

## Security decisions

- **Runs as a non-root user** with no home folder and no login shell. App code is owned by root, so the running app can't rewrite itself. The only writable path is `/app/data`.
- **Small, pinned base image** (`python:3.13-slim`) and pinned package versions, so builds are repeatable and scan results are meaningful.
- **`.dockerignore` and `.gitignore`** keep the database, photos, virtual environment and `.env` files out of both the image and this repository.
- **Uploads are never trusted.** Every photo is re-opened and re-saved as a fresh JPEG with a random name, which strips hidden content and avoids user-controlled file names. Uploads are capped at 16 MB.
- **SQL injection prevented** by using parameterized queries everywhere.
- **Admin login hardening:** constant-time password comparison, an 8-hour session limit, and a session tied to a fingerprint of the current password (see below).
- **Privacy by default:** phone numbers are masked server-side, and the app asks for a first name or player number, since many club members are minors.
- **Every push is scanned** (see below). The image ships without `pip` and with current Debian security fixes.
- **Known gap:** editing gear doesn't require a login yet. The admin login currently protects phone numbers only. Protecting write actions is on the list before a public deployment.

## CI security scanning

[`.github/workflows/security.yml`](.github/workflows/security.yml) runs on every push and pull request to `main`:

| Job | What it does | Fails the run when |
|---|---|---|
| Trivy | Builds the image, then scans its OS packages, Python packages, secrets and image config | A HIGH or CRITICAL problem has a fix available |
| Checkov | Scans the Dockerfile, the workflow files, committed secrets, and Terraform (once added) | Any check fails |

All findings, at every severity, are uploaded to the repo's **Security → Code scanning** tab, so lower-severity issues are visible without blocking work.

How the pipeline itself is hardened:
- **Least privilege:** the workflow can only read the code. Each scan job adds just `security-events: write` to upload results. No secrets are used.
- **Actions pinned to commit hashes**, not tags. A tag can be re-pointed at malicious code (this happened to real, popular actions); a hash can't.
- `persist-credentials: false` so the GitHub token isn't left on disk for later steps.

**Accepted risk: unfixed OS vulnerabilities.** The Debian base image still reports HIGH vulnerabilities that Debian has not released a fix for. There's nothing to upgrade to, so the gate ignores them (`ignore-unfixed`) rather than blocking every build. They stay visible in the Security tab, and the `apt-get upgrade` step picks up fixes as soon as Debian publishes them. Reviewing a slimmer base image (for example a distroless image) is a future step.

## What broke and how I fixed it

**1. The container crashed on start because of gunicorn's control socket.**
gunicorn 26 tries to create an admin socket in the user's home folder. For security, the container's user has no home folder, so gunicorn failed at startup. I didn't need that socket, so I turned it off with `--no-control-socket` instead of weakening the user account.

**2. Two gunicorn workers raced each other on a database change.**
When I added phone numbers, the app adds the new columns to an existing database with `ALTER TABLE` at startup. With two workers starting at the same moment, both checked "is the column missing?", both saw yes, and the second one crashed with "duplicate column". The fix was to treat "duplicate column" as success, since it means the other worker already did the job. The real-world lesson: anything that runs at startup must be safe to run twice at the same time, because you'll have more than one copy running.

**3. Changing the admin password didn't log anyone out.**
The login cookie only said "this person is admin", so an old cookie stayed valid after the password changed. I changed it to store a one-way fingerprint (HMAC) of the current password instead. When the password is rotated, the fingerprint no longer matches and every old session stops working. Sessions also expire after 8 hours. This matters for credential rotation: rotating a secret should actually cut off whoever had the old one.

**4. Trivy flagged libraries I never installed.**
The first scan found HIGH vulnerabilities in `urllib3`, `msgpack` and `setuptools`, which aren't in `requirements.txt`. Tracing them showed they were copies bundled *inside pip*, the package installer. The app never installs anything at runtime, so I uninstall pip after installing the app's packages. That removed all four Python findings and shrank the image. A fifth finding was in an OS library (`libpcre2`) where Debian had a fix the base image didn't have yet, so the build now runs `apt-get upgrade`. Lesson: scan the image you actually ship, not just your requirements file, and remove tools the running app doesn't need.

## Project layout

```
app.py              Routes, database, photo handling (read top to bottom)
templates/          HTML pages (Jinja)
static/style.css    Styling
requirements.txt    Pinned Python packages
Dockerfile          Production container image
.dockerignore       Keeps local and private files out of the image
.github/workflows/  CI pipeline: build, Trivy and Checkov scans
.gitignore          Keeps data, photos and secrets out of GitHub
```
