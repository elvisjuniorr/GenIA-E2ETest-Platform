#!/bin/sh
set -eu

printf 'window.GENIA_API_BASE_URL = "%s";\n' "${GENIA_API_BASE_URL:-}" > /usr/share/nginx/html/runtime-config.js
