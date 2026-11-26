# Finance Dashboard Workshop: Steps

Web console only. The sync and the dashboard both run on one EC2 instance. Full instructions are in `WORKSHOP_GUIDE.md`.

## Participant steps (about 1 hour)

1. **Check the shared setup (5 min):** confirm the bucket, the four Parameter Store values, and the `finance-dashboard-readonly` role exist.
2. **Security group (5 min):** create `finance-dashboard-<your-name>` with inbound TCP 8000 from My IP only.
3. **Launch EC2 (10 min):** Amazon Linux 2023, `t3.micro`, no key pair, your security group, public IP enabled, and the `finance-dashboard-readonly` instance profile.
4. **Deploy (10 min):** open Session Manager, clone the repo, and run `sudo bash deploy/bootstrap.sh <your-name>`.
5. **Run the sync (5 min):** run `sudo bash deploy/sync-now.sh`, then confirm `transactions.csv` is in your `processed/` folder in S3.
6. **Verify (10 min):** run `curl -s http://127.0.0.1:8000/api/status` for `"state": "ok"`, then open `http://<public-ip>:8000`.
7. **Clean up (5 min):** terminate the instance and delete your security group.

## Optional: Terraform CLI version (20 min)

Build steps 2 and 3 with Terraform. The steps are in the appendix of `WORKSHOP_GUIDE.md`. Use it in a fresh account, since Terraform creates resources.

## Instructor setup (one time)

1. Check the bucket and its public access block.
2. Check `/finance-dashboard/bucket_name` (String) and the three `/finance-dashboard/plaid/...` values (SecureString).
3. Update the `finance-dashboard-readonly` policy with `deploy/iam-policy.json`, and confirm the role has `AmazonSSMManagedInstanceCore`.
4. Run steps 2 to 6 on a test instance, then terminate it.
