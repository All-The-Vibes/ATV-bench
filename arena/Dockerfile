# ATV-bench arena image.
#
# Explicit local integration runs may execute a submitted bot inside THIS image under a
# locked-down `docker run` policy. GitHub Actions never builds or runs this image.
#
# The image is built explicitly from this in-repo trusted Dockerfile and run by a local
# tag — never pulled from a mutable/unbuilt `:latest` registry ref.
# That closes santa re-review finding #2 (the ok/scoring path was dead because
# `atv-bench/arena:latest` had no producer).
#
# TRUSTED ADJUDICATION (FOLLOW_UPS item 1, RESOLVED): the image ENTRYPOINT is the TRUSTED
# referee (`python3 -m atv_bench.arena`), NOT a bare `python3` that runs the bot's stdout
# as the result. The referee runs a deterministic lightcycles/Tron game, spawns the
# mounted bot at /work/main.py as an untrusted subprocess that may only emit ONE move
# token per turn (per-turn timeout), plays it against a trusted in-process anchor, and
# prints the ADJUDICATED result JSON derived from real gameplay. A bot that prints a
# fabricated win to stdout is emitting an invalid move and forfeits — it can never inject
# an outcome. The referee code is baked into the image (arena/pkg), so no trusted code is
# read from the untrusted /work mount.
#
# Base image pinned BY DIGEST for reproducibility: a mutable tag could be swapped under
# the sandbox between the tripwire test and the real run.
FROM python:3.12-slim-bookworm@sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b

# Bake the TRUSTED referee package into the image (byte-identical to src/atv_bench/arena;
# a drift tripwire in tests/test_arena_image.py asserts they match). It lives on the
# import path under /opt so the untrusted read-only /work mount can never shadow it.
COPY pkg/ /opt/arena/
ENV PYTHONPATH=/opt/arena

# Non-root by default (defense-in-depth; the sandbox also forces --user 65534). 65534
# is the conventional `nobody` uid present in the base image.
USER 65534:65534

# The bot is mounted read-only at /work by a local runner (`-v $PWD/submission:/work:ro`).
# It is NOT executed by the entrypoint — the trusted referee spawns it as a move-only
# subprocess and adjudicates the game itself.
WORKDIR /work
ENTRYPOINT ["python3", "-m", "atv_bench.arena"]
CMD ["/work/main.py"]
