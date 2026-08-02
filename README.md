# bndy Capture

An Android share target and durable backlog for sending Facebook profiles, websites, images and other discoveries into bndy for later processing by Claude and MCP.

## Production flow

```text
Android share sheet
  -> Send to bndy
  -> local offline queue
  -> https://capture.bndy.co.uk
  -> API Gateway
  -> Lambda
  -> DynamoDB
```

The Android app always writes locally first. If the phone is offline or AWS is unavailable, the item remains queued and can be retried.

## Repository

```text
android/   Native Android share-target app
aws/       Recommended AWS SAM production stack
server/    Local Express/SQLite API and MCP development server
```

## Set up locally

```bash
git clone https://github.com/flowency-live/bndy-capture.git
cd bndy-capture
code .
```

### Deploy AWS

The SAM stack creates:

- API Gateway HTTP API
- Lambda capture API
- DynamoDB backlog with point-in-time recovery
- ACM certificate for `capture.bndy.co.uk`
- API Gateway custom domain
- Route 53 alias record
- throttling and CloudWatch/X-Ray integration

```bash
cd aws
sam build
sam deploy --guided
```

See [`aws/README.md`](aws/README.md) for the exact parameters and verification commands.

### Build Android

Open `android/` in Android Studio or build from the command line:

```bash
cd android
./gradlew assembleDebug
```

After installing the APK, open **Send to bndy** and configure:

```text
API URL: https://capture.bndy.co.uk
Bearer token: the token used during SAM deployment
```

Then share from Facebook, Chrome or another Android app and select **Send to bndy**.

## Local server

The Express/SQLite implementation remains useful for local development:

```bash
cd server
cp .env.example .env
npm install
npm run dev
```

It implements the same `/v1/captures` contract as AWS.

## MCP

The included development MCP server currently reads the local SQLite backlog:

```bash
cd server
npm install
npm run build
npm run mcp
```

Tools:

- `list_bndy_captures`
- `get_bndy_capture`
- `update_bndy_capture_status`
- `add_bndy_capture_note`

A DynamoDB-backed MCP transport can be added after the ingestion path is deployed and verified.

## Security

- Use a randomly generated token of at least 32 characters.
- Never commit the token.
- The production Android endpoint is HTTPS-only.
- API Gateway throttling is enabled.
- DynamoDB data is encrypted and retained if the CloudFormation stack is removed.
