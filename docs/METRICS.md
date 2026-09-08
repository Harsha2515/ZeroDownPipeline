# Metrics — measure them, do not estimate them

Every number in the README table comes from a script in this repo. Fill this file in as you run them, then copy the results up into the README.

The reason to be strict about this: an interviewer who has built a pipeline can tell an estimate from a measurement in about two follow-up questions. "Roughly 3 minutes" invites doubt. "3m 42s, here is the Jenkins build history" ends the topic.

---

## 1. Manual deploy baseline

**Measure once, before Jenkins exists.** Follow step 4 of [AWS-SETUP.md](AWS-SETUP.md) with a stopwatch, and count every discrete action you take — every command, every SSH, every copy-paste of a tag.

| | Value |
|---|---|
| Date measured | 2026-09-08 |
| Wall-clock time | _NOT RECORDED - fill this in_ |
| Number of manual steps | _NOT RECORDED - 8 commands across 2 windows, plus the tag copy-paste_ |
| Steps that needed a second attempt | |

> The manual deploy was performed on 2026-09-08 (build, push, then `start_container.sh`,
> `healthcheck.sh`, `switch_traffic.sh` over SSH) but was not timed. **Either put your own
> honest estimate here and label it an estimate, or leave it blank.** Do not invent a
> figure - the whole value of this row is that it is measured.

A typical honest first result is 8–15 minutes and 12–16 steps. Do not tidy it up afterwards; the messiness is the point of the comparison.

---

## 2. Automated deploy, push to live

Read it off the Jenkins build page — total duration, and the Stage View for the per-stage split.

Measured directly from `deploy.py` (deploy step only - no build or push):

| Run | Total | Health check | Switch | Result |
|---|---|---|---|---|
| 1 | 11.7 s | 1.8 s | 8.1 s | blue -> green |
| 2 | 10.8 s | 1.7 s | - | green -> blue |
| 3 | 10.1 s | 1.5 s | - | blue -> green |
| 4 | 10.0 s | 1.2 s | - | green -> blue |
| 5 | 9.0 s | 1.3 s | - | blue -> green |
| 6 | 8.7 s | 1.1 s | - | green -> blue |

**Median: 10.1 s** across 6 runs. The first is slowest (cold image pull on the instance).

### Full pipeline, git push to live traffic

Jenkins build #1, all nine stages (checkout, venv, lint, pytest, shellcheck, docker build,
push to Docker Hub, deploy, smoke test):

| Run | Total | Deploy stage | Result |
|---|---|---|---|
| #1 | **3 min 11 s** (190.9 s) | 16.5 s | SUCCESS |

Triggered by `git push`, with no manual step between the push and live traffic.

Take the median of at least three runs. The first is always slower (cold caches) and quoting it flatters you in the wrong direction.

`deploy.py` also records its own timings in S3:

```bash
aws s3 cp s3://<bucket>/deployments/history.json - | python -m json.tool | tail -40
```

---

## 3. Downtime during a deploy

```bash
# terminal 1
python deploy/measure_downtime.py --duration 120 --rps 5 --out docs/downtime-run.json
# terminal 2, while that runs
python deploy/deploy.py --tag <new-sha>
```

Two independent runs, each with a real deploy inside the window:

| | Run 1 | Run 2 |
|---|---|---|
| Requests sent | 287 | 367 |
| **Failed during the switch** | **0** | **0** |
| Failed elsewhere in the window | 4 | 3 |
| Traffic switch observed at | 12.7 s | 12.2 s |
| p50 / p95 latency | 98 / 148 ms | 85 / 106 ms |
| `served_by` flip | blue -> green | green -> blue |

**654 requests across two live traffic switches, zero failed at either switch.**

The failures in both runs landed at random points far from the switch (30-86 s in run 1,
61-78 s in run 2) and nginx's error log was empty throughout - client-side timeouts on the
probe's connection, not the deploy. `measure_downtime.py` reports the two categories
separately for exactly this reason: an aggregate availability figure cannot distinguish
"the deploy dropped traffic" from "my WiFi hiccuped", and only the first one is a claim
about the pipeline.

**Commit `docs/downtime-run.json`.** It is the difference between a claim and evidence.

If you see a small number of failures, that is still a result worth reporting honestly — and worth investigating. The usual causes are the old container being stopped before nginx finished reloading (raise `--grace`) or the box swapping under load.

---

## 4. Time to automatic rollback (MTTR)

```bash
python deploy/deploy.py --tag <sha> --break-health
```

The script prints `time from deploy start to completed rollback`. It is also stored as `time_to_rollback_seconds` in the S3 history.

| Run | Time to rollback | Health check attempts used | Traffic moved? |
|---|---|---|---|
| 1 | **35.7 s** | 10 of 10 (all failed) | No - green served throughout |

Recorded in S3 as a `"status": "failed"` entry alongside the successes.

This number is dominated by your health check settings: `--retries 10 --interval 3` means a failing deploy takes ~30 s to be declared dead. That is a deliberate trade — fewer retries detects failure faster but risks failing a slow-starting-but-healthy container. **Be ready to say that out loud**; it is the follow-up question this metric attracts.

---

## 5. Manual steps eliminated

| | Before | After |
|---|---|---|
| Steps to deploy | | 1 (`git push`) |
| Steps to roll back | | 0 (automatic) |
| People who must be watching | 1 | 0 |

---

## 6. Database operations

| Operation | Command | Time | Notes |
|---|---|---|---|
| Backup (full) | `scripts/db/backup_db.sh` | ~2 s | 4 KB gzipped, uploaded to S3, verified before upload |
| Restore | `scripts/db/restore_db.sh --confirm` | **2 s** | verified on EC2: 5 rows -> added 1 -> restored -> 5 rows, test row gone |
| Failover | `scripts/db/promote_replica.sh --confirm` | **2 s** | replica promoted, accepted writes (tested locally) |
| Replication repair after restore | `setup_replication.sh --force` | **1 s** | both servers reconciled, verified by a live write |
| Replication lag, steady state | `scripts/db/replication_status.sh` | **0 s behind** | measured on EC2 |
| Infrastructure teardown | `infra/teardown.py` | ~40 s | instance terminated; S3, IAM and key pair preserved by design |

---

## Turning these into resume bullets

Once the table is filled in, the bullets write themselves. Replace the bracketed values with **your** measurements — do not ship these with the placeholders in.

> **ZeroDownPipeline** — Jenkins CI/CD to AWS, Docker, MySQL, S3, Bash
> - Built a zero-downtime CI/CD pipeline (Jenkins, Docker, AWS EC2/S3, boto3) deploying a containerized Python API via a blue-green Nginx switch, cutting deploys from **[N] minutes and [M] manual steps to a single git push in [X] minutes**.
> - Engineered an automated health-check gate with rollback, recovering from a failed deploy in **[Y] seconds with zero human intervention** and **0 failed requests across [K] measured requests** during a live traffic switch.
> - Automated all AWS provisioning and teardown with boto3 (EC2, S3 versioning + lifecycle, IAM instance roles, security groups) — reproducible infrastructure with **no long-lived credentials on the instance**.
> - Set up MySQL primary/replica replication with verified nightly `mysqldump` backups to S3 and a scripted failover completing in **[Z] seconds**, plus Redis caching on the read path.

### Why these are phrased this way

- Each one leads with an action and lands on a measured outcome. A bullet with no number is a description of a tutorial.
- "Zero human intervention" and "0 failed requests" are the two phrases a DevOps reviewer will stop on — both are things you can back with a file in the repo.
- The tools are named because resume filters look for them, but every tool sits next to what it accomplished, not in a list on its own.
- Nothing here claims scale. One t3.micro is not a large-scale system, and claiming it is invites a question you cannot answer. Reliability is the claim this project genuinely supports — lead with that instead.

---

## Questions to expect, and where the answer lives

| Question | Where you demonstrate it |
|---|---|
| "Walk me through what happens on a push." | README architecture section |
| "How do you know it is actually zero downtime?" | `docs/downtime-run.json` |
| "What happens if the health check passes but the app is broken?" | The smoke-test stage, then `rollback.py` |
| "Why not `:latest`?" | README design decisions |
| "How would you do this with 20 instances?" | ALB + target groups; the switch logic becomes a target-group swap |
| "Have you ever restored a backup?" | RUNBOOK "Practise it properly" — with the row count you verified |
| "What breaks first under load?" | Honest answer: RAM. It is a 1 GiB t3.micro running two MySQL servers on swap. Say so. |
