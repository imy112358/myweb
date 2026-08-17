#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

if [[ -f ".xfyun_iat.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source ".xfyun_iat.env"
  set +a
fi

if [[ -z "${XFYUN_APP_ID:-}" ]]; then
  read -r -p "请输入讯飞 APPID: " XFYUN_APP_ID
  export XFYUN_APP_ID
fi

if [[ -z "${XFYUN_API_KEY:-}" ]]; then
  read -r -p "请输入讯飞 APIKey: " XFYUN_API_KEY
  export XFYUN_API_KEY
fi

if [[ -z "${XFYUN_API_SECRET:-}" ]]; then
  read -r -s -p "请输入讯飞 APISecret: " XFYUN_API_SECRET
  echo
  export XFYUN_API_SECRET
fi

python3 tools/xfyun_iat_stream_to_text.py "$@"
