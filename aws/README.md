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

## WhatsApp Send to bndy

The stack includes a disabled-by-default WhatsApp Cloud API transport. It uses the same Capture records and Backline processing path as the Android and web clients.

The production flow is:

1. Meta calls `GET /v1/whatsapp/webhook` once to verify the webhook.
2. Meta delivers signed message webhooks to `POST /v1/whatsapp/webhook`.
3. The API verifies `X-Hub-Signature-256` against the raw request body.
4. The message ID is recorded once and a minimal envelope is sent to SQS.
5. The worker downloads supported image media, stores it as Capture evidence and creates an idempotent Capture record.
6. The sender gets one acknowledgement and one bounded final result reply.

Duplicate webhook deliveries do not create duplicate Capture records. The durable dedupe record stores a hash of the sender identifier rather than the identifier itself. A separate reply record retains the recipient for at most 30 days and removes it after the final reply is sent.

### Production activation

Keep `WhatsAppEnabled=false` until all of these values are ready:

- A dedicated public BNDY number that is not attached to a personal WhatsApp account
- WhatsApp Business Account access for that number
- Meta phone number ID
- Permanent Cloud API access token
- Meta app secret
- A fresh webhook verification token

Deploy the existing stack with those values supplied as protected parameters:

```text
WhatsAppEnabled=true
WhatsAppVerifyToken=<protected value>
WhatsAppAppSecret=<protected value>
WhatsAppAccessToken=<protected value>
WhatsAppPhoneNumberId=<Meta phone number ID>
WhatsAppGraphVersion=v25.0
```

Do not write those values into `samconfig.toml`, shell history, GitHub comments or tracked files. Use an approved protected deployment channel. The stack stores the values in `bndy/whatsapp-service` and gives only the Capture functions permission to read that secret.

Configure Meta with this callback URL after deployment:

```text
https://capture.bndy.co.uk/v1/whatsapp/webhook
```

Before publishing the number, qualify text, public links, poster images, screenshots, duplicate delivery, oversized media, transient Graph API failure, added-gig replies, existing-gig replies and unresolved submissions.

## Removing

```bash
sam delete --stack-name bndy-capture --region eu-west-2
```

The DynamoDB table uses a retain policy, so deleting the stack does not delete captured data automatically.
