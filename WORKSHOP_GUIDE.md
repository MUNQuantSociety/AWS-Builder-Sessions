# Finance Dashboard Workshop: Beginner Guide

You only need a web browser. Everything happens in the AWS web console. The sync and the dashboard both run on one EC2 instance, so there is nothing to install on your computer.

Use only sandbox data. Do not connect a real bank account.

## Words you will see

- **AWS console:** the website at https://console.aws.amazon.com where you click through AWS services.
- **Region:** the AWS data-center area, for example `us-east-1`. Every step must use the same region. Check the top-right corner of the console.
- **S3:** file storage. Files live in buckets (top-level folders) and prefixes (sub-folders).
- **Parameter Store:** AWS's place for settings and secrets. A SecureString is hidden until you click **Show**.
- **EC2:** a rented virtual computer. This guide calls it "the instance."
- **Security group:** a list of rules that decides which network traffic may reach the instance.
- **IAM role:** a set of permissions attached to the instance, so it can read and write the right things.
- **Session Manager:** a terminal inside the browser. You type commands into it instead of installing software.

## What you will build

```
Plaid sandbox  -->  EC2 instance  -->  S3 bucket  -->  EC2 instance  -->  your browser
(fake bank data)    sync-now.sh        participants/   client.py         port 8000
                    runs server.py     <your-name>/
```

- The instance runs `server.py`, which fetches fake transactions from the Plaid sandbox and saves a CSV to S3.
- The instance runs `client.py`, which reads that CSV and shows charts on port 8000.

## Before you start

You need:

- The AWS account your instructor gave you, and its sign-in details.
- The workshop region, for example `us-east-1`.
- Your participant name, for example `alice`. Use lowercase letters, digits, and hyphens only. You will type it several times.
- The repo link from your instructor.

## Access the AWS web console

Your instructor gives you three things: a **sign-in URL**, an **IAM user name**, and a **temporary password**. Keep them private.

### A1. Open the sign-in page

1. Open the sign-in URL from your instructor. It looks like `https://855603407903.signin.aws.amazon.com/console`.
2. If you were given only the account ID, go to https://console.aws.amazon.com, choose **IAM user**, and type the account ID or alias.

### A2. Sign in as the IAM user

1. Type your **IAM user name**, then click **Next**.
2. Type the **password**, then click **Sign in**.
3. If AWS asks you to change the password, choose a new one of at least 8 characters with a number and a symbol. Write it down somewhere safe.
4. If AWS asks for a verification code (MFA), open your authenticator app and type the six-digit code.

**If sign-in fails:** check the account ID and user name for typos. Ask the instructor to reset the password if you are locked out.

### A3. Set the region

1. Look at the top-right corner of the console. Click the region name.
2. Choose the workshop region, for example **US East (N. Virginia)** for `us-east-1`.

Use the same region for every step. If a resource seems missing, the wrong region is the most common reason.

### A4. Find services with the search bar

1. Click the search bar at the top of the page.
2. Type a service name, for example `EC2`, `S3`, `Systems Manager`, or `IAM`.
3. Click the service in the results.

### A5. Check your permissions

Your IAM user needs permission to use EC2, S3, Parameter Store, IAM roles, and Session Manager. If a step says **You are not authorized**, the message names the missing action. Send that message to the instructor, who can add the permission.

Before you start, confirm you can open each of these pages without an error:
- EC2 → Instances
- S3 → Buckets
- Systems Manager → Parameter Store
- IAM → Roles

**Done when:** all four pages open in the workshop region.


---

## Step 1. Check the shared setup (5 min)

**Goal:** confirm the bucket, the secrets, and the role exist. You only look. Change nothing.

### 1a. Check the bucket

1. In the search bar at the top, type `S3`, then click **S3**.
2. Click the workshop bucket.
3. Click the **Permissions** tab. Find **Block all public access**. It should say **On**.

### 1b. Check the secrets

1. In the search bar, type `Systems Manager`, then click it.
2. In the left menu, click **Parameter Store**.
3. You should see four names that start with `/finance-dashboard/`:
   - `/finance-dashboard/bucket_name`
   - `/finance-dashboard/plaid/client_id`
   - `/finance-dashboard/plaid/secret`
   - `/finance-dashboard/plaid/access_token`

You do not need to open the values. The instance reads them itself.

### 1c. Check the role

1. In the search bar, type `IAM`, then click it.
2. In the left menu, click **Roles**.
3. In the search box, type `finance-dashboard`. You should see `finance-dashboard-readonly`.

**Done when:** you see the bucket, all four parameters, and the role.

**If something is missing:** stop and ask the instructor. Do not create it yourself, because it must match the other workshop resources.

---

## Step 2. Create a security group (5 min)

**Goal:** open port 8000 for your own IP address only. A security group is a list of allowed doors. You open one door, for you only.

1. In the search bar, type `EC2`, then click it.
2. In the left menu, under **Network & Security**, click **Security Groups**.
3. Click **Create security group**.
4. Fill in the top section:
   - **Security group name:** `finance-dashboard-<your-name>`, for example `finance-dashboard-alice`
   - **Description:** `Dashboard on port 8000 from my IP`
   - **VPC:** keep the default that is already selected.
5. Scroll to **Inbound rules**. Click **Add rule**, then fill in:
   - **Type:** Custom TCP
   - **Port range:** `8000`
   - **Source type:** **My IP**. The console fills in your address.
6. Leave the outbound rules as they are.
7. Click **Create security group** at the bottom.

Do not set the source to `0.0.0.0/0`. That would open the dashboard to the whole internet.

**Done when:** the group appears in the list, and its inbound rule shows port 8000 with your IP.

---

## Step 3. Launch the EC2 instance (10 min)

**Goal:** start the computer that will run the sync and the dashboard.

1. In EC2, click **Instances** in the left menu, then **Launch instances**.
2. Fill in the top of the form:
   - **Name:** `finance-dashboard-<your-name>`
   - **Application and OS Images:** choose **Amazon Linux 2023**.
   - **Instance type:** choose `t3.micro`. If your account is on the older free tier, `t2.micro` also works.
   - **Key pair (login):** choose **Proceed without a key pair**. You will use Session Manager.
3. Under **Network settings**, click **Edit**:
   - Choose the security group from step 2.
   - Make sure **Auto-assign public IP** is **Enable**.
4. Expand **Advanced details**. Find **IAM instance profile** and choose `finance-dashboard-readonly`.
5. Click **Launch instance**, then click **View all instances**.
6. Wait until **Instance state** shows **Running** and the status checks show **2/2 checks passed**. This takes about two minutes. Click the refresh icon if the page does not update.

**Done when:** the instance shows Running and 2/2 checks passed.

---

## Step 4. Open the terminal and deploy (10 min)

**Goal:** install the app on the instance and start the dashboard service.

### 4a. Open a terminal on the instance

1. Click the checkbox next to your instance, then click **Connect**.
2. Choose the **Session Manager** tab, then click **Connect**.
3. A black terminal opens in a new browser tab. Every command below is typed there.

If you see "not managed" or cannot connect, wait a minute and try again. If it still fails, ask the instructor.

### 4b. Download the code

Type these commands, pressing Enter after each line:

```bash
sudo dnf install -y git
git clone https://github.com/MUNQuantSociety/AWS-Builder-Sessions.git finance-dashboard
cd finance-dashboard
```

If the install asks `Is this ok [y/N]`, type `y` and press Enter.

**Done when:** the prompt shows you are in `finance-dashboard`.

### 4c. Run the setup script

Replace `<your-name>` with your participant name:

```bash
sudo bash deploy/bootstrap.sh <your-name>
```

This takes two to three minutes and prints a lot of text. That is normal. It installs the app, reads the bucket name and the Plaid values from Parameter Store, and starts the dashboard service.

The last lines should say:

```
Bootstrap done. Dashboard service is running on port 8000.
Next: sudo bash deploy/sync-now.sh   (runs one Plaid sync into S3)
```

**If it stops with an error:**
- A Parameter Store error: the role cannot read a secret. Check step 3 and ask the instructor.
- A `dnf` or `pip` error: the instance cannot reach the internet. Check that it has a public IP.
- A usage message: your name has capital letters or spaces. Use lowercase letters, digits, and hyphens.

**Done when:** the script ends with `Bootstrap done`.

---

## Step 5. Run the sync (5 min)

**Goal:** the instance fetches the Plaid sandbox data and saves a CSV to your S3 folder.

1. In the same terminal, run:

   ```bash
   sudo bash deploy/sync-now.sh
   ```

2. Wait for the last line. Expected output:

   ```
   INFO Stored N transactions from M pages
   Sync finished. Check: curl -s http://127.0.0.1:8000/api/status
   ```

   `N` is the number of transactions saved, and `M` is the number of pages the Plaid API returned. Both numbers can differ from one run to another. The sync stores every page it fetches under `raw/<date>/` in your folder, then writes the combined result to `processed/transactions.csv`.

3. Check the file in the console:
   - In the search bar, type `S3`, then click it.
   - Click the workshop bucket.
   - Click `participants`, then your name, then `processed`.
   - You should see `transactions.csv`. The file name has no date, because each sync replaces it. To see when it was written, click the file and read **Last modified**.

**If you see `Missing /etc/finance-dashboard/sync.env`:** step 4c did not finish. Run the setup script again.

**If you see AccessDenied:** the role is missing a permission. Ask the instructor to update the policy.

**Done when:** `transactions.csv` is in your `processed` folder.

---

## Step 6. Verify the dashboard (10 min)

**Goal:** check the status, then open the dashboard in your browser.

### 6a. Check the status

In the terminal, run:

```bash
curl -s http://127.0.0.1:8000/api/status
```

Expected output: JSON that includes `"state": "ok"`, the UTC `last_run_utc` time, and the row count. Because `"state"` is `"ok"`, the sync worked.

**If you see `Failed to connect`:** the service is not running. Run `sudo systemctl status finance-dashboard --no-pager` and ask the instructor.

### 6b. Open the dashboard

1. Go back to the EC2 tab. Click your instance. In the details panel, copy the **Public IPv4 address**, for example `54.12.34.56`.
2. Open a new browser tab. Type `http://`, then the address, then `:8000`. For example: `http://54.12.34.56:8000`. Use `http://`, not `https://`.
3. Check that the **Month** selector, the three cards (Income, Spending, Savings), and the charts show your data. The header line under the title should say `Last sync ... (state: ok)`.

The dashboard keeps the data for 60 seconds. If the page shows old data after a new sync, wait a minute and reload.

**If the page does not load:** check that the security group allows port 8000 from your current IP. If your internet connection changed, edit the rule and choose **My IP** again.

**Done when:** the dashboard shows your data in the browser.

---

## Step 7. Clean up (5 min)

**Goal:** stop the instance so it does not keep costing money.

1. In the EC2 console, tick your instance. Click **Instance state**, then **Terminate instance**. Confirm.
2. Wait until the instance shows **Terminated**. This can take a few minutes. AWS will not delete a security group that an instance still uses.
3. Go to **Security Groups**, tick `finance-dashboard-<your-name>`, then choose **Actions → Delete security group**.

Do not delete the bucket, the parameters, or the shared role. The instructor manages those.

**Done when:** the instance is terminated and your security group is deleted.

---

## Optional: Terraform version (CLI)

Build the security group and the instance with code instead of clicking. The full CLI steps are in `WORKSHOP_WEB_CONSOLE.md`, in the appendix, and follow the setup steps of the HashiCorp AWS get-started tutorial: https://developer.hashicorp.com/terraform/tutorials/aws-get-started

Terraform creates resources. Use it in a fresh account, not the shared workshop account, or ask the instructor first.

---

## Troubleshooting

| What you see | What it means | What to do |
|---|---|---|
| The bootstrap stops with a Parameter Store error | The role cannot read a secret | Check step 3 and ask the instructor |
| The chart area says `No transactions yet.` | The dashboard read no rows | Check that `transactions.csv` exists (step 5), then reload |
| `Missing /etc/finance-dashboard/sync.env` | The setup script did not finish | Run step 4c again |
| AccessDenied | The role is missing a permission | Ask the instructor |
| `Failed to connect` from curl | The service is not running | Run `sudo systemctl status finance-dashboard --no-pager` |
| The browser cannot open the dashboard | Security group or IP | Check the port 8000 rule uses **My IP** |
| Session Manager will not connect | The role is missing the SSM policy | Ask the instructor |

To see service errors:

```bash
sudo journalctl -u finance-dashboard -n 50 --no-pager
```
