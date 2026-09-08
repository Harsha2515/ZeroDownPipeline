# Jenkins setup

Jenkins runs **on your laptop, in Docker** — not on the EC2 instance. The t3.micro has 1 GiB of RAM and already hosts two MySQL servers, Redis, nginx and two app containers; adding a JVM to that would get something OOM-killed mid-deploy. Jenkins is the thing that *talks to* the instance, so it does not need to live there.

---

## 1. Run Jenkins

The controller needs the Docker CLI (to build images), Python 3, and SSH (to reach EC2). The stock image has none of those, so build a small one.

The image definition lives in [`jenkins/Dockerfile`](../jenkins/Dockerfile) — it adds the
Docker CLI, Python, SSH, git and shellcheck to the stock Jenkins image. Only the Docker
*client* is installed; the daemon comes from your machine via the mounted socket.

```powershell
cd "C:\Z - Projects\ZeroDownPipeline"
docker build -t jenkins-zdp jenkins/
docker volume create jenkins_home

docker run -d --name jenkins `
  -p 8080:8080 -p 50000:50000 `
  -v jenkins_home:/var/jenkins_home `
  -v //var/run/docker.sock:/var/run/docker.sock `
  --group-add 0 `
  --restart unless-stopped `
  jenkins-zdp
```

Two Windows-specific details, both of which will bite you otherwise:

- **`//var/run/docker.sock`** with a leading double slash. A single slash gets rewritten by
  MSYS/Git Bash path translation into a Windows path that does not exist.
- **`--group-add 0`**. On Docker Desktop the socket inside the container is `root:root`
  mode 660, while Jenkins runs as uid 1000. Without this you get
  `permission denied while trying to connect to the docker API`. Linux guides say to use
  the `docker` group's gid — there is no such group here, so group 0 is the equivalent.

> Mounting the Docker socket gives the Jenkins container full control of your machine's
> Docker daemon. That is inherent to socket-mounted builds, not something `--group-add`
> introduces — but it is worth knowing that this container is privileged in practice, and
> is a reason not to expose it beyond localhost.

Verify it came up correctly before going further:

```powershell
docker exec jenkins docker version --format "server {{.Server.Version}}"
docker exec jenkins bash -c "python3 --version; git --version"
```

Unlock it:

```bash
docker exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword
```

Open <http://localhost:8080>, paste the password, choose **Install suggested plugins**, and create your admin user.

Then **Manage Jenkins → Plugins → Available** and install:

- **Pipeline: Stage View** — the visual stage timeline (this is what you screenshot)
- **SSH Agent**
- **Credentials Binding**
- **Timestamper**

---

## 2. Add the three credentials

**Manage Jenkins → Credentials → System → Global credentials → Add Credentials.** The IDs must match the `Jenkinsfile` exactly.

| ID | Kind | Contents |
|---|---|---|
| `dockerhub-credentials` | Username with password | Docker Hub username + the **access token** from AWS-SETUP 0.6 (not your password) |
| `ec2-ssh-key` | SSH Username with private key | Username `ec2-user`; **Enter directly** and paste the full contents of `zerodownpipeline-key.pem`, including the `-----BEGIN` and `-----END` lines |
| `aws-credentials` | Username with password | Username = AWS access key ID, Password = AWS secret access key |

Jenkins needs its own AWS credentials because `deploy.py` writes the deployment ledger to S3 from the Jenkins side. The EC2 instance uses its IAM role instead and holds no keys at all.

---

## 3. Point the Jenkinsfile at your accounts

Edit the `environment` block at the top of `Jenkinsfile`:

```groovy
DOCKERHUB_USER = 'your-dockerhub-username'
S3_BUCKET      = 'zerodownpipeline-data-harsha2515'
AWS_REGION     = 'ap-south-1'
```

Commit and push.

---

## 4. Create the job

**New Item** → name `ZeroDownPipeline` → **Pipeline** → OK.

- **Pipeline → Definition**: *Pipeline script from SCM*
- **SCM**: Git
- **Repository URL**: `https://github.com/Harsha2515/ZeroDownPipeline.git`
- **Branch**: `*/main`
- **Script Path**: `Jenkinsfile`

Under **Build Triggers**, tick **Poll SCM** with schedule `H/5 * * * *` — checks GitHub every five minutes.

> A webhook is instant and looks better in a demo, but GitHub cannot reach a Jenkins on `localhost`. If you want webhooks, expose it with `ngrok http 8080` and set the resulting URL as a GitHub webhook (`/github-webhook/`).

---

## 5. Give Jenkins the instance details

`deploy.py` normally reads `infra/outputs.json`, which is gitignored — so the Jenkins workspace does not have it.

`S3_BUCKET`, `AWS_REGION`, `DOCKERHUB_USER` and `DOCKER_IMAGE` are already set in the
`environment` block at the top of the `Jenkinsfile`, so there is nothing to add for those.
That leaves the host, and it does not need setting either — see below.

If you ever do need to override one, **Manage Jenkins → System → Global properties →
Environment variables** is the place: `load_config()` puts the environment ahead of both
`.env` and `outputs.json`.

### You do not have to set `EC2_HOST`

The public IP changes every time the instance is replaced, and a hand-copied
address in this settings page is a note that goes stale the moment that
happens — which shows up later as `Connection timed out` on a build whose code
was perfectly fine.

So when no host is configured, `discover_host()` asks AWS directly for the
running instance tagged `Name=zerodownpipeline-app` and uses its current public
IP. Provision a new instance and the next build finds it, with nothing to
update here.

The build log says when this happens:

```
[deploy] discovered 13.201.186.129 from AWS (i-0abc123, tag Name=zerodownpipeline-app)
```

Two things worth knowing:

- **The AWS credentials Jenkins uses need `ec2:DescribeInstances`.** Without it,
  discovery is skipped and you are back to setting `EC2_HOST` by hand.
- **If two instances carry that tag, the deploy refuses rather than guessing.**
  Picking whichever AWS listed first is how you discover, a week later, that
  production was never updated. Set `EC2_HOST` to name the one you mean.

`EC2_HOST` still wins when set, so it remains the override for pointing a build
at a different box.

---

## 6. Run it

**Build Now.** The first run takes longest — it builds the Python venv and pulls base images.

Stages: Checkout → Setup → Lint → Test → Shell lint → Build image → Push image → Deploy → Smoke test.

**The screenshot worth keeping** is the Stage View after a few runs: green columns with per-stage timings, including one **yellow (UNSTABLE)** run from a rollback drill. That single image tells the whole story.

---

## 7. Run the rollback drill through the pipeline

**Build with Parameters → tick `ROLLBACK_DRILL` → Build.**

The Deploy stage fails its health check on purpose, the container is torn down, and the build finishes **UNSTABLE** rather than FAILED — because the deploy failing *and* being caught is the intended outcome. Traffic never moves; keep a `curl http://<ip>/health` running in a terminal to show the old version answering throughout.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `docker: not found` in the build | You used the stock `jenkins/jenkins` image. Rebuild with the Dockerfile above. |
| `permission denied /var/run/docker.sock` | `docker exec -u root jenkins chmod 666 /var/run/docker.sock` (or fix the `--group-add` gid). |
| `Host key verification failed` | The scripts already pass `StrictHostKeyChecking=no`. If you see this, the SSH credential is wrong or the key was pasted without its header/footer lines. |
| `Permission denied (publickey)` in Deploy | The `ec2-ssh-key` credential's username must be `ec2-user`. |
| `NoCredentialsError` from boto3 | `aws-credentials` is missing, or its ID does not match the Jenkinsfile. |
| `no EC2 host, and no running instance tagged...` | Nothing is running, or the AWS credentials lack `ec2:DescribeInstances`. Run `infra/provision.py`, or set `EC2_HOST` (step 5). |
| Build hangs on Setup | The venv is downloading. First run only; a few minutes is normal. |
| Deploy fails with `connection timed out` | The security group only allows SSH from the IP `provision.py` saw. If your IP changed, re-run `provision.py`. |
