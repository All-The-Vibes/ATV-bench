# ATV-bench arena image.
#
# The league match job runs the submitted bot inside THIS image, under a locked-down
# `docker run` (--network none, --read-only, --user 65534, --cap-drop ALL, caps on
# cpu/mem/pids, per-container timeout). See .github/workflows/league.yml.
#
# The image is built from this in-repo, TRUSTED Dockerfile inside the match job and run
# by a pinned local tag — never pulled from a mutable/unbuilt `:latest` registry ref.
# That closes santa re-review finding #2 (the ok/scoring path was dead because
# `atv-bench/arena:latest` had no producer).
#
# v1 scope: the arena provides the Python runtime that executes the mounted bot at
# /work/main.py and passes its stdout JSON through as the match result. Trusted
# outcome ADJUDICATION (the arena refereeing the game rather than trusting bot stdout)
# is deferred — see docs/FOLLOW_UPS.md item 1. The bot is still fully sandboxed here.
#
# Base image pinned BY DIGEST for reproducibility: a mutable tag could be swapped under
# the sandbox between the tripwire test and the real run.
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

# Non-root by default (defense-in-depth; the sandbox also forces --user 65534). 65534
# is the conventional `nobody` uid present in the base image.
USER 65534:65534

# The bot is mounted read-only at /work by the match job (`-v $PWD/submission:/work:ro`)
# and executed as /work/main.py. No bot code is baked into the image.
WORKDIR /work
ENTRYPOINT ["python3"]
