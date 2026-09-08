# Shortcuts for the commands used most often.
# Everything here is a thin wrapper - the scripts are the real interface, and
# nothing in this file is required to use the project.
#
# On Windows, run the underlying commands directly, or use make from Git Bash.

SHELL := /bin/bash
TAG   ?= $(shell git rev-parse --short HEAD)
DOCKERHUB_USER ?= $(shell grep -E '^DOCKERHUB_USER=' .env 2>/dev/null | cut -d= -f2)
IMAGE := $(DOCKERHUB_USER)/zerodownpipeline-api:$(TAG)

.PHONY: help install test lint check build push provision sync deploy drill rollback \
        smoke downtime status backup teardown clean

help:  ## show this help
	@grep -E '^[a-z-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n", $$1, $$2}'

install:  ## install python dev dependencies
	pip install -r app/requirements-dev.txt

lint:  ## flake8 + bash syntax check
	flake8 app deploy infra
	@for f in $$(find scripts infra -name '*.sh'); do bash -n "$$f" || exit 1; done
	@echo "shell scripts parse cleanly"

test:  ## run the unit tests
	pytest

check: lint test  ## everything CI runs before it builds

build:  ## build the image, tagged with the current git sha
	docker build --build-arg APP_VERSION=$(TAG) -t $(IMAGE) .

push: build  ## build and push to docker hub
	docker push $(IMAGE)

provision:  ## create all AWS infrastructure
	python infra/provision.py

sync:  ## copy scripts and .env to the instance
	python deploy/sync_scripts.py

deploy:  ## deploy the current commit
	python deploy/deploy.py --tag $(TAG)

drill:  ## deploy a deliberately broken build to demo automatic rollback
	python deploy/deploy.py --tag $(TAG) --break-health

rollback:  ## roll back to the previous known-good version
	python deploy/rollback.py

smoke:  ## smoke-test the live endpoint through nginx
	python deploy/healthcheck.py

downtime:  ## measure availability across a deploy (run a deploy in another terminal)
	python deploy/measure_downtime.py --duration 120 --rps 5 --out docs/downtime-run.json

status:  ## show recent deploy history from S3
	python deploy/rollback.py --list

teardown:  ## terminate the EC2 instance (keeps S3 and IAM)
	python infra/teardown.py

clean:  ## remove local build and test artefacts
	rm -rf .pytest_cache .ruff_cache test-results.xml
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
