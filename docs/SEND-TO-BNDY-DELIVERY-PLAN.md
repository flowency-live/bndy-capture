# Send to bndy delivery plan

Status: approved for active delivery  
Owner: BNDY product and platform  
Date: 29 August 2026

## Outcome

Make sending a gig to BNDY feel as simple and trustworthy as messaging a friend.

People can send a poster, screenshot, image, public link or event text through either:

- `chat.bndy.live`, presented publicly as **Send to bndy**;
- a dedicated BNDY WhatsApp Business number.

Both transports create the same durable Capture record and enter the same Backline resolution path. Neither transport interprets events or writes canonical data independently.

## Fixed product decisions

1. Capture owns transport, durable receipt, idempotency and safe public status.
2. Backline owns interpretation, identity resolution, evidence, deduplication and canonical projection policy.
3. WhatsApp is an intake and response channel, not a chatbot or a second intelligence engine.
4. The public experience must be honest about uncertainty. It must never claim that a gig was added when resolution or projection failed.
5. Web and WhatsApp must return the same outcome vocabulary and canonical gig link when successful.
6. No generic conversation, comments, social posting or assistant persona is introduced.
7. The active Claim authority and Backline provider-qualification streams are not to be redesigned by this work.

## Audit of the live implementation

### `bndy-chatzone`

What already works:

- public text and image submission;
- drag, file-select and clipboard image intake;
- direct private S3 upload through a presigned form;
- polling of a sanitised public Capture status;
- successful gig details and a canonical `bndy.live` link.

Problems found:

- the product is still labelled `Signal Dropzone` and uses internal language such as `Interpret`;
- Capture IDs and implementation concepts are unnecessarily prominent;
- the desktop page is a sparse technical form rather than a BNDY product;
- the mobile experience is incidental rather than deliberately designed;
- images and supporting text cannot be submitted together;
- unsupported type and oversize failures are silently ignored;
- the UI accepts images up to 10 MB while the API accepts only 5 MB;
- a transient polling failure stops the journey immediately;
- processing cannot resume after refresh or accidental navigation;
- a long-running submission is shown as an error after three minutes even if it is still processing;
- uncertain and failed results provide no useful next action;
- the example text implies that plain text works reliably, but the current Backline scanner rejects text without a URL before interpretation.

### `bndy-capture`

What already works:

- HTTPS API Gateway and Lambda boundary;
- DynamoDB durability and point-in-time recovery;
- private S3 image evidence with expiry;
- public origin restriction, payload checks and IP rate limiting;
- sanitised public status rather than exposure of private notes;
- authenticated worker claim, status and note routes.

Problems found:

- there is no client or transport idempotency key;
- a retried web POST can create duplicate Capture records;
- the public result is reconstructed by parsing a human-readable worker note;
- all ambiguous, missing-data and exhausted-retry outcomes collapse into `could_not_resolve`;
- there is no public resume token beyond the Capture UUID;
- there is no WhatsApp webhook, signature verification, media retrieval, message-ID deduplication or reply path;
- the current public route accepts only `sourceApp=chatzone` records;
- the rate limit counts image reservation and Capture creation independently, which must be intentional and documented or replaced with per-submission idempotency.

### `bndy-enrichment` Capture processor

What already works:

- durable scanner and SQS worker separation;
- three-attempt retry behaviour;
- multimodal image processing;
- Google Search grounding;
- Facebook event URL handling;
- conservative artist matching and canonical event deduplication;
- final created or existing gig link returned through Capture.

Problems found:

- the scanner fails every text-only Capture that has no URL or stored image, even when `sharedText` contains complete gig facts;
- direct event resolution requires a complete Artist profile, including Facebook URL, location, Artist type and act type, before attempting to match an existing canonical Artist;
- a known existing Artist can therefore be blocked because facts needed only for creating a new Artist are absent;
- direct events without a start time are retried three times even when the missing fact is deterministic rather than transient;
- user-facing outcomes depend on regex parsing of a private note;
- a resolvable event that needs one human decision is presented publicly as a generic failure;
- current prompt and provider architecture is under a separate active qualification stream and must not be casually changed here.

## Target public experience

### Entry

The public page uses one clear proposition:

> Send a gig to bndy

Supporting copy:

> Drop a poster, screenshot, link or event message. We will work out the Artist, Venue, date and time, check whether the gig already exists, and show you the result.

The primary surface supports:

- image only;
- image plus optional supporting text;
- public link;
- plain event text.

The primary action is **Send to bndy**. The interface does not ask people to choose an AI or interpretation mode.

### Progress

Only real states are shown:

1. Received
2. Checking the gig
3. Added, already listed, needs help, not a gig, or could not complete

Progress wording may vary, but the UI must not invent intermediate model activity that the backend has not reported.

### Outcomes

| Public state | Meaning | Primary action |
|---|---|---|
| `processing` | Capture is durably received and still being worked | Keep checking automatically |
| `added` | A canonical gig was created | View gig |
| `already_exists` | The canonical gig already existed | View gig |
| `needs_review` | BNDY understood useful facts but cannot safely complete automatically | Explain what is missing and preserve the Capture |
| `ignored` | The submission is not recognised as a live-music event | Send something else |
| `could_not_resolve` | Processing failed without a safe partial result | Try again or add clearer context |

The web result keeps the Capture reference available behind a secondary details action, not as the visual headline.

### Resume and retry

- create a stable client submission ID before the first POST;
- make Capture creation idempotent for that transport submission ID;
- retain the current Capture ID in browser session storage;
- resume sanitised polling after refresh;
- use bounded backoff and tolerate transient status failures;
- after the normal polling window, show `Still checking` with a manual `Check again` action rather than declaring failure;
- `Send another` always resets local state cleanly.

## Shared Capture contract

The target record extends the existing schema without breaking Android or current workers:

```json
{
  "id": "stable UUID",
  "sourceApp": "chatzone | whatsapp | android",
  "transport": "web | whatsapp | android",
  "transportMessageId": "transport-specific idempotency key",
  "sharedText": "optional supporting text",
  "sharedUrl": "first public URL when present",
  "media": {
    "type": "image",
    "bucket": "private evidence bucket",
    "key": "private object key",
    "mimeType": "image/jpeg"
  },
  "status": "existing private worker status",
  "publicState": "safe public outcome",
  "publicMessage": "safe public explanation",
  "publicResult": {
    "artist": {},
    "event": {}
  },
  "publicIssue": {
    "code": "safe machine code",
    "missing": []
  }
}
```

Rules:

- private notes remain private;
- public status uses structured fields when present and retains current note parsing only as a compatibility fallback;
- phone numbers, tokens, raw Meta payloads and private worker reasons never enter the public response;
- idempotency is enforced server-side, not only in the client;
- existing Android and worker contracts continue to work.

## Backline reliability correction

The smallest safe Backline change is a separate, narrow PR rebased on the latest `bndy-enrichment` main:

1. Allow meaningful `sharedText` to pass the Capture scanner even without a URL or image.
2. Preserve image plus caption through the existing multimodal prompt.
3. Separate requirements for matching an existing Artist from requirements for creating a new Artist.
4. Attempt conservative canonical Artist resolution before blocking on new-Artist profile completeness.
5. Return a structured `needs_review` result when the gig is credible but safe creation needs missing identity or time information.
6. Keep all current authority, evidence and canonical-write controls.
7. Do not alter the active Gemini qualification adapter, provider evidence rules or Claim authority stream.

## WhatsApp transport

### Supported first release

- text messages;
- public links contained in text;
- image messages and their captions;
- one immediate receipt acknowledgement;
- one terminal result message with the canonical gig link when available.

Audio, video, location, contacts and arbitrary documents receive a concise unsupported-content response in the first release. They are not silently discarded.

### Webhook behaviour

1. `GET /v1/whatsapp/webhook` validates the configured verify token and returns `hub.challenge`.
2. `POST /v1/whatsapp/webhook` validates `X-Hub-Signature-256` against the exact raw body and Meta app secret.
3. Only `whatsapp_business_account` message notifications are accepted.
4. Meta message IDs are idempotency keys because webhook delivery is at least once.
5. Text and captions map to `sharedText`.
6. Image media is retrieved from Meta using the configured access token, validated and stored in the private Capture image bucket.
7. The webhook acknowledges quickly after durable receipt.
8. Reply work is decoupled from canonical projection so a WhatsApp delivery failure cannot replay a gig creation.
9. Logs contain Capture and Meta message IDs, but do not print message bodies, access tokens or full sender numbers.

### Secrets and production configuration

Use a dedicated Secrets Manager value containing:

- webhook verify token;
- Meta app secret;
- system-user or production access token;
- WhatsApp Business phone-number ID;
- approved Graph API version.

No secret value is committed to Git or exposed in CloudFormation outputs.

### Privacy

- store the minimum sender reference required to provide the result;
- keep it out of the public Capture response and canonical gig data;
- clear or separately expire the reply reference after the terminal response and operational retry window;
- update the public privacy notice before advertising the number;
- do not use submitted phone numbers for marketing or unrelated messaging.

## Repository ownership

| Capability | Repository |
|---|---|
| Web product and interaction | `flowency-live/bndy-chatzone` |
| Durable intake, public status, idempotency and WhatsApp webhook | `flowency-live/bndy-capture` |
| Interpretation, identity, evidence and canonical projection | `flowency-live/bndy-enrichment` |
| Public privacy/help copy and workboard | `flowency-live/bndy-website` |

## Delivery order

### Slice 1: web product

- rebuild the Chatzone interface as Send to bndy;
- support image plus context;
- align validation with the 5 MB API contract;
- add accessible, mobile-first progress, recovery and resume behaviour;
- keep the existing production endpoints compatible.

This slice can deploy independently.

### Slice 2: Capture reliability contract

- add idempotent transport submission IDs;
- add structured public state, result and safe issue fields;
- preserve current note parsing as fallback;
- add tests for duplicates, sanitisation and compatibility.

### Slice 3: narrow Backline correction

- allow text-only captures;
- distinguish matching requirements from new-Artist creation requirements;
- publish structured safe outcomes;
- rebase after the active Backline work and run the full suite.

### Slice 4: WhatsApp code and infrastructure

- implement verified webhook and media intake;
- add message-ID deduplication;
- add receipt and terminal response delivery;
- deploy disabled until production Meta secrets are configured.

### Slice 5: HITL Meta activation

The owner must choose and provision the public phone number and complete the production Meta binding. The recommended choice is a new dedicated UK number that is not attached to an existing personal WhatsApp account.

Required activation values:

- chosen public number;
- WhatsApp Business Account ID;
- phone-number ID;
- app secret;
- production/system-user access token;
- webhook subscription and display-name approval.

These values must be entered through Meta and AWS secret configuration, never pasted into source control or public chat.

### Slice 6: production acceptance

Run a bounded acceptance matrix:

1. clear poster for a new gig;
2. clear poster for an existing duplicate;
3. screenshot plus useful caption;
4. public Facebook event link;
5. plain event text with no URL;
6. ambiguous Artist identity;
7. missing time;
8. non-music image;
9. duplicate WhatsApp delivery;
10. temporary Meta media-download failure;
11. temporary Capture status failure;
12. page refresh during processing.

Acceptance requires:

- no duplicate Capture or canonical gig from retries;
- no false success;
- successful results contain the correct canonical gig link;
- ambiguity is preserved for review rather than guessed;
- web and WhatsApp use equivalent outcome language;
- no secret, phone number or private note appears in public output or logs;
- all repository CI and production health checks pass.

## Explicit non-goals

- a general BNDY chatbot;
- open-ended WhatsApp conversation;
- comments, direct messaging or social posting;
- a second interpretation engine inside Capture or Chatzone;
- changes to Backline's active provider-qualification architecture;
- changes to Claim V2 authority or canonical projection controls;
- automatic use of submitted phone numbers for marketing;
- support for every WhatsApp content type in the first release.

## Definition of done

Send to bndy is complete when a person can submit the same supported gig evidence through web or the public WhatsApp number, receive a durable acknowledgement, leave and resume, and get a truthful final result with the canonical gig link where safe, without knowing anything about Capture, Backline, models or claims.
