variable "function_name" {
  description = "Name of the Lambda function, its IAM role and its log group"
  type        = string
  default     = "bucket-archiver"
}

variable "archive_bucket_arns" {
  description = "Buckets the function may read, bundle and delete sources in"
  type        = list(string)
}

variable "artifact_bucket" {
  description = "Bucket holding the deployment package built by bin/deploy.sh"
  type        = string
}

variable "artifact_key" {
  description = "Key of the deployment package within artifact_bucket"
  type        = string
  default     = "bucket-archiver/function.zip"
}

variable "artifact_version" {
  description = "Version id of the package to deploy; null means current"
  type        = string
  default     = null
}

variable "schedules" {
  description = "Bucket name -> EventBridge cron. Stagger them; runs must not overlap."
  type        = map(string)
}

variable "max_concurrency" {
  description = "reserved_concurrent_executions; -1 leaves it unreserved"
  type        = number
  default     = -1
}

variable "log_retention_days" {
  type    = number
  default = 30
}
