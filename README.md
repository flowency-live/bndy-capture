# bndy Capture

A lightweight Android share target and backlog queue for sending links, text and images into bndy for later processing by Claude and MCP.

## Included

- Native Android app named **Send to bndy**
- Android share-sheet support for text, URLs and images
- Local offline SQLite queue
- Optional delivery to the included TypeScript API
- Retry support
- SQLite-backed server backlog
- MCP server for Claude
- Docker Compose and GitHub Actions

## Structure

```text
android/   Native Android app
server/    Capture API and MCP server
```

## Server

```bash
cd server
cp .env.example .env
npm install
npm run dev
```

The API runs on `http://localhost:8787`.

```bash
curl -X POST http://localhost:8787/v1/captures \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer change-me' \
  -d '{"sharedText":"Fuzzy Duck https://facebook.com/example","sourceApp":"manual"}'
```

## Android

1. Open `android/` in Android Studio.
2. Let Gradle sync.
3. Build and install the debug app.
4. Open **Send to bndy** once and set the API URL and token.
5. Share from Facebook, Chrome or another app and select **Send to bndy**.

## MCP

```bash
cd server
npm install
npm run build
```

```json
{
  "mcpServers": {
    "bndy-capture": {
      "command": "node",
      "args": ["/absolute/path/to/bndy-capture/server/dist/mcp.js"],
      "env": {
        "BNDY_CAPTURE_DB": "/absolute/path/to/bndy-capture/server/data/captures.db"
      }
    }
  }
}
```

Tools:

- `list_bndy_captures`
- `get_bndy_capture`
- `update_bndy_capture_status`
- `add_bndy_capture_note`

## Security

Change `BNDY_CAPTURE_TOKEN` before exposing the service publicly and use HTTPS outside a local network.
