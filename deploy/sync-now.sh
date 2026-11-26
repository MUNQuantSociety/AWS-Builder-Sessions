#!/usr/bin/env bash
# Run one Plaid sandbox sync on this instance and write the results to S3.
#
# Usage (on the EC2 instance, after bootstrap.sh has finished):
#   sudo bash deploy/sync-now.sh
#
# Reads settings from /etc/finance-dashboard/env and the Plaid values from
# /etc/finance-dashboard/sync.env (both written by bootstrap.sh). Output goes to
# participants/<name>/ in the bucket. Check the result with /api/status.
set -euo pipefail

readonly APP_DIR=/opt/finance-dashboard
readonly ENV_FILE=/etc/finance-dashboard/env
readonly SYNC_ENV_FILE=/etc/finance-dashboard/sync.env

if [[ $EUID -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

for required_file in "${ENV_FILE}" "${SYNC_ENV_FILE}"; do
  if [[ ! -f "${required_file}" ]]; then
    echo "Missing ${required_file}. Run deploy/bootstrap.sh first." >&2
    exit 1
  fi
done

# The subshell keeps the exported variables out of this shell.
(
  set -a
  # shellcheck disable=SC1090
  . "${ENV_FILE}"
  # shellcheck disable=SC1090
  . "${SYNC_ENV_FILE}"
  set +a
  cd "${APP_DIR}"
  "${APP_DIR}/.venv/bin/python" server.py
)

echo "Sync finished. Check: curl -s http://127.0.0.1:8000/api/status"
