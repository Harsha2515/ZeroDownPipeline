# Runbook

Day-2 operations. Run each of these at least once before you put this project on a CV — "I wrote a restore script" and "I have restored from a backup" are different claims, and interviewers ask which one you mean.

Throughout: `SSH` means `ssh -i zerodownpipeline-key.pem ec2-user@<public_ip>`, and everything on the instance lives in `/opt/zerodown`.

On Windows PowerShell, write `curl.exe` rather than `curl` — the bare name is an alias for `Invoke-WebRequest` and behaves differently.

---

## Where things are

| | |
|---|---|
| Scripts on the instance | `/opt/zerodown/scripts/` |
| Secrets | `/opt/zerodown/.env` (mode 600) |
| Live nginx config | `/etc/nginx/conf.d/zerodown.conf` |
| App logs | `docker logs zdp-app-blue` / `zdp-app-green` |
| nginx logs | `/var/log/nginx/zerodown.{access,error}.log` |
| First-boot log | `/var/log/user-data.log` |
| Backup log | `/var/log/zerodown-backup.log` |
| Deploy ledger | `s3://<bucket>/deployments/history.json` |

---

## Check the state of the world

```bash
# which colour is live, and what version is it?
curl -s http://<ip>/health
curl -sI http://<ip>/health | grep X-Served-By

# on the instance
docker ps --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
/opt/zerodown/scripts/db/replication_status.sh
free -h                      # watch swap usage - only 1 GiB of real RAM
df -h /                      # docker images accumulate

# recent deploys, from S3
python deploy/rollback.py --list
```

---

## Deploy a new version

```bash
python deploy/deploy.py --tag <git-short-sha>
```

Safe by construction: the new version starts on the inactive colour and only receives traffic after passing its health check. If it fails, it is destroyed and the current version keeps serving — no action needed from you.

---

## Roll back a version that deployed cleanly but is wrong

Automatic rollback covers builds that fail their health check. This is for the other case: it started fine, passed, and is misbehaving.

```bash
python deploy/rollback.py --list          # see what is available
python deploy/rollback.py                 # go back to the previous known-good
python deploy/rollback.py --tag a1b2c3d   # go back to something specific
```

The rollback target is health-checked before it gets traffic, exactly like a forward deploy. Being "known good" last week is not evidence it is good now — the database may have moved on since.

---

## Back up the database

Runs automatically at 02:00 UTC via `/etc/cron.d/zerodown-backup`. To run one now:

```bash
SSH
/opt/zerodown/scripts/db/backup_db.sh
```

What it does: dumps from the **replica** (so the write path is undisturbed), with `--single-transaction` (no table locks) and `--set-gtid-purged=ON` (records the exact replication position). Then it verifies the file is non-empty, gzip-valid, and actually contains a schema **before** uploading. An unverified backup is not a backup.

Check what exists:

```bash
aws s3 ls s3://<bucket>/backups/mysql/ --recursive
aws s3 cp s3://<bucket>/backups/latest.json -
```

Backups older than 30 days are removed by the S3 lifecycle rule.

---

## Restore from a backup

**Destructive — it replaces the current database.**

```bash
SSH
/opt/zerodown/scripts/db/restore_db.sh --confirm                     # latest
/opt/zerodown/scripts/db/restore_db.sh --confirm backups/mysql/2026/09/08/zerodown-20260908T020000Z.sql.gz
```

`restore_db.sh` loads the dump into **both** servers, not just the primary. Restoring only
the primary leaves the replica holding every transaction that happened after the backup —
and because those GTIDs are already in its executed set, replication reconnects, reports
healthy, and never reconciles the gap. The script prints both row counts and warns loudly
if they disagree.

After any restore, replication must still be re-pointed — the primary's GTID history was
reset, so `--force` is required to get past the "already running" check:

```bash
/opt/zerodown/scripts/db/setup_replication.sh --force
/opt/zerodown/scripts/db/replication_status.sh
```

Then prove it actually *flows*, rather than trusting the thread states:

```bash
curl -s -X POST http://localhost/shorten -H 'Content-Type: application/json'   -d '{"url":"https://replication-check.example.com"}'
# the new short_code must appear on BOTH servers
```

### Practise it properly

Do not test a restore for the first time during an incident:

```bash
# 1. note the current row count
docker exec mysql-primary mysql -uroot -p"$MYSQL_ROOT_PASSWORD" \
  -e "SELECT COUNT(*) FROM zerodown.links;"

# 2. take a backup
/opt/zerodown/scripts/db/backup_db.sh

# 3. add a row through the API
curl -X POST http://<ip>/shorten -H 'Content-Type: application/json' -d '{"url":"https://after-backup.example.com"}'

# 4. restore, and confirm the new row is gone
/opt/zerodown/scripts/db/restore_db.sh --confirm
```

If the count matches step 1, your backup is real. **Time this and record it** — restore time is a number worth having.

---

## Fail over to the replica

When the primary is unhealthy or corrupted:

```bash
SSH
/opt/zerodown/scripts/db/promote_replica.sh --confirm
```

The script drains the replica's relay log, stops replication, clears the replication config, drops `read_only`, rewrites `DB_HOST` in `.env`, and restarts the live app container against the new primary. It prints the elapsed time — **that is your failover number.**

Verify:

```bash
curl -s http://<ip>/health          # mysql_primary should be true again
curl -X POST http://<ip>/shorten -H 'Content-Type: application/json' -d '{"url":"https://post-failover.example.com"}'
```

### After a failover

You are running on a single database with no replica. To get back to a pair:

```bash
docker rm -f mysql-primary
docker volume rm zdp-data_mysql-primary-data
# edit docker-compose.data.yml so the OLD primary becomes the new replica
# (swap the server-id, read-only and skip-replica-start settings between the two)
docker compose --env-file /opt/zerodown/.env -f /opt/zerodown/docker-compose.data.yml up -d
/opt/zerodown/scripts/db/setup_replication.sh
```

**Never bring the old primary back up as a writer.** Two servers both accepting writes is how you get a split brain and a data-loss postmortem.

---

## Prove zero downtime

```bash
# terminal 1
python deploy/measure_downtime.py --duration 120 --rps 5 --out docs/downtime-run.json
# terminal 2
python deploy/deploy.py --tag <new-sha>
```

Zero failed requests plus a `SWITCHED` line is the whole claim, evidenced.

---

## Prove automatic rollback

```bash
python deploy/deploy.py --tag <sha> --break-health
```

Expect: health check fails after its retries, the bad container is destroyed, nginx is untouched, exit code 1, and a `"status": "failed"` entry in the S3 history. `curl http://<ip>/health` answers normally the entire time.

---

## Common problems

**502 from nginx.** The container behind the active port is gone.
```bash
docker ps -a | grep zdp-app
docker logs zdp-app-blue --tail 50
python deploy/rollback.py            # fastest way back
```

**Health check fails on every deploy.** Almost always the data tier.
```bash
docker ps | grep -E 'mysql|redis'
/opt/zerodown/scripts/db/replication_status.sh
docker logs zdp-app-green --tail 50   # the app logs which dependency it cannot reach
```

**Out of memory / the box is thrashing.**
```bash
free -h                               # swap in use is fine; swap full is not (1 GiB box)
docker stats --no-stream
docker image prune -af                # old images are usually the culprit
```

**Disk full.**
```bash
df -h /
docker system prune -af --volumes     # careful: --volumes deletes DB data
sudo truncate -s 0 /var/log/nginx/zerodown.access.log
```

**Both colours are running and you are unsure which is live.** nginx is the authority:
```bash
grep 'server 127.0.0.1' /etc/nginx/conf.d/zerodown.conf
. /opt/zerodown/scripts/lib.sh && active_color
```

**The public IP changed after a stop/start.** Re-run `python infra/provision.py` to refresh `outputs.json` and the SSH rule, then update `EC2_HOST` in Jenkins.
