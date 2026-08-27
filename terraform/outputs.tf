output "function_arn" {
  value = aws_lambda_function.this.arn
}

output "function_name" {
  value = aws_lambda_function.this.function_name
}

output "role_arn" {
  value = aws_iam_role.this.arn
}

output "schedules" {
  description = "EventBridge schedule expressions per bucket"
  value = {
    for bucket, crons in var.schedules : bucket => [
      for key, schedule in local.schedules : aws_cloudwatch_event_rule.this[key].schedule_expression
      if schedule.bucket == bucket
    ]
  }
}
