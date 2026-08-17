#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"
python3 tools/xfyun_stream_to_text.py "$@"
