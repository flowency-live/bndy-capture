# AWS deployment

This deploys the production capture endpoint used by the Android app:

```text
capture.bndy.co.uk
  -> Route 53
  -> API Gateway HTTP API
  -> Lambda
  -> DynamoDB
```

## Prerequisites

- AWS CLI authenticated to the correct account
- AWS SAM CLI
- `bndy.co.uk` hosted in Route 53
- Permission to create IAM, Lambda, API Gateway, DynamoDB, ACM and Route 53 resources

Check access:

```bash
aws sts get-caller-identity
aws route53 list-hosted-zones-by-name --dns-name bndy.co.uk
```

Generate a private bearer token:

```bash
openssl rand -hex 32
```

Keep the token out of Git and password managers/shared notes where it is not needed.

## Deploy

From the repository root:

```bash
cd aws
sam build
sam deploy --guided
```

Recommended answers:

```text
Stack name: bndy-capture
Region: eu-west-2
Parameter CaptureToken: <generated token>
Parameter DomainName: capture.bndy.co.uk
Parameter HostedZoneId: <Route 53 hosted zone ID>
Parameter AllowedOrigin: *
Confirm changes before deploy: Y
Allow SAM CLI IAM role creation: Y
Disable rollback: N
Save arguments to configuration file: Y
```

The ACM certificate validation and Route 53 alias are created by the stack. The first deployment can pause while ACM validates the DNS record.

After deployment:

```bash
curl https://capture.bndy.co.uk/health

curl -X POST https://capture.bndy.co.uk/v1/captures \
  -H 'Authorization: Bearer YOUR_TOKEN' \
  -H 'Content-Type: application/json' \
  -d '{"sharedText":"Fuzzy Duck https://www.facebook.com/example","sourceApp":"manual"}'

curl https://capture.bndy.co.uk/v1/captures?status=unprocessed \
  -H 'Authorization: Bearer YOUR_TOKEN'
```

## Android setup

Open **Send to bndy** and enter:

```text
API URL: https://capture.bndy.co.uk
Bearer token: the same CaptureToken used during deployment
```

The Android client appends `/v1/captures` itself. Do not include that path in the API URL field.

The app stores captures locally first. It then attempts delivery over HTTPS. A failed upload remains on the phone and can be retried from the app.

## Updating

```bash
cd aws
sam build
sam deploy
```

## Removing

```bash
sam delete --stack-name bndy-capture --region eu-west-2
```

The DynamoDB table uses a retain policy, so deleting the stack does not delete captured data automatically.
