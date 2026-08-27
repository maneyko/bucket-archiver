# Hardcoded settings
locals {
  lambda_function = {
    runtime = "python3.14"
    handler = "main.lambda_handler"

    architectures = ["arm64"]

    # Peak memory is roughly part_size plus one source object; the larger
    # allocation is for the proportionally larger network throughput.
    memory_size  = 1024 # MiB
    storage_size = 512  # MiB
  }
}

# The deployment package is an artifact in S3, built and uploaded by
# bin/deploy.sh from this repo. Terraform only *references* it, so a plan/apply
# does not require the source to be checked out.
data "aws_s3_object" "package" {
  bucket     = var.artifact_bucket
  key        = var.artifact_key
  version_id = var.artifact_version
}

resource "aws_iam_role" "this" {
  name        = var.function_name
  description = "S3 archiver Lambda: rolls small objects into DEEP_ARCHIVE tars"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Deliberately narrow: the function can read anything in an archive bucket, but
# may only write tars/manifests under "bucket-archive/", and the explicit Deny
# means no config error can ever make it delete a tar, a manifest or the config
# itself. Everything outside "bucket-archive/" is a source it may bundle and
# delete, so the code's prefix_pattern is what keeps deletes in bounds.
resource "aws_iam_role_policy" "this" {
  name = "${var.function_name}-access"
  role = aws_iam_role.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ListBucketAndMultipartUploads"
        Effect   = "Allow"
        Action   = ["s3:ListBucket", "s3:ListBucketMultipartUploads"]
        Resource = var.archive_bucket_arns
      },
      {
        Sid      = "ReadAnyObject"
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = [for arn in var.archive_bucket_arns : "${arn}/*"]
      },
      {
        Sid    = "WriteArchivesOnly"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:AbortMultipartUpload", "s3:ListMultipartUploadParts"]
        # "bucket-archive/me@example.com/INBOX/email/archive-000001.tar"
        Resource = [for arn in var.archive_bucket_arns : "${arn}/bucket-archive/*/archive-*"]
      },
      {
        Sid      = "DeleteSources"
        Effect   = "Allow"
        Action   = ["s3:DeleteObject"]
        Resource = [for arn in var.archive_bucket_arns : "${arn}/*"]
      },
      {
        Sid      = "NeverDeleteArchives"
        Effect   = "Deny"
        Action   = ["s3:DeleteObject"]
        Resource = [for arn in var.archive_bucket_arns : "${arn}/bucket-archive/*"]
      },
    ]
  })
}

resource "aws_iam_role_policy_attachment" "basic_execution" {
  role       = aws_iam_role.this.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

resource "aws_cloudwatch_log_group" "this" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days
}

resource "aws_lambda_function" "this" {
  function_name = var.function_name
  description   = "Rolls up many small objects into large tars under bucket-archive/"
  role          = aws_iam_role.this.arn

  s3_bucket         = var.artifact_bucket
  s3_key            = data.aws_s3_object.package.key
  s3_object_version = data.aws_s3_object.package.version_id

  runtime       = local.lambda_function.runtime
  handler       = local.lambda_function.handler
  architectures = local.lambda_function.architectures

  memory_size = local.lambda_function.memory_size
  timeout     = 900 # Maximum (15 minutes)

  reserved_concurrent_executions = var.max_concurrency

  ephemeral_storage {
    size = local.lambda_function.storage_size
  }
}

locals {
  # One rule per (bucket, schedule) pair. A bucket with an empty list gets no
  # rules at all, which is how archiving is turned off for it.
  schedules = merge([
    for bucket, crons in var.schedules : {
      for i, cron in crons : "${bucket}-${i + 1}" => { bucket = bucket, cron = cron }
    }
  ]...)
}

# Stagger the crons: a run may take the full 15 minutes and two invocations must
# never overlap on the same bucket.
resource "aws_cloudwatch_event_rule" "this" {
  for_each = local.schedules

  name                = "${var.function_name}-${each.key}"
  description         = "Roll-up small objects in ${each.value.bucket} into tars under bucket-archive/"
  schedule_expression = each.value.cron
}

resource "aws_cloudwatch_event_target" "this" {
  for_each = local.schedules

  rule      = aws_cloudwatch_event_rule.this[each.key].name
  target_id = var.function_name
  arn       = aws_lambda_function.this.arn

  # The bucket to work on. Everything else comes from that bucket's own config.
  input = jsonencode({ bucket = each.value.bucket })

  # "The bucket is the state": two runs would list the same objects and race to
  # bundle and delete them. A retry can overlap an invocation that is merely
  # slow, so never retry; whatever is left is simply picked up tomorrow.
  retry_policy {
    maximum_event_age_in_seconds = 3600
    maximum_retry_attempts       = 0
  }
}

resource "aws_lambda_permission" "events" {
  for_each = aws_cloudwatch_event_rule.this

  statement_id  = "AllowExecutionFromEventBridge-${each.key}"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.this.function_name
  principal     = "events.amazonaws.com"
  source_arn    = each.value.arn
}
