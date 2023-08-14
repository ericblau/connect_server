data "aws_caller_identity" "current" {}

# Create a namespace
locals {
  namespace = "MDF-Connect"
  envs = ["test", "prod"]
  environments = toset(local.envs)
  funcs = ["auth", "submit", "status"]
  functions = toset(local.funcs)
  account_id = data.aws_caller_identity.current.account_id
  region         = "us-east-1"

}

terraform {
  backend "s3" {
    # Replace this with your bucket name!
    bucket         = "accelerate-terraform-state-storage"
    key            = "terraform/MDF-Connect/terraform.tfstate"
    region         = "us-east-1"

    # Replace this with your DynamoDB table name!
    dynamodb_table = "accelerate-terraform-state-storage-locks"
    encrypt        = true
  }
}

# Create the Lambda execution role
resource "aws_iam_role" "lambda_execution" {
  name = "${local.namespace}-LambdaExecutionRole"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })
}

# Attach the necessary permissions policies to the role
resource "aws_iam_role_policy_attachment" "lambda_execution_permissions" {
  for_each = local.environments
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
  role       = aws_iam_role.lambda_execution.id
}

resource "aws_iam_role_policy" "sm_policy" {
  name = "sm_access_permissions"
  role = aws_iam_role.lambda_execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = [
          "secretsmanager:GetSecretValue",
        ]
        Effect   = "Allow"
        Resource = "*"
      },
    ]
  })
}