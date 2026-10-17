#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
umask 077
exec flock -n .chat-monitor.lock python -u main.py --run-test-monitor "${1:---resume-test-account}"
