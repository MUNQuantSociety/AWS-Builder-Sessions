#!/usr/bin/env bash
# Set up the dashboard and the Plaid sync on an Amazon Linux 2023 EC2 instance.
#
# Usage (from the cloned repo, as root):
#   sudo bash deploy/bootstrap.sh <participant-name>
#
# What it does:
#   1. Installs Python and the app into /opt/finance-dashboard.
#   2. Reads the bucket name and the Plaid sandbox values from Parameter Store.
#   3. Writes the dashboard settings to /etc/finance-dashboard/env (readable by the
#      service user) and the Plaid values to /etc/finance-dashboard/sync.env
#      (root only, mode 600).
#   4. Installs and starts the dashboard service on port 8000.
#
# It does not run the sync. Run deploy/sync-now.sh for that, after this script.
set -euo pipefail

readonly APP_DIR=/opt/finance-dashboard
readonly ENV_DIR=/etc/finance-dashboard
readonly ENV_FILE="${ENV_DIR}/env"
readonly SYNC_ENV_FILE="${ENV_DIR}/sync.env"
readonly SSM_PREFIX=/finance-dashboard
readonly SERVICE_USER=finance

if [[ $EUID -ne 0 ]]; then
  echo "Run this script with sudo." >&2
  exit 1
fi

if [[ $# -ne 1 || ! $1 =~ ^[a-z0-9][a-z0-9-]{0,39}$ ]]; then
  echo "Usage: sudo bash deploy/bootstrap.sh <participant-name>" >&2
  echo "Name: lowercase letters, digits, and hyphens, up to 40 characters." >&2
  exit 1
fi
readonly PARTICIPANT="$1"

# Read one parameter. A failed lookup stops the script under set -e.
get_param() {
  aws ssm get-parameter \
    --name "${SSM_PREFIX}/$1" \
    --with-decryption \
    --query Parameter.Value \
    --output text
}

dnf install -y python3 python3-pip
if ! id "${SERVICE_USER}" &>/dev/null; then
  useradd --system --home-dir "${APP_DIR}" --shell /sbin/nologin "${SERVICE_USER}"
fi

rm -rf "${APP_DIR}"
mkdir -p "${APP_DIR}"
# Copy only the files the app needs. Never copy .env or local_store/.
cp server.py client.py requirements.txt "${APP_DIR}/"
python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

mkdir -p "${ENV_DIR}"

bucket_name="$(get_param bucket_name)"
plaid_client_id="$(get_param plaid/client_id)"
plaid_secret="$(get_param plaid/secret)"
plaid_access_token="$(get_param plaid/access_token)"

umask 077
{
  printf 'BUCKET_NAME=%s\n' "${bucket_name}"
  printf 'STORE_PREFIX=participants/%s/\n' "${PARTICIPANT}"
  printf 'DASHBOARD_HOST=0.0.0.0\n'
  printf 'DASHBOARD_PORT=8000\n'
} > "${ENV_FILE}"
chown root:"${SERVICE_USER}" "${ENV_FILE}"
chmod 640 "${ENV_FILE}"

{
  printf 'PLAID_CLIENT_ID=%s\n' "${plaid_client_id}"
  printf 'PLAID_SECRET=%s\n' "${plaid_secret}"
  printf 'PLAID_ACCESS_TOKEN=%s\n' "${plaid_access_token}"
} > "${SYNC_ENV_FILE}"
chown root:root "${SYNC_ENV_FILE}"
chmod 600 "${SYNC_ENV_FILE}"

cp deploy/finance-dashboard.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now finance-dashboard.service

echo "Bootstrap done. Dashboard service is running on port 8000."
echo "Next: sudo bash deploy/sync-now.sh   (runs one Plaid sync into S3)"
