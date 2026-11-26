# Finance Dashboard Workshop: AWS Web Console Steps

You only need a web browser. Everything happens in the AWS web console. The sync and the dashboard both run on one EC2 instance. There is no local install.

Use only sandbox data. Do not connect a real bank account.

## What you will build

```
Plaid sandbox  -->  EC2 instance  -->  S3 bucket  -->  EC2 instance  -->  your browser
(fake bank data)    sync-now.sh        participants/   client.py         port 8000
                    runs server.py     <your-name>/
```

- **EC2 instance:** a small Linux computer. It runs the sync (`server.py`) and the dashboard (`client.py`).
- **S3 bucket:** file storage. Your CSV is saved in your own folder.
- **Parameter Store:** holds the bucket name and the Plaid sandbox values. The instance reads them.
- **Session Manager:** a terminal inside the browser. You type a few commands there.

## Before you start

You need:

- The AWS account your instructor gave you, and its sign-in details.
- The workshop region, for example `us-east-1`.
- Your participant name, for example `alice`. Use lowercase letters, digits, and hyphens only.
- The repo link from your instructor.

### Accessing the AWS web console

1. Open the sign-in URL from your instructor. It looks like `https://<12-digit-account-id>.signin.aws.amazon.com/console`. If you only have the account ID, go to https://console.aws.amazon.com and choose **IAM user**.
2. Type your **IAM user name**, click **Next**, type the **password**, and click **Sign in**.
3. If AWS asks you to change the password, choose a new one and write it down. If it asks for an MFA code, type the six digits from your authenticator app.
4. Click the region name in the top-right corner and choose the workshop region. Every step uses that same region. If a resource seems missing, the wrong region is the usual cause.
5. Use the search bar at the top to find services such as **EC2**, **S3**, **Systems Manager**, and **IAM**.

**Done when:** you can open EC2 → Instances, S3 → Buckets, Systems Manager → Parameter Store, and IAM → Roles in the workshop region without an error.

If you see **You are not authorized to perform ...**, copy the full message and send it to your instructor. They add the missing permission to your IAM user.

---

## Step 1. Check the shared setup (5 min)

**Goal:** confirm the bucket, the secrets, and the role exist. You only look. You do not change anything.

1. In the search bar, type `S3` and click it. Find the workshop bucket. Click it, then open the **Permissions** tab. Confirm **Block all public access** is **On**.
2. In the search bar, type `Systems Manager`, click it, then in the left menu click **Parameter Store**. You should see four names:
   - `/finance-dashboard/bucket_name`
   - `/finance-dashboard/plaid/client_id`
   - `/finance-dashboard/plaid/secret`
   - `/finance-dashboard/plaid/access_token`
3. In the search bar, type `IAM`, click it, then click **Roles**. Search for `finance-dashboard`. You should see `finance-dashboard-readonly`.

**Done when:** you see the bucket, the four parameters, and the role. If one is missing, stop and ask the instructor.

---

## Step 2. Create a security group (5 min)

**Goal:** open port 8000 for your own IP address only.

1. In the search bar, type `EC2`, click it, then in the left menu under **Network & Security**, click **Security Groups**.
2. Click **Create security group**.
3. Fill in:
   - **Security group name:** `finance-dashboard-<your-name>`
   - **Description:** `Dashboard on port 8000 from my IP`
   - **VPC:** keep the default.
4. Under **Inbound rules**, click **Add rule**:
   - **Type:** Custom TCP
   - **Port range:** `8000`
   - **Source type:** **My IP**
5. Click **Create security group**.

Do not use `0.0.0.0/0`. That would open the dashboard to everyone.

**Done when:** the group is listed with an inbound rule for port 8000 from My IP.

---

## Step 3. Launch the EC2 instance (10 min)

**Goal:** start the computer that will run the sync and the dashboard.

1. In EC2, click **Instances**, then **Launch instances**.
2. Fill in:
   - **Name:** `finance-dashboard-<your-name>`
   - **Application and OS Images:** **Amazon Linux 2023**
   - **Instance type:** `t3.micro`. If your account is on the older free tier, `t2.micro` also works.
   - **Key pair (login):** **Proceed without a key pair**. You will use Session Manager.
3. Under **Network settings**, click **Edit**:
   - Choose the security group from step 2.
   - Make sure **Auto-assign public IP** is **Enable**.
4. Expand **Advanced details**. Set **IAM instance profile** to `finance-dashboard-readonly`.
5. Click **Launch instance**, then **View all instances**.
6. Wait until **Instance state** is **Running** and the status checks show **2/2 checks passed**. This takes about two minutes.

**Done when:** the instance shows Running and 2/2 checks passed.

---

## Step 4. Open the terminal and deploy (10 min)

**Goal:** install the app on the instance and start the dashboard service.

1. Select your instance. Click **Connect**, choose the **Session Manager** tab, then click **Connect**. A black terminal opens in the browser. Every command below is typed there.
2. Download the code:

   ```bash
   sudo dnf install -y git
   git clone https://github.com/MUNQuantSociety/AWS-Builder-Sessions.git finance-dashboard
   cd finance-dashboard
   ```

   If it asks `Is this ok [y/N]`, type `y` and press Enter.

3. Run the setup script. Replace `<your-name>`:

   ```bash
   sudo bash deploy/bootstrap.sh <your-name>
   ```

   This takes two to three minutes. It reads the bucket and the Plaid values from Parameter Store, then starts the dashboard service.

4. The last lines should say:

   ```
   Bootstrap done. Dashboard service is running on port 8000.
   Next: sudo bash deploy/sync-now.sh   (runs one Plaid sync into S3)
   ```

**If it fails:** read the last error line. A Parameter Store error means the role cannot read a secret, so check step 3 and ask the instructor. A `dnf` or `pip` error means the instance has no internet, so check that it has a public IP.

**Done when:** the script ends with `Bootstrap done`.

---

## Step 5. Run the sync (5 min)

**Goal:** the instance fetches the Plaid sandbox data and saves the CSV to your S3 folder.

1. In the same Session Manager terminal, run:

   ```bash
   sudo bash deploy/sync-now.sh
   ```

2. Wait for the last line. Expected output:

   ```
   INFO Stored N transactions from M pages
   Sync finished. Check: curl -s http://127.0.0.1:8000/api/status
   ```

   `N` is the number of transactions saved, and `M` is the number of pages the Plaid API returned. Both can differ from run to run. Every page is also stored under `raw/<date>/` in your folder.

3. Check the file in the console. Go to **S3**, open the workshop bucket, then open `participants`, then your name, then `processed`. You should see `transactions.csv`. Each sync replaces it, so click the file and read **Last modified** to see when it was written.

**If you see `Missing /etc/finance-dashboard/sync.env`:** step 4 did not finish. Run the bootstrap again.

**If you see AccessDenied:** the role is missing a permission. Ask the instructor to update the role's policy.

**Done when:** `transactions.csv` is in your `processed` folder.

---

## Step 6. Verify the dashboard (10 min)

**Goal:** check the status, then open the dashboard in your browser.

1. In the Session Manager terminal, run:

   ```bash
   curl -s http://127.0.0.1:8000/api/status
   ```

   Expected: JSON that includes `"state": "ok"`, the UTC `last_run_utc` time, and the row count.

2. In the EC2 console, select your instance and copy the **Public IPv4 address**.
3. Open a new browser tab and go to `http://<public-ip>:8000`. Use `http://`, not `https://`.
4. Check that the **Month** selector, the three cards (Income, Spending, Savings), and the charts show data. The status line should say `(state: ok)`. The dashboard keeps data for 60 seconds, so wait a minute and reload after a new sync.

**If the page does not load:** check that the security group allows port 8000 from your current IP. If your internet connection changed, edit the rule and choose **My IP** again.

**Done when:** the dashboard shows your data in the browser.

---

## Step 7. Clean up (5 min)

**Goal:** stop the instance so it does not keep costing money.

1. In the EC2 console, tick your instance. Click **Instance state**, then **Terminate instance**. Confirm.
2. Wait until the instance shows **Terminated**. This can take a few minutes. AWS will not delete a security group that an instance still uses.
3. Go to **Security Groups**, tick `finance-dashboard-<your-name>`, and choose **Actions → Delete security group**.

Do not delete the bucket, the parameters, or the shared role. The instructor manages those.

**Done when:** the instance is terminated and your security group is deleted.

---

## Appendix: CLI version with Terraform

**Goal:** build the same EC2 setup from code, using the Terraform files in the `terraform/` folder. This follows the setup steps of the HashiCorp AWS get-started tutorial: https://developer.hashicorp.com/terraform/tutorials/aws-get-started

**Important:** Terraform creates resources. The shared bucket, parameter, and role already exist in the workshop account, so use this appendix in a fresh account, or ask the instructor first. Running `apply` in the shared account would try to create duplicates.

The Terraform files create the bucket, the bucket-name parameter, the role and its policy, the security group, and the instance. They do not create the four Plaid values. Store those in Parameter Store first, as SecureString, using the console.

### A1. Install the tools (5 min)

1. Install Terraform 1.2 or newer, from the tutorial's install page.
2. Install the AWS CLI.
3. Check both:

   ```bash
   terraform -version
   aws --version
   ```

**Done when:** both print a version number.

### A2. Authenticate (2 min)

1. Set the access key from your instructor as environment variables.

   macOS or Linux:

   ```bash
   export AWS_ACCESS_KEY_ID=<your-access-key-id>
   export AWS_SECRET_ACCESS_KEY=<your-secret-access-key>
   ```

   Windows (PowerShell):

   ```powershell
   $env:AWS_ACCESS_KEY_ID="<your-access-key-id>"
   $env:AWS_SECRET_ACCESS_KEY="<your-secret-access-key>"
   ```

2. Check:

   ```bash
   aws configure list
   ```

**Done when:** the output shows your access key and region.

### A3. Create your values file (3 min)

1. Open the `terraform` folder and copy the example:

   ```bash
   cd terraform
   cp terraform.tfvars.example terraform.tfvars
   ```

   On Windows PowerShell, use `Copy-Item terraform.tfvars.example terraform.tfvars`.

2. Edit `terraform.tfvars` and set:
   - `bucket_name`: a unique name.
   - `participant_name`: your name.
   - `my_ip_cidr`: your IP with `/32`, for example `203.0.113.10/32`. Search "what is my IP" to find it.

**Done when:** the file has all three values.

### A4. Initialize and validate (3 min)

```bash
terraform init
terraform validate
```

Expected: `Success! The configuration is valid.`

**Done when:** validate passes.

### A5. Plan and apply (5 min)

1. Run `terraform apply`. Read the plan.
2. Type `yes`.
3. Copy the `public_ip` output.

**Done when:** apply finishes with no errors.

### A6. Deploy and sync (10 min)

1. Open the Session Manager terminal for the new instance, as in step 4 of the web console steps.
2. Run `git clone`, `sudo bash deploy/bootstrap.sh <your-name>`, then `sudo bash deploy/sync-now.sh`.
3. Check the CSV in S3 and the dashboard, as in steps 5 and 6.

**Done when:** the dashboard shows your data.

### A7. Clean up (2 min)

```bash
terraform destroy
```

Type `yes`. The bucket must be empty first. If destroy fails on the bucket, empty it in the S3 console, then run destroy again.
