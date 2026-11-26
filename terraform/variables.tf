variable "region" {
  description = "AWS region for every resource in this stack."
  type        = string
  default     = "us-east-1"
}

variable "bucket_name" {
  description = "Globally unique S3 bucket name, for example finance-dashboard-workshop-alice-123."
  type        = string
}

variable "participant_name" {
  description = "Your participant name. Lowercase letters, digits, and hyphens. Used in names and the S3 prefix."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,39}$", var.participant_name))
    error_message = "participant_name must be lowercase letters, digits, and hyphens, up to 40 characters."
  }
}

variable "my_ip_cidr" {
  description = "Your public IP with /32, for example 203.0.113.10/32. Only this address can reach port 8000."
  type        = string

  validation {
    condition     = can(cidrhost(var.my_ip_cidr, 0)) && endswith(var.my_ip_cidr, "/32")
    error_message = "my_ip_cidr must be a single address ending in /32."
  }
}

variable "instance_type" {
  description = "EC2 instance size. t3.micro is the default in the README."
  type        = string
  default     = "t3.micro"
}
