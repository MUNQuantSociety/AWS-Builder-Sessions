output "instance_id" {
  description = "EC2 instance ID. Use it to find the instance in the console."
  value       = aws_instance.dashboard.id
}

output "public_ip" {
  description = "Public IPv4 address of the dashboard instance."
  value       = aws_instance.dashboard.public_ip
}

output "dashboard_url" {
  description = "Open this in your browser after the bootstrap step."
  value       = "http://${aws_instance.dashboard.public_ip}:8000"
}

output "bucket_name" {
  description = "S3 bucket that holds participant prefixes."
  value       = aws_s3_bucket.data.bucket
}
