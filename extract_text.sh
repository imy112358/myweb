#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
python3 tools/douyin_to_text.py "$@"
