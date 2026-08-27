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
  description = "EventBridge schedule expression per bucket"
  value       = { for bucket, rule in aws_cloudwatch_event_rule.this : bucket => rule.schedule_expression }
}
