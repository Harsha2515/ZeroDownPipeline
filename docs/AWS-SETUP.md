# AWS setup, step by step

Written for someone who has never opened the AWS console. Follow it in order; every step says what you should see when it worked, and what to do when it did not.

Total time: about 90 minutes the first time, most of it waiting.

---

## Step 0 — Accounts and tools

### 0.1 Create an AWS account

Go to <https://aws.amazon.com/free/> and sign up. You need a card; you will not be charged if you stay inside the free tier and tear down when you are done.

The email and password you just made is the **root user**. You will use it exactly twice: now, and never again. Root can do anything, including delete your account, so it is not what you build with.

### 0.2 Set a billing alarm — do this before anything else

Everyone means to do this later. Later is after the bill.

1. Sign in as root → top-right menu → **Billing and Cost Management**
2. Left sidebar → **Billing preferences** → tick **Receive AWS Free Tier alerts**, enter your email → Save
3. Left sidebar → **Budgets** → **Create budget** → choose **Use a template** → **Zero spend budget** → enter your email → Create

You now get an email the moment anything costs money. This project should never trigger it.

### 0.3 Create an IAM user for yourself

Root is for emergencies. Day-to-day work uses an IAM user.

1. Console search bar → **IAM** → **Users** → **Create user**
2. User name: `zerodown-admin` → tick **Provide user access to the AWS Management Console** → **I want to create an IAM user** → set a password → Next
3. Permissions → **Attach policies directly** → tick **AdministratorAccess**
   *(For a personal learning account this is fine. On a real team you would scope this down.)*
4. Create user
5. On the success screen, **save the sign-in URL** — it looks like `https://123456789012.signin.aws.amazon.com/console`

Sign out of root. Sign in with that URL as `zerodown-admin`. Everything from here on is done as this user.

### 0.4 Create access keys

The scripts need programmatic access.

1. IAM → **Users** → `zerodown-admin` → **Security credentials** tab
2. **Access keys** → **Create access key** → choose **Command Line Interface (CLI)** → tick the confirmation → Next → Create
3. You get an **Access key ID** (`AKIA...`) and a **Secret access key**.

**The secret is shown exactly once.** Copy both somewhere safe right now. If you lose the secret, delete the key and make a new one — there is no way to look it up.

Never commit these. Never paste them into a chat, a screenshot, or a public repo. GitHub scans for them and bots find leaked keys within minutes.

### 0.5 Install the tools

| Tool | Install | Check |
|---|---|---|
| AWS CLI v2 | <https://aws.amazon.com/cli/> | `aws --version` |
| Python 3.11+ | <https://python.org> | `python --version` |
| Docker Desktop | <https://docker.com/products/docker-desktop> | `docker --version` |
| Git | <https://git-scm.com> | `git --version` |

Then configure the CLI:

```bash
aws configure
```

It asks four things:

```
AWS Access Key ID     : AKIA...            (from 0.4)
AWS Secret Access Key : ...                (from 0.4)
Default region name   : ap-south-1         (Mumbai — closest to India, use it)
Default output format : json
```

Verify:

```bash
aws sts get-caller-identity
```

You should see your account number and `.../zerodown-admin`. If you get `Unable to locate credentials`, `aws configure` did not save — run it again.

### 0.6 Create a Docker Hub account

<https://hub.docker.com> → sign up. Then **Account Settings → Personal access tokens → Generate new token** with Read/Write scope. Save the token; Jenkins uses it instead of your password.

---

## Before you start - run it locally

Everything except EC2, S3 and the nginx switch runs on your own machine:

```bash
cp .env.example .env
scripts/local_stack.sh up
scripts/local_stack.sh test
scripts/local_stack.sh down
```

Do this first. Finding a bug locally takes seconds; finding the same bug over SSH on a t3.micro takes an hour.

---

## Step 1 — Configure the project

```bash
git clone https://github.com/Harsha2515/ZeroDownPipeline.git
cd ZeroDownPipeline
cp .env.example .env
pip install boto3
```

Open `.env` and set:

```bash
AWS_REGION=ap-south-1
S3_BUCKET=zerodownpipeline-data-harsha2515   # must be globally unique across ALL of AWS
DOCKERHUB_USER=your-dockerhub-username
MYSQL_ROOT_PASSWORD=<a long random string>
MYSQL_PASSWORD=<a different long random string>
MYSQL_REPL_PASSWORD=<a third one>
```

Generate passwords with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.

`.env` is in `.gitignore`. Keep it that way — it holds your database passwords.

> **Bucket names are globally unique across every AWS account on Earth.** If `provision.py` says the name is taken, add something of your own to it.

---

## Step 2 — Create the infrastructure

```bash
python infra/provision.py
```

This takes 3–5 minutes, most of it waiting for the instance to boot. It creates:

| Resource | What it is | Why |
|---|---|---|
| S3 bucket | versioned, private, lifecycle-pruned | records, deploy ledger, DB backups |
| IAM role + instance profile | scoped to that one bucket | the box reaches S3 with **no access keys on it** |
| Key pair | `zerodownpipeline-key.pem`, written locally | SSH |
| Security group | 22 from your IP, 80 from anywhere | 8000/8001 stay internal — nginx is the only public door |
| EC2 instance | t3.micro, 30 GB gp3, Amazon Linux 2023 | the deploy target |

The script is idempotent: run it again and it verifies rather than duplicates.

**What success looks like** — the last block prints your instance details:

```
[provision] done. Summary:
    region               ap-south-1
    s3_bucket            zerodownpipeline-data-harsha2515
    instance_id          i-0abc123...
    public_ip            13.234.56.78
```

It also writes `infra/outputs.json`. Every other script reads that file, so you never copy an IP by hand.

### 2.1 Lock down the key file

**Linux/macOS:** `provision.py` already did it.

**Windows** — SSH will refuse the key until you do this:

```powershell
icacls .\zerodownpipeline-key.pem /inheritance:r /grant:r "$env:USERNAME:R"
```

### 2.2 Wait for first boot to finish

The instance is running, but it is still installing Docker, nginx and the AWS CLI. Give it 2–3 minutes, then:

```bash
ssh -i zerodownpipeline-key.pem ec2-user@<public_ip>
```

First connection asks about the host key — type `yes`.

On the box:

```bash
ls /opt/zerodown/.bootstrap-complete   # exists = first boot finished
sudo tail -30 /var/log/user-data.log   # what it did
docker --version && nginx -v && aws --version
```

If `.bootstrap-complete` is missing after five minutes, read the log — it records every command.

Type `exit` to come back.

### 2.3 Look at what you built (optional, but do it once)

Console → **EC2** → **Instances**: your instance, `running`, 2/2 checks passed.
Console → **S3** → your bucket → **Properties**: Bucket Versioning is **Enabled**.

Seeing the resources your own script created is the point of the exercise.

---

## Step 3 — Ship the scripts and start the database

```bash
python deploy/sync_scripts.py
```

This copies `scripts/`, the nginx template, the compose file and your `.env` to `/opt/zerodown` on the instance. Re-run it any time you change a script.

Now bring up MySQL and Redis — **once**, not on every deploy:

```bash
ssh -i zerodownpipeline-key.pem ec2-user@<public_ip>
/opt/zerodown/scripts/db/bootstrap_db.sh
```

Takes 2–3 minutes: MySQL initialises two data directories, then replication is configured. Verify:

```bash
/opt/zerodown/scripts/db/replication_status.sh
```

You want:

```
  IO thread            : Yes
  SQL thread           : Yes
  Seconds behind source: 0
```

If either thread says `No`, the script prints the error. The usual cause is a `.env` password mismatch — fix `.env` locally, re-run `sync_scripts.py`, then `setup_replication.sh` again.

---

## Step 4 — The manual deploy (do this once, and time it)

Before automating anything, do it by hand — this is your baseline number, and you cannot claim an improvement without it.

**Start a timer. Count every step.**

```bash
# on your machine
export TAG=$(git rev-parse --short HEAD)
docker build --build-arg APP_VERSION=$TAG -t <dockerhub-user>/zerodownpipeline-api:$TAG .
docker login
docker push <dockerhub-user>/zerodownpipeline-api:$TAG

# on the instance
ssh -i zerodownpipeline-key.pem ec2-user@<public_ip>
/opt/zerodown/scripts/start_container.sh blue <dockerhub-user>/zerodownpipeline-api:$TAG $TAG
/opt/zerodown/scripts/healthcheck.sh 8000 $TAG
/opt/zerodown/scripts/switch_traffic.sh blue
exit

# verify
curl http://<public_ip>/health
```

**Stop the timer. Write down the minutes and the step count.** Record them in `docs/METRICS.md`. This is the "before" half of a resume bullet, and it is the half most people skip.

Test the app properly:

```bash
curl -X POST http://<public_ip>/shorten \
  -H 'Content-Type: application/json' \
  -d '{"url":"https://github.com/Harsha2515/ZeroDownPipeline"}'

curl -i http://<public_ip>/<short_code>        # 302
curl http://<public_ip>/stats/<short_code>     # served from the replica
```

Then check S3 — console → your bucket → `records/`. Click the object, then the **Versions** toggle. Versioning is why old copies are still there.

---

## Step 5 — Automated deploys

Now the same thing, one command:

```bash
python deploy/deploy.py --tag $TAG
```

It picks the inactive colour, starts the container there, health-checks it, switches nginx, drains the old container, and writes the result to S3. Time it against your baseline.

### The rollback drill — the demo that matters

```bash
python deploy/deploy.py --tag $TAG --break-health
```

Watch the log. The health check fails, the bad container is destroyed, nginx is never touched, and the script exits non-zero. Meanwhile, in another terminal, the site keeps answering:

```bash
curl http://<public_ip>/health   # still the old version, still 200
```

**Record the "time from deploy start to completed rollback" the script prints.** That is your MTTR number.

### The zero-downtime measurement

Terminal 1:

```bash
python deploy/measure_downtime.py --duration 120 --rps 5 --out docs/downtime-run.json
```

Terminal 2, while that runs:

```bash
python deploy/deploy.py --tag <a different sha>
```

The probe prints `SWITCHED` when `X-Served-By` flips, and a failure count at the end. Zero failures is the result you are looking for, and the JSON file is your evidence. Commit it.

---

## Step 6 — Jenkins

See [JENKINS-SETUP.md](JENKINS-SETUP.md). Run Jenkins in Docker on your laptop — putting it on the t3.micro alongside two MySQL servers will exhaust the RAM.

---

## Step 7 — Day-2 operations

See [RUNBOOK.md](RUNBOOK.md) for backups, restores, database failover and manual rollback. Run each one at least once. "I have a backup script" and "I have restored from it" are different sentences in an interview.

---

## Step 8 — Shut it down

The instance is the only part with real cost. When you are done for the day:

```bash
python infra/teardown.py
```

That terminates the instance and keeps S3, IAM, the key pair and the security group — so `provision.py` brings you back up quickly. Your database backups stay in S3.

To remove everything including the bucket:

```bash
python infra/teardown.py --all --delete-bucket --confirm
```

Then confirm in the console that **EC2 → Instances** shows only `terminated` entries. A terminated instance costs nothing.

> **Keep a screen recording of the rollback demo before you tear down.** It is the single most convincing artefact this project produces, and you do not want to rebuild the environment to make one.

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `Unable to locate credentials` | `aws configure` again; check `aws sts get-caller-identity` |
| `bucket name is taken by another account` | Bucket names are globally unique. Change `S3_BUCKET` in `.env`. |
| `UnauthorizedOperation` from provision.py | Your IAM user lacks permissions. IAM → Users → attach `AdministratorAccess`. |
| SSH: `Permission denied (publickey)` | Wrong user — it is `ec2-user`, not `root` or `ubuntu`. Check the `.pem` path. |
| SSH: `UNPROTECTED PRIVATE KEY FILE` | Run the `icacls` command from step 2.1 (Windows) or `chmod 600` (Linux/macOS). |
| SSH hangs with no error | Your public IP changed (common on home broadband). Re-run `provision.py` — it re-detects your IP and adds the rule. |
| `curl http://<ip>/` connection refused | nginx has no config until the first `switch_traffic.sh`. Deploy first. |
| Health check fails, container logs show DB errors | The data tier is not up. `docker ps` on the box; re-run `bootstrap_db.sh`. |
| `docker: permission denied` on the instance | The `docker` group membership needs a fresh login. `exit` and SSH back in. |
| Instance unreachable, everything was fine yesterday | Free tier is 750 hours/month — one instance running continuously fits, two do not. Check for a second instance. |
| Replica shows `Seconds behind source: NULL` | Replication stopped. `replication_status.sh` prints the error; usually a password mismatch after an `.env` edit. |
