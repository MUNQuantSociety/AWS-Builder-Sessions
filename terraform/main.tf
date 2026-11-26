provider "aws" {
  region = var.region
}

# The latest Amazon Linux 2023 image, the same OS the README uses.
data "aws_ami" "al2023" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["al2023-ami-2023.*-x86_64"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }
}

# The default VPC, so the security group and instance have a network to live in.
data "aws_caller_identity" "current" {}

data "aws_vpc" "default" {
  default = true
}

# Shared data bucket. Public access is blocked.
resource "aws_s3_bucket" "data" {
  bucket = var.bucket_name
}

resource "aws_s3_bucket_public_access_block" "data" {
  bucket                  = aws_s3_bucket.data.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Parameter the bootstrap script reads to learn the bucket name.
resource "aws_ssm_parameter" "bucket_name" {
  name  = "/finance-dashboard/bucket_name"
  type  = "String"
  value = aws_s3_bucket.data.bucket
}

# Read-only access for the dashboard instance, as in deploy/iam-policy.json.
resource "aws_iam_policy" "readonly" {
  name = "finance-dashboard-readonly-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListParticipantPrefixesOnly"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.data.arn
        Condition = {
          StringLike = { "s3:prefix" = ["participants/*"] }
        }
      },
      {
        Sid      = "ReadWriteParticipantObjectsOnly"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.data.arn}/participants/*"
      },
      {
        Sid    = "ReadWorkshopParameters"
        Effect = "Allow"
        Action = "ssm:GetParameter"
        Resource = [
          aws_ssm_parameter.bucket_name.arn,
          "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/finance-dashboard/plaid/*"
        ]
      },
      {
        Sid      = "DecryptSecureStringsViaSsmOnly"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = "*"
        Condition = {
          StringEquals = { "kms:ViaService" = "ssm.${var.region}.amazonaws.com" }
        }
      }
    ]
  })
}

# Role the EC2 instance assumes.
resource "aws_iam_role" "dashboard" {
  name = "finance-dashboard-readonly"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "ec2.amazonaws.com" }
      }
    ]
  })
}

resource "aws_iam_role_policy_attachment" "readonly" {
  role       = aws_iam_role.dashboard.name
  policy_arn = aws_iam_policy.readonly.arn
}

# Lets you open a terminal with Session Manager instead of SSH.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.dashboard.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "dashboard" {
  name = "finance-dashboard-readonly"
  role = aws_iam_role.dashboard.name
}

# Port 8000 from your IP only. No SSH rule, because Session Manager is used.
resource "aws_security_group" "dashboard" {
  name        = "finance-dashboard-${var.participant_name}"
  description = "Dashboard on port 8000 from my IP"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Dashboard from my IP"
    from_port   = 8000
    to_port     = 8000
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  egress {
    description = "All outbound, for package installs and S3"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "dashboard" {
  ami                         = data.aws_ami.al2023.id
  instance_type               = var.instance_type
  iam_instance_profile        = aws_iam_instance_profile.dashboard.name
  vpc_security_group_ids      = [aws_security_group.dashboard.id]
  associate_public_ip_address = true

  tags = {
    Name = "finance-dashboard-${var.participant_name}"
  }
}
