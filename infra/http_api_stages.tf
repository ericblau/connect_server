
# Define the AWS API Gateway v2 deployment for production
resource "aws_apigatewayv2_stage" "prod" {
  name        = "prod"
  api_id      = aws_apigatewayv2_api.http_api.id
  auto_deploy = true
  stage_variables = {
    auth_function = "MDF-Connect-auth-prod"
    submit_function = "MDF-Connect-submit-prod"
    status_function = "MDF-Connect-status-prod"
  }
  access_log_settings  {
    destination_arn = "${aws_cloudwatch_log_group.MDFConnect-logs.arn}"
    format =  "$context.identity.sourceIp $context.identity.caller $context.identity.user [$context.requestTime] $context.httpMethod $context.resourcePath $context.protocol $context.status $context.responseLength $context.requestId $context.extendedRequestId $context.authorizer.error $context.authorizer.principalId $context.authorizer.claims"
  }
}

# Define the AWS API Gateway v2 deployment for testing
resource "aws_apigatewayv2_stage" "test" {
  name        = "test"
  api_id      = aws_apigatewayv2_api.http_api.id
  auto_deploy = true
  stage_variables = {
    auth_function = "MDF-Connect-auth-test"
    submit_function = "MDF-Connect-submit-test"
    status_function = "MDF-Connect-status-test"
  }
  access_log_settings  {
    destination_arn = "${aws_cloudwatch_log_group.MDFConnect-logs.arn}"
    format =  "$context.extendedRequestId $context.identity.sourceIp $context.identity.caller $context.identity.user [$context.requestTime] $context.httpMethod $context.resourcePath $context.protocol $context.status $context.responseLength $context.requestId $context.extendedRequestId"
  }
}# Output the URLs for each environment
output "auth_url" {
  value = "${aws_apigatewayv2_stage.prod.invoke_url}/auth"
}

output "submit_dataset_url" {
  value = "${aws_apigatewayv2_stage.prod.invoke_url}/submit-dataset"
}

output "submission_status_url" {
  value = "${aws_apigatewayv2_stage.prod.invoke_url}/submission-status/{submission_id}"
}

output "auth_url_test" {
  value = "${aws_apigatewayv2_stage.test.invoke_url}/auth"
}

output "submit_dataset_url_test" {
  value = "${aws_apigatewayv2_stage.test.invoke_url}/submit-dataset"
}

output "submission_status_url_test" {
  value = "${aws_apigatewayv2_stage.test.invoke_url}/submission-status/{submission_id}"
}