# ZeroDownPipeline

**A Dockerized Python API deployed through Jenkins to AWS free tier with a blue-green traffic switch, a health-check gate, and automatic rollback — a broken deploy never reaches users, and no human has to notice.**

Behind it sits a replicated MySQL pair with scripted backups to S3 and a scripted failover, because a deployment story that ignores the database is only half a story.

---

## What this actually does

```
git push
   │
   ▼
┌─────────────────────────────────────────────────────────────────┐
│ Jenkins                                                          │
│  checkout → lint → pytest → shell lint → docker build → push     │
└─────────────────────────────────────────────────────────────────┘
   │  image tagged with the git short SHA — never :latest
   ▼
┌─────────────────────────────────────────────────────────────────┐
│ EC2 t3.micro                                                     │
│                                                                  │
│   nginx :80 ──────────────► ┌──────────────┐  blue  :8000       │
│      │  (live traffic)      │ app (live)   │                     │
│      │                      └──────────────┘                     │
│      │                                                           │
│      ╎  switch only after                                        │
│      ╎  the health check passes                                  │
│      ╎                      ┌──────────────┐  green :8001       │
│      └╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌► │ app (new)    │ ◄── health check    │
│                             └──────────────┘     runs here       │
│                                    │                             │
│         ┌──────────────┬───────────┴──────────┐                 │
│         ▼              ▼                      ▼                 │
│   mysql-primary ──► mysql-replica          redis                 │
│   (writes)          (reads, backups)       (cache)               │
└─────────────────────────────────────────────────────────────────┘
   │
   ▼
┌─────────────────────────────────────────────────────────────────┐
│ S3 (versioned)                                                   │
│   records/            audit copy of every link                   │
│   deployments/history.json    every attempt, success or failure  │
│   deployments/last-good.json  written ONLY after a passed check  │
│   backups/            nightly mysqldump, verified before upload  │
└─────────────────────────────────────────────────────────────────┘
```

**The deploy path in one paragraph.** A new version always starts on the colour that is *not* serving traffic. It is health-checked directly on its own port, and the check requires the container to report back the exact git SHA that was just deployed — so a container that failed to start cannot be mistaken for a healthy one. Only if that passes does nginx get reloaded to point at the new port; the reload is graceful, so requests already in flight finish on the old workers before they exit. The old container is then drained and removed, and `last-good.json` in S3 is updated. If the check fails, the new container is destroyed and nothing else happens — the old one was never touched. That is why the rollback is fast: on the live path, it is a no-op.

---

## Results

> Fill these in from your own runs — every number here is measured by a script in this repo, not estimated. `docs/METRICS.md` explains exactly how to reproduce each one.

| Metric | Result | How it was measured |
|---|---|---|
| Manual deploy (baseline) | _e.g. 11 min, 14 steps_ | `docs/METRICS.md` — timed once, by hand |
| Automated deploy, push → live | _e.g. 3 min 40 s_ | Jenkins build duration |
| Failed requests during a deploy | _e.g. 0 of 600_ | `deploy/measure_downtime.py` |
| Time to automatic rollback (MTTR) | _e.g. 38 s_ | `deploy/deploy.py --break-health` |
| Manual steps eliminated | _e.g. 14 → 1_ | counted, before vs after |
| Database failover time | _e.g. 22 s_ | `scripts/db/promote_replica.sh` |

---

## Repository layout

```
app/                     Flask URL shortener — small on purpose
  main.py                endpoints, including the /health the pipeline gates on
  db.py                  MySQL: writes to the primary, /stats reads the replica
  cache.py               Redis, best-effort — a cache outage is not an outage
  s3_store.py            versioned audit copy of each record
  tests/                 16 unit tests, no live dependencies needed

Dockerfile               multi-stage, non-root, HEALTHCHECK, SHA-tagged
docker-compose.data.yml  MySQL primary + replica + Redis, tuned for a 1 GiB box
Jenkinsfile              8 stages, with rollback wired into the failure paths

infra/                   AWS, entirely in boto3 — no console clicks
  provision.py           S3 + IAM role + SG + key pair + EC2, idempotent
  user_data.sh           first-boot: docker, nginx, awscli, swap, backup cron
  teardown.py            destroys it all; guards the data behind --confirm

deploy/                  orchestration, run from Jenkins over SSH
  deploy.py              the blue-green + health-gate + auto-rollback logic
  rollback.py            deliberate rollback to a known-good version from S3
  healthcheck.py         outside-in smoke test through nginx
  measure_downtime.py    proves "zero downtime" with a request log
  sync_scripts.py        ships the bash layer and .env to the instance
  nginx/app.conf.template

scripts/                 everything that runs ON the instance, in bash
  local_stack.sh         run the whole stack on your laptop: up | test | down
  lib.sh                 shared helpers; active_color() reads nginx, not a file
  start_container.sh     docker run into a colour slot
  stop_container.sh      drain and remove, with a grace period
  healthcheck.sh         the retry loop the whole pipeline turns on
  switch_traffic.sh      render nginx conf, validate, graceful reload
  db/bootstrap_db.sh     bring up the data tier (run once)
  db/setup_replication.sh   GTID replication, idempotent
  db/replication_status.sh  health, exits non-zero when broken
  db/backup_db.sh        dump from the replica, verify, upload to S3
  db/restore_db.sh       restore from S3, --confirm required
  db/promote_replica.sh  failover, and repoint the app at the new primary

docs/
  AWS-SETUP.md           start here if you have never used AWS
  JENKINS-SETUP.md       installing Jenkins and wiring the credentials
  RUNBOOK.md             day-2 operations: backup, restore, failover, rollback
  METRICS.md             how to measure every number in the table above
```

---

## Quick start

Full detail is in [docs/AWS-SETUP.md](docs/AWS-SETUP.md). The short version:

```bash
# 0. prerequisites: Docker, Python 3.10+, AWS CLI, an AWS account, a Docker Hub account
cp .env.example .env          # then edit it — S3_BUCKET must be globally unique
pip install boto3

# 1. create all AWS infrastructure (~4 minutes, mostly waiting on the instance)
python infra/provision.py

# 2. ship the bash layer and secrets to the box
python deploy/sync_scripts.py

# 3. bring up MySQL primary + replica + Redis (once)
ssh -i zerodownpipeline-key.pem ec2-user@<ip> /opt/zerodown/scripts/db/bootstrap_db.sh

# 4. build and push an image, then deploy it
docker build --build-arg APP_VERSION=$(git rev-parse --short HEAD) \
  -t <dockerhub-user>/zerodownpipeline-api:$(git rev-parse --short HEAD) .
docker push <dockerhub-user>/zerodownpipeline-api:$(git rev-parse --short HEAD)
python deploy/deploy.py --tag $(git rev-parse --short HEAD)

# 5. prove it works
curl http://<ip>/health
python deploy/healthcheck.py
```

**Windows note:** SSH refuses a private key that other accounts can read. After `provision.py` writes the `.pem`:

```powershell
$me = "$env:USERDOMAIN\$env:USERNAME"
icacls .\zerodownpipeline-key.pem /inheritance:r /grant:r "${me}:R"
```

---

## The two demos that make this project worth showing

### 1. Zero downtime, measured

```bash
# terminal 1 — hammer the live endpoint for two minutes
python deploy/measure_downtime.py --duration 120 --rps 5 --out docs/downtime-run.json

# terminal 2 — deploy a new version while that is running
python deploy/deploy.py --tag <new-sha>
```

The probe prints a `SWITCHED` line the moment the `X-Served-By` header flips from `blue` to `green`, and finishes with a failure count. Zero failures across a live traffic switch is the claim, and `docs/downtime-run.json` is the evidence.

### 2. Automatic rollback, on a build that is genuinely broken

```bash
python deploy/deploy.py --tag <sha> --break-health
```

`BREAK_HEALTHCHECK=1` makes `/health` return 500, so this is a real failing deploy, not a simulation of one. What you should see: the container starts on the inactive colour, the health check retries and gives up, the container is destroyed, nginx is never touched, and the whole thing is recorded as `"status": "failed"` in `deployments/history.json`. Traffic never moved. Run `curl http://<ip>/health` during it — the old version answers throughout.

In Jenkins, tick the **ROLLBACK_DRILL** parameter to do the same thing through the pipeline. The build ends UNSTABLE, which is the correct outcome: the deploy failed *and* the recovery worked.

---

## Design decisions worth defending

**Why nginx and a script instead of a load balancer?** The AWS free tier has no ALB. Rebuilding the guarantee by hand — graceful reload, health gate, drain period — means understanding what a managed service is actually doing for you, not just which checkbox turns it on. In production this would be an ALB with two target groups.

**Why deploy by git SHA and never `:latest`?** Rollback has to be unambiguous. `:latest` means "whatever was pushed most recently", which is exactly the wrong thing to point at when you are trying to get back to a version that worked.

**Why does the health check verify the version, not just the status?** Because the most confusing failure in blue-green is a new container that dies on startup while the old one still holds the port. A 200 from the *previous* version would promote a build that never ran. Matching the SHA closes that hole.

**Why is `active_color` read from the nginx config rather than a state file?** A state file records what you intended. The nginx config is what is actually routing packets. When those two disagree, only one of them is right.

**Why MySQL primary + replica on a single instance?** It is honestly one host, so this is not high availability against hardware failure. What it does give is real replication: a read path served by the replica (`/stats`), backups taken off the replica so they do not contend with writes, and a promotion script that has been run and timed. The mechanics — GTID positioning, `super_read_only`, draining the relay log before promoting, remembering that the app user must exist on the replica too — are identical at any scale.

**Why take backups from the replica?** Backups are a classic source of lock contention. `--single-transaction` avoids table locks, and taking the dump off the replica keeps even that cost away from the write path.

**Why is `s3_store` best-effort?** S3 holds an audit copy, not the source of truth. If S3 is unavailable, users should still be able to shorten a URL. Deciding in advance which dependencies are allowed to take the service down is most of what "reliability engineering" means here.

**What would change for production?** RDS with Multi-AZ instead of self-managed MySQL; an ALB with two target groups instead of nginx; an autoscaling group instead of one instance; Secrets Manager instead of a `.env` on the box; canary or percentage-based traffic shifting instead of an all-at-once switch; and the health gate driven by real error-rate metrics, not just an endpoint.

---

## Run it locally first — no AWS, no cost

The whole stack runs on one machine. Do this before touching AWS: the expensive place to discover a bug is a remote instance over SSH.

```bash
cp .env.example .env      # set the MySQL passwords; AWS values can wait
scripts/local_stack.sh up     # build image, start MySQL pair + Redis, wire replication, run the app
scripts/local_stack.sh test   # 13 assertions across the full request path
scripts/local_stack.sh down   # stop everything, delete the volumes
```

`up` goes from nothing to a healthy replicated stack in about 40 seconds. `test` asserts rather than prints — it exercises MySQL writes, a Redis-cached redirect, a `/stats` read served **by the replica**, input validation, and that the replica really is read-only.

What this does *not* cover: EC2, S3, and the nginx traffic switch (which needs the instance's nginx and systemd). Everything else behaves exactly as it does in production, because it is the same image against the same MySQL and Redis.

If your machine already runs MySQL on 3306, set `MYSQL_PRIMARY_HOST_PORT` / `MYSQL_REPLICA_HOST_PORT` in `.env` — the containers talk to each other over `zdp-net` regardless, so only host-side access changes.

## Verified

Run and passing:

- `flake8 app deploy infra` — clean
- `pytest` — 16 unit tests, no live dependencies needed
- `bash -n` on all 12 shell scripts — clean
- `docker build` — 299 MB image, runs as non-root `uid=1001`
- `scripts/local_stack.sh test` — **13/13**, from a clean rebuild
- MySQL GTID replication — configured, verified, idempotent on re-run
- `scripts/db/backup_db.sh` — dump taken from the replica, integrity-verified, GTID position recorded
- `scripts/db/promote_replica.sh` — failover completed in **2 s**; replica became writable and accepted writes

Not yet exercised, because they need real AWS: `infra/provision.py`, `infra/teardown.py`, the S3 deployment ledger, and the nginx blue-green switch. Follow [docs/AWS-SETUP.md](docs/AWS-SETUP.md).

## Cost

Free tier, within the first 12 months: one t3.micro (750 h/month), 30 GB gp3 EBS, 5 GB S3, and S3 request volumes this project will not come close to. The lifecycle rules in `provision.py` prune old object versions and backups so the bucket cannot quietly grow. **Run `python infra/teardown.py` when you are done** — the instance is the only part that would ever cost money.
