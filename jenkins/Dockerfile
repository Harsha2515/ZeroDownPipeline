# Jenkins controller for this project.
#
# The stock jenkins/jenkins image has no Docker CLI, no Python and no SSH
# client - and this pipeline needs all three: it builds an image, runs pytest in
# a venv, and drives the deploy scripts over SSH. Rather than install them at
# build time inside the job (slow, and it re-downloads on every run), they are
# baked into the controller once.
#
# Only the Docker *client* is installed. The daemon comes from the host via the
# mounted socket, so builds run on your machine's Docker rather than a second
# engine inside the container.
FROM jenkins/jenkins:lts-jdk17

USER root

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl gnupg \
        python3 python3-venv python3-pip \
        openssh-client git shellcheck \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg \
      | gpg --dearmor -o /etc/apt/keyrings/docker.gpg \
 && chmod a+r /etc/apt/keyrings/docker.gpg \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/debian $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && rm -rf /var/lib/apt/lists/*

# The Jenkinsfile calls `python3 -m venv`; some Debian images ship a python3
# without the `python` alias, and the pipeline uses `python3` explicitly, so no
# alias is needed - but pip must exist for the venv to bootstrap.

USER jenkins

# Skip the "Getting Started" wizard's plugin-selection screen? No - leave the
# setup wizard on. Doing it by hand once is how you learn where things live.
