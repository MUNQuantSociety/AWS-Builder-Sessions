# Personal Finance Dashboard (Sandbox Workshop)

A small Python project that shows monthly income, spending, savings, and spending by category. It uses fake bank data from the Plaid sandbox, stores it in Amazon S3, and serves a dashboard from an EC2 instance.

Use sandbox data only. Do not connect a real bank account.

## Start here

- **Participants:** follow [`WORKSHOP_GUIDE.md`](WORKSHOP_GUIDE.md). It takes you through the whole workshop in the AWS web console, with no local install.
- **Quick list of steps:** [`WORKSHOP_STEPS.md`](WORKSHOP_STEPS.md).
- **Full web-console steps and the Terraform CLI appendix:** [`WORKSHOP_WEB_CONSOLE.md`](WORKSHOP_WEB_CONSOLE.md).

## Accessing and completing the workshop in the AWS web console

Everything in the workshop runs in the AWS web console. You do not install anything on your computer.

1. **Sign in.** Open the sign-in URL from your instructor, and sign in as your IAM user with the account ID, user name, and password. Change the password if asked, and enter an MFA code if asked.
2. **Set the region.** Click the region name in the top-right corner and choose the workshop region, for example `us-east-1`. Use it for every step.
3. **Find services.** Use the search bar at the top, then click the service name.
4. **Follow the steps.** Work through [`WORKSHOP_GUIDE.md`](WORKSHOP_GUIDE.md) in order. Each step lists the console path, what to click, what to type in the terminal, and how to tell it worked.
5. **Use the browser terminal.** On the EC2 instance page, click **Connect**, choose **Session Manager**, then **Connect**. The terminal runs commands on the instance. Type the commands from the guide, one line at a time.
6. **Check your work.** Each step ends with a **Done when** check. If it fails, the guide's troubleshooting table shows the likely cause.
7. **Clean up.** Terminate the instance and delete your security group when you finish (step 7 of the guide).

### What your IAM user needs

Your user needs permission for each service the steps use. The instructor adds these:

| Service | Actions the steps use |
|---|---|
| EC2 | Describe, create a security group, launch an instance, terminate an instance |
| IAM | Pass the `finance-dashboard-readonly` role to EC2 |
| S3 | Browse the bucket and list the `participants/` prefix |
| Systems Manager | Start a Session Manager session on the instance |

If a step fails with `not authorized to perform iam:PassRole`, the role permission is missing. Send the full message to the instructor.

## How it works

```
Plaid sandbox  -->  EC2 instance  -->  S3 bucket  -->  EC2 instance  -->  browser
(fake bank data)    server.py          participants/   client.py         port 8000
                    (sync)             <name>/         (dashboard)
```

- **`server.py`** calls the Plaid sandbox, normalizes the transactions, and writes a CSV to S3. Run it with `deploy/sync-now.sh` on the instance. You can also run it locally with `--from-sample`, which needs no AWS access.
- **`client.py`** reads the CSV from S3 and serves the dashboard on port 8000. It runs as a systemd service.
- **`deploy/bootstrap.sh`** installs the app on the instance, reads its settings from Parameter Store, and starts the service.
- **`terraform/`** creates the bucket, the parameter, the role, the security group, and the instance, for a fresh account.

## Project layout

| Path | Purpose |
|---|---|
| `server.py` | Plaid sync: fetch pages, apply changes, write the CSV, record status |
| `client.py` | Dashboard: monthly summaries, category totals, SVG charts, HTTP routes |
| `tests/` | Unit tests for both programs. No AWS or network access needed |
| `sample_data/sync_pages.json` | Two Plaid-shaped sync pages for offline runs |
| `deploy/bootstrap.sh` | Installs the app, loads settings and Plaid values, starts the service |
| `deploy/sync-now.sh` | Runs one sync on the instance |
| `deploy/finance-dashboard.service` | systemd unit for `client.py` |
| `deploy/iam-policy.json` | The shared role's policy: read and write `participants/*`, read the parameters |
| `terraform/` | Terraform files for the same resources |

## Developer quick start (no AWS needed)

Python 3.10 or newer is required.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt pytest ruff

python server.py --from-sample sample_data/sync_pages.json
python client.py                # open http://127.0.0.1:8000
python -m pytest
ruff check .
```

The sample run writes to `./local_store/participants/demo/`. Expect 21 transactions and these monthly totals:

| Month | Income | Spending | Savings |
|---|---|---|---|
| 2026-09 | $5,000.00 | $1,347.39 | $3,652.61 |
| 2026-10 | $5,000.00 | $1,462.35 | $3,537.65 |
| 2026-11 | $3,140.00 | $1,392.10 | $1,747.90 |

## Configuration

Settings are environment variables. `.env.example` lists them.

| Variable | Used by | Meaning |
|---|---|---|
| `BUCKET_NAME` | both | S3 bucket. Leave empty to use `LOCAL_STORE_DIR` |
| `STORE_PREFIX` | both | Participant prefix, for example `participants/alice/` |
| `LOCAL_STORE_DIR` | both | Local folder when `BUCKET_NAME` is empty |
| `PLAID_CLIENT_ID`, `PLAID_SECRET`, `PLAID_ACCESS_TOKEN` | `server.py` | Plaid sandbox values |
| `CACHE_SECONDS` | `client.py` | How long to cache data, default 60 |
| `DASHBOARD_HOST`, `DASHBOARD_PORT` | `client.py` | Bind address (default `127.0.0.1`) and port (default `8000`) |

On EC2, `bootstrap.sh` fills these in from Parameter Store:

| Parameter | Type | Written to |
|---|---|---|
| `/finance-dashboard/bucket_name` | String | `/etc/finance-dashboard/env` (readable by the service user) |
| `/finance-dashboard/plaid/client_id` | SecureString | `/etc/finance-dashboard/sync.env` (root only) |
| `/finance-dashboard/plaid/secret` | SecureString | `/etc/finance-dashboard/sync.env` (root only) |
| `/finance-dashboard/plaid/access_token` | SecureString | `/etc/finance-dashboard/sync.env` (root only) |

## Instructor setup

The bucket, the parameters, and the shared role already exist. Check them, then update the policy:

1. Check the bucket exists and **Block all public access** is on.
2. Check `/finance-dashboard/bucket_name` (String), and the three `/finance-dashboard/plaid/...` values (SecureString).
3. Edit the `finance-dashboard-readonly` policy and replace its JSON with `deploy/iam-policy.json`, with `BUCKET_NAME`, `REGION`, and `ACCOUNT_ID` filled in.
4. Check the role has that policy and `AmazonSSMManagedInstanceCore`.
5. Test once: run steps 2 to 6 of the guide on a test instance, then terminate it.

## Security notes

- **Sandbox only.** `server.py` refuses any Plaid base URL other than `https://sandbox.plaid.com`.
- **Plaid values on EC2.** The sync runs on the instance, so the instance role can read the Plaid parameters. The values are written to `/etc/finance-dashboard/sync.env`, which only root can read. Use sandbox values only.
- **Role scope.** The role can read and write only `participants/*` in the bucket. It cannot list other prefixes.
- **Shared access.** Participants share one role, so they are separated by convention (their own `STORE_PREFIX`), not by permissions.
- **No login.** The dashboard has no login. The security group limits access to your IP. Do not open port 8000 to the internet.
- **Safe CSV.** Text starting with `=`, `+`, `-`, `@`, or `'` is escaped, so spreadsheets will not run it as a formula.
- **Safe HTML.** Every value in the page is HTML-escaped. Responses send a Content-Security-Policy and other security headers.

## Tests and checks

`python -m pytest` runs the unit tests. They cover the Plaid client (with a fake HTTP opener), ledger updates, CSV round trips, summaries, rendering, routing, caching, security headers, and the S3 store (with a fake client). `ruff check .` checks style.

These checks do not call Plaid, use a real bucket, or run the EC2 scripts. The workshop steps cover those.

## Taking it further

- Replace the shared sandbox account with Plaid Link, so each participant connects their own sandbox user.
- Schedule `deploy/sync-now.sh` with an EventBridge rule, so the data refreshes daily.
- Put the dashboard behind Amazon Cognito or an Application Load Balancer with HTTPS.
- Add a net-worth line from Plaid's `/accounts/balance/get`.
- Store the data in DynamoDB or Athena instead of a CSV file.
