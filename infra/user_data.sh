#!/usr/bin/env bash
# EC2 first-boot bootstrap (Amazon Linux 2023).
#
# Runs once, as root, before anyone logs in. Everything the box needs to be a
# deploy target is installed here, so the instance is reproducible from
# provision.py alone - there is no "and then I SSHed in and installed things"
# step hiding in this project.
#
# Progress is logged to /var/log/user-data.log and a marker file is written at
# the end, which is what provision.py waits on.
exec > >(tee -a /var/log/user-data.log) 2>&1
set -xeuo pipefail

echo "=== zerodown bootstrap started at $(date -u) ==="

dnf update -y
dnf install -y docker nginx git jq unzip tar gzip

# --- swap -------------------------------------------------------------------
# t3.micro has 2 GB. Two MySQL servers, Redis, two app containers and nginx
# will fit, but only just. 2 GB of swap turns a would-be OOM kill during a
# deploy into a brief slowdown.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10
  echo 'vm.swappiness=10' > /etc/sysctl.d/99-zerodown.conf
fi

# --- docker -----------------------------------------------------------------
systemctl enable --now docker
usermod -aG docker ec2-user

# The compose plugin is not in the AL2023 repos, so install it as a CLI plugin.
COMPOSE_VERSION=v2.29.1
mkdir -p /usr/libexec/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
  -o /usr/libexec/docker/cli-plugins/docker-compose
chmod +x /usr/libexec/docker/cli-plugins/docker-compose

# --- aws cli v2 -------------------------------------------------------------
if ! command -v aws >/dev/null 2>&1; then
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o /tmp/awscliv2.zip
  unzip -q /tmp/awscliv2.zip -d /tmp
  /tmp/aws/install
  rm -rf /tmp/aws /tmp/awscliv2.zip
fi

# --- nginx ------------------------------------------------------------------
# The default server block would answer on port 80 and shadow ours, so it goes.
rm -f /etc/nginx/conf.d/default.conf
if [ -f /etc/nginx/nginx.conf ]; then
  sed -i '/^\s*server\s*{/,/^\s*}/d' /etc/nginx/nginx.conf.default 2>/dev/null || true
fi
systemctl enable nginx

# nginx must be allowed to proxy to localhost ports under SELinux.
setsebool -P httpd_can_network_connect 1 2>/dev/null || true

# --- application directories ------------------------------------------------
mkdir -p /opt/zerodown/{scripts,deploy/nginx,state,backups}
chown -R ec2-user:ec2-user /opt/zerodown

# --- app network ------------------------------------------------------------
docker network inspect zdp-net >/dev/null 2>&1 || docker network create zdp-net

# --- nightly backup cron ----------------------------------------------------
cat > /etc/cron.d/zerodown-backup <<'CRON'
# Nightly MySQL backup to S3, 02:00 UTC.
0 2 * * * ec2-user /opt/zerodown/scripts/db/backup_db.sh >> /var/log/zerodown-backup.log 2>&1
CRON
chmod 0644 /etc/cron.d/zerodown-backup
touch /var/log/zerodown-backup.log
chown ec2-user:ec2-user /var/log/zerodown-backup.log

echo "=== zerodown bootstrap finished at $(date -u) ==="
touch /opt/zerodown/.bootstrap-complete
