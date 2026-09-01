import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
import urllib.error
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["CAPTURES_TABLE"]
CAPTURE_TOKEN = os.environ["CAPTURE_TOKEN"]
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
IMAGE_BUCKET = os.environ.get("IMAGE_BUCKET")
PUBLIC_ALLOWED_ORIGINS = {
    origin.strip().rstrip("/")
    for origin in os.environ.get("PUBLIC_ALLOWED_ORIGINS", "https://chat.bndy.live").split(",")
    if origin.strip()
}
PUBLIC_MAX_IMAGE_BYTES = int(os.environ.get("PUBLIC_MAX_IMAGE_BYTES", str(5 * 1024 * 1024)))
PUBLIC_MAX_TEXT_CHARS = int(os.environ.get("PUBLIC_MAX_TEXT_CHARS", "20000"))
PUBLIC_RATE_LIMIT = int(os.environ.get("PUBLIC_RATE_LIMIT", "20"))
PUBLIC_RATE_WINDOW_SECONDS = int(os.environ.get("PUBLIC_RATE_WINDOW_SECONDS", "600"))
FOLLOW_UP_TTL_DAYS = int(os.environ.get("FOLLOW_UP_TTL_DAYS", "30"))
WHATSAPP_ENABLED = os.environ.get("WHATSAPP_ENABLED", "false").lower() == "true"
WHATSAPP_QUEUE_URL = os.environ.get("WHATSAPP_QUEUE_URL", "")
WHATSAPP_SECRET_ARN = os.environ.get("WHATSAPP_SECRET_ARN", "")
WHATSAPP_GRAPH_VERSION = os.environ.get("WHATSAPP_GRAPH_VERSION", "v25.0")
WHATSAPP_FOLLOW_UP_TEMPLATE = os.environ.get("WHATSAPP_FOLLOW_UP_TEMPLATE", "")
CAPTURE_QUEUE_URL = os.environ.get("CAPTURE_QUEUE_URL", "")

TABLE = boto3.resource("dynamodb").Table(TABLE_NAME)
S3 = boto3.client("s3")
SQS = boto3.client("sqs")
SECRETS = boto3.client("secretsmanager")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VALID_STATUSES = {"unprocessed", "processing", "processed", "rejected", "failed", "ignored"}
VALID_ENTITY_TYPES = {"unknown", "venue", "artist", "event"}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
PUBLIC_IMAGE_PREFIX = "captures/public/"
WHATSAPP_IMAGE_PREFIX = "captures/whatsapp/"
TRANSPORT_ID_NAMESPACE = uuid.UUID("9413f742-72cc-47e9-847e-4260653760cb")
PUBLIC_TERMINAL_STATES = {"added", "already_exists", "processed", "needs_review", "could_not_resolve", "ignored"}
_WHATSAPP_CONFIG: dict[str, str] | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_default(value: Any):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Unsupported value: {type(value)!r}")


def response(status: int, body: Any, extra_headers: dict[str, str] | None = None):
    headers = {
        "content-type": "application/json",
        "access-control-allow-origin": ALLOWED_ORIGIN,
        "cache-control": "no-store",
    }
    if extra_headers:
        headers.update(extra_headers)
    return {
        "statusCode": status,
        "headers": headers,
        "body": json.dumps(body, default=json_default),
    }


def parse_body(event: dict) -> dict:
    value = json.loads(raw_body(event).decode("utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def raw_body(event: dict) -> bytes:
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        return base64.b64decode(raw)
    return str(raw).encode("utf-8")


def headers(event: dict) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


def transport_capture_id(transport: str, message_id: str) -> str:
    return str(uuid.uuid5(TRANSPORT_ID_NAMESPACE, f"{transport}:{message_id}"))


def get_whatsapp_config() -> dict[str, str] | None:
    global _WHATSAPP_CONFIG
    if not WHATSAPP_ENABLED or not WHATSAPP_SECRET_ARN:
        return None
    if _WHATSAPP_CONFIG is not None:
        return _WHATSAPP_CONFIG

    secret = SECRETS.get_secret_value(SecretId=WHATSAPP_SECRET_ARN)
    value = json.loads(secret.get("SecretString") or "{}")
    required = {"verifyToken", "appSecret", "accessToken", "phoneNumberId"}
    if not isinstance(value, dict) or any(not value.get(key) or value.get(key) == "disabled" for key in required):
        raise RuntimeError("WhatsApp is enabled but its production secret is incomplete")
    _WHATSAPP_CONFIG = {key: str(value[key]) for key in required}
    return _WHATSAPP_CONFIG


def verify_whatsapp_signature(body: bytes, signature: str | None, app_secret: str) -> bool:
    if not signature or not signature.startswith("sha256="):
        return False
    expected = hmac.new(app_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return secrets.compare_digest(signature[7:], expected)


def authorised(event: dict) -> bool:
    supplied = headers(event).get("authorization", "")
    expected = f"Bearer {CAPTURE_TOKEN}"
    return secrets.compare_digest(supplied, expected)


def public_origin_allowed(event: dict) -> bool:
    origin = headers(event).get("origin", "").rstrip("/")
    return bool(origin and origin in PUBLIC_ALLOWED_ORIGINS)


def first_url(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip("),.;!?") if match else None


def validate_media(value: Any, *, public_only: bool = False) -> tuple[dict | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "media must be an object"
    if value.get("type") != "image":
        return None, "Only image media is currently supported"
    bucket = value.get("bucket")
    key = value.get("key")
    mime_type = value.get("mimeType")
    if not bucket or bucket != IMAGE_BUCKET:
        return None, "Invalid image bucket"
    required_prefix = PUBLIC_IMAGE_PREFIX if public_only else "captures/"
    if not isinstance(key, str) or not key.startswith(required_prefix):
        return None, "Invalid image key"
    if mime_type not in ALLOWED_IMAGE_TYPES:
        return None, "Unsupported image mimeType"
    media = {
        "type": "image",
        "bucket": bucket,
        "key": key,
        "mimeType": mime_type,
    }
    if isinstance(value.get("size"), int) and value["size"] >= 0:
        media["size"] = value["size"]
    if isinstance(value.get("originalName"), str) and value["originalName"].strip():
        media["originalName"] = value["originalName"].strip()[:255]
    return media, None


def validate_create(body: dict) -> tuple[dict | None, str | None]:
    shared_text = body.get("sharedText")
    shared_url = body.get("sharedUrl") or first_url(shared_text)
    raw_payload = body.get("rawPayload")
    media, media_error = validate_media(body.get("media"))
    if media_error:
        return None, media_error

    if shared_text is not None and not isinstance(shared_text, str):
        return None, "sharedText must be a string"
    if shared_url is not None and not isinstance(shared_url, str):
        return None, "sharedUrl must be a string"
    if not shared_text and not shared_url and not raw_payload and not media:
        return None, "At least one shared value is required"

    entity_type = body.get("suggestedEntityType", "unknown")
    if entity_type not in VALID_ENTITY_TYPES:
        return None, "Invalid suggestedEntityType"

    now = now_iso()
    item = {
        "id": str(uuid.uuid4()),
        "capturedAt": body.get("capturedAt") or now,
        "receivedAt": now,
        "updatedAt": now,
        "sharedText": shared_text,
        "sharedUrl": shared_url,
        "mimeType": body.get("mimeType") or (media.get("mimeType") if media else "text/plain"),
        "sourceApp": body.get("sourceApp"),
        "note": body.get("note"),
        "suggestedEntityType": entity_type,
        "status": "unprocessed",
        "rawPayload": raw_payload,
        "media": media,
    }
    return {k: v for k, v in item.items() if v is not None}, None


def validate_public_capture(body: dict) -> tuple[dict | None, str | None]:
    shared_text = body.get("sharedText")
    if shared_text is not None:
        if not isinstance(shared_text, str):
            return None, "sharedText must be a string"
        shared_text = shared_text.strip()
        if len(shared_text) > PUBLIC_MAX_TEXT_CHARS:
            return None, f"sharedText exceeds {PUBLIC_MAX_TEXT_CHARS} characters"

    media, media_error = validate_media(body.get("media"), public_only=True)
    if media_error:
        return None, media_error
    if not shared_text and not media:
        return None, "A poster image or event text is required"

    client_submission_id = body.get("clientSubmissionId")
    if client_submission_id is not None:
        if not isinstance(client_submission_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{8,128}", client_submission_id):
            return None, "clientSubmissionId must be 8 to 128 safe characters"

    if media:
        try:
            head = S3.head_object(Bucket=media["bucket"], Key=media["key"])
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in {"404", "NoSuchKey", "NotFound"}:
                return None, "Uploaded image was not found"
            raise
        actual_size = int(head.get("ContentLength", 0))
        actual_type = str(head.get("ContentType") or "")
        if actual_size <= 0 or actual_size > PUBLIC_MAX_IMAGE_BYTES:
            return None, "Uploaded image is empty or too large"
        if actual_type != media["mimeType"]:
            return None, "Uploaded image content type does not match"
        media["size"] = actual_size

    now = now_iso()
    item = {
        "id": transport_capture_id("web", client_submission_id) if client_submission_id else str(uuid.uuid4()),
        "capturedAt": now,
        "receivedAt": now,
        "updatedAt": now,
        "sharedText": shared_text or None,
        "sharedUrl": first_url(shared_text),
        "mimeType": media.get("mimeType") if media else "text/plain",
        "sourceApp": "chatzone",
        "suggestedEntityType": "event",
        "status": "unprocessed",
        "rawPayload": {"transport": "web_dropzone", "public": True},
        "media": media,
        "transportIdempotencyKey": f"web:{client_submission_id}" if client_submission_id else None,
    }
    return {k: v for k, v in item.items() if v is not None}, None


def get_capture(capture_id: str):
    return TABLE.get_item(Key={"id": capture_id}, ConsistentRead=True).get("Item")


def queue_capture_for_processing(capture_id: str):
    """Dispatch a newly-created Capture immediately when Backline is connected.

    The periodic scanner remains the recovery path. The processor owns the atomic
    claim, so a scanner message racing this one cannot process the Capture twice.
    """
    if not CAPTURE_QUEUE_URL:
        return
    try:
        SQS.send_message(
            QueueUrl=CAPTURE_QUEUE_URL,
            MessageBody=json.dumps({"captureId": capture_id}),
        )
        return True
    except Exception as exc:
        # The durable record is the source of truth and the periodic scanner is
        # the recovery path. A dispatch outage must not turn a received Capture
        # into a false submission failure.
        print(json.dumps({"captureDispatch": "deferred_to_scanner", "captureId": capture_id, "error": type(exc).__name__}))
        return False


def follow_up_key(capture_id: str) -> str:
    return f"followup#{capture_id}"


def get_capture_follow_up(capture_id: str) -> dict | None:
    return TABLE.get_item(Key={"id": follow_up_key(capture_id)}, ConsistentRead=True).get("Item")


def normalise_follow_up_contact(method: str, value: str) -> str | None:
    candidate = value.strip()
    if method == "email":
        if len(candidate) > 254 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", candidate):
            return None
        return candidate.lower()
    if method == "whatsapp":
        digits = re.sub(r"[^0-9+]", "", candidate)
        if digits.startswith("00"):
            digits = f"+{digits[2:]}"
        elif digits.startswith("07"):
            digits = f"+44{digits[1:]}"
        elif not digits.startswith("+"):
            digits = f"+{digits}"
        return digits if re.fullmatch(r"\+[1-9][0-9]{7,14}", digits) else None
    return None


def save_public_follow_up(capture_id: str, body: dict):
    capture = get_capture(capture_id)
    if not capture or capture.get("sourceApp") != "chatzone":
        return response(404, {"error": "not_found"})
    if public_capture_view(capture)["state"] != "needs_review":
        return response(409, {"error": "not_reviewable", "message": "This submission does not need follow-up."})

    method = str(body.get("method") or "").lower()
    raw_value = body.get("value")
    contact = normalise_follow_up_contact(method, raw_value) if isinstance(raw_value, str) else None
    if not contact:
        return response(400, {"error": "invalid_contact", "message": "Enter a valid email address or WhatsApp number."})
    if body.get("consent") is not True:
        return response(400, {"error": "consent_required", "message": "Consent is required for this submission update."})

    now = now_iso()
    try:
        TABLE.put_item(Item={
            "id": follow_up_key(capture_id),
            "recordType": "capture_follow_up",
            "captureId": capture_id,
            "method": method,
            "contact": contact,
            "consentedAt": now,
            "updatedAt": now,
            "notificationStatus": "pending",
            "expiresAt": int(time.time()) + FOLLOW_UP_TTL_DAYS * 24 * 60 * 60,
        }, ConditionExpression="attribute_not_exists(id)")
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            existing = get_capture_follow_up(capture_id)
            if existing and existing.get("method") == method and existing.get("contact") == contact:
                return response(200, {"saved": True, "method": method})
            return response(409, {"error": "follow_up_already_saved", "message": "Contact details are already saved for this submission."})
        raise
    return response(200, {"saved": True, "method": method})


def capture_media_view(capture: dict) -> dict:
    media = capture.get("media") or {}
    bucket = media.get("bucket")
    key = media.get("key")
    if not bucket or not key:
        return {"available": False}
    return {
        "available": True,
        "type": media.get("type"),
        "mimeType": media.get("mimeType"),
        "originalName": media.get("originalName"),
        "url": S3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=600,
        ),
        "expiresIn": 600,
    }


def append_capture_note(capture_id: str, note: str):
    current = get_capture(capture_id)
    combined = f"{current.get('note')}\n\n{note}" if current and current.get("note") else note
    TABLE.update_item(
        Key={"id": capture_id},
        UpdateExpression="SET note = :note, updatedAt = :updatedAt",
        ExpressionAttributeValues={":note": combined, ":updatedAt": now_iso()},
    )


def review_capture(capture_id: str, body: dict):
    capture = get_capture(capture_id)
    if not capture:
        return response(404, {"error": "not_found"})
    if public_capture_view(capture)["state"] != "needs_review":
        return response(409, {"error": "not_reviewable"})
    action = body.get("action")
    reviewer = str(body.get("reviewer") or "Godmode reviewer")[:200]
    review_note = str(body.get("note") or "").strip()[:2000]

    if action == "retry":
        if not review_note:
            return response(400, {"error": "review_context_required"})
        now = now_iso()
        TABLE.update_item(
            Key={"id": capture_id},
            UpdateExpression=(
                "SET #status = :unprocessed, reviewContext = :context, reviewedBy = :reviewer, "
                "reviewedAt = :reviewedAt, updatedAt = :updatedAt "
                "REMOVE publicOutcome, processingWorkerId, processingStartedAt, leaseUntil"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":unprocessed": "unprocessed",
                ":context": review_note,
                ":reviewer": reviewer,
                ":reviewedAt": now,
                ":updatedAt": now,
            },
            ConditionExpression="attribute_exists(id)",
        )
        append_capture_note(capture_id, f"Human review: retry requested by {reviewer}. Context: {review_note}")
        queue_capture_for_processing(capture_id)
        return response(200, public_capture_view(get_capture(capture_id)))

    if action == "resolve":
        public_outcome, outcome_error = validate_public_outcome(body.get("publicOutcome"))
        if outcome_error:
            return response(400, {"error": "invalid_public_outcome", "message": outcome_error})
        if not public_outcome or public_outcome["state"] not in {"added", "already_exists", "could_not_resolve", "ignored"}:
            return response(400, {"error": "invalid_resolution_state"})
        status = "processed" if public_outcome["state"] in {"added", "already_exists"} else (
            "ignored" if public_outcome["state"] == "ignored" else "failed"
        )
        now = now_iso()
        TABLE.update_item(
            Key={"id": capture_id},
            UpdateExpression=(
                "SET #status = :status, publicOutcome = :outcome, reviewedBy = :reviewer, "
                "reviewedAt = :reviewedAt, updatedAt = :updatedAt"
            ),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":status": status,
                ":outcome": public_outcome,
                ":reviewer": reviewer,
                ":reviewedAt": now,
                ":updatedAt": now,
            },
            ConditionExpression="attribute_exists(id)",
        )
        append_capture_note(capture_id, f"Human review: resolved as {public_outcome['state']} by {reviewer}. {review_note}".strip())
        return response(200, {
            **public_capture_view(get_capture(capture_id)),
            "followUp": get_capture_follow_up(capture_id),
        })

    return response(400, {"error": "invalid_review_action"})


def create_image_upload(body: dict):
    if not IMAGE_BUCKET:
        return response(503, {"error": "image_storage_unavailable"})
    mime_type = body.get("mimeType")
    if mime_type not in ALLOWED_IMAGE_TYPES:
        return response(400, {"error": "unsupported_image_type", "allowed": sorted(ALLOWED_IMAGE_TYPES)})
    suffix = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }[mime_type]
    key = f"captures/{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{uuid.uuid4()}.{suffix}"
    upload_url = S3.generate_presigned_url(
        "put_object",
        Params={"Bucket": IMAGE_BUCKET, "Key": key, "ContentType": mime_type},
        ExpiresIn=900,
    )
    return response(200, {
        "uploadUrl": upload_url,
        "bucket": IMAGE_BUCKET,
        "key": key,
        "mimeType": mime_type,
        "expiresIn": 900,
    })


def enforce_public_rate_limit(event: dict) -> bool:
    source_ip = str(event.get("requestContext", {}).get("http", {}).get("sourceIp") or "unknown")
    window = int(time.time()) // PUBLIC_RATE_WINDOW_SECONDS
    digest = hashlib.sha256(f"{source_ip}:{window}".encode("utf-8")).hexdigest()[:32]
    key = f"rate#{digest}"
    expires_at = int(time.time()) + PUBLIC_RATE_WINDOW_SECONDS * 2
    try:
        TABLE.update_item(
            Key={"id": key},
            UpdateExpression="SET recordType = :recordType, expiresAt = :expiresAt ADD requestCount :one",
            ExpressionAttributeValues={
                ":recordType": "rate_limit",
                ":expiresAt": expires_at,
                ":one": 1,
                ":limit": PUBLIC_RATE_LIMIT,
            },
            ConditionExpression="attribute_not_exists(requestCount) OR requestCount < :limit",
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise


def create_public_image_upload(event: dict, body: dict):
    if not enforce_public_rate_limit(event):
        return response(429, {"error": "rate_limited", "message": "Too many submissions. Please try again shortly."})
    if not IMAGE_BUCKET:
        return response(503, {"error": "image_storage_unavailable"})

    mime_type = body.get("mimeType")
    file_name = body.get("fileName")
    size = body.get("size")
    if mime_type not in ALLOWED_IMAGE_TYPES:
        return response(400, {"error": "unsupported_image_type", "allowed": sorted(ALLOWED_IMAGE_TYPES)})
    if not isinstance(size, int) or size <= 0 or size > PUBLIC_MAX_IMAGE_BYTES:
        return response(400, {"error": "invalid_image_size", "maxBytes": PUBLIC_MAX_IMAGE_BYTES})

    suffix = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }[mime_type]
    key = f"{PUBLIC_IMAGE_PREFIX}{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{uuid.uuid4()}.{suffix}"
    post = S3.generate_presigned_post(
        Bucket=IMAGE_BUCKET,
        Key=key,
        Fields={"Content-Type": mime_type},
        Conditions=[
            {"Content-Type": mime_type},
            ["content-length-range", 1, PUBLIC_MAX_IMAGE_BYTES],
        ],
        ExpiresIn=600,
    )
    return response(200, {
        "uploadUrl": post["url"],
        "fields": post["fields"],
        "media": {
            "type": "image",
            "bucket": IMAGE_BUCKET,
            "key": key,
            "mimeType": mime_type,
            "size": size,
            **({"originalName": file_name.strip()[:255]} if isinstance(file_name, str) and file_name.strip() else {}),
        },
        "expiresIn": 600,
    })


def public_result_from_note(note: str) -> dict | None:
    artist_match = re.search(r"^Artist:\s*(.+?)\s*\|\s*([^|]+?)\s*\|\s*([0-9a-f-]{36})\s*$", note, re.MULTILINE | re.IGNORECASE)
    event_match = re.search(
        r"^-\s*(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2})\s*\|\s*(.+?)\s*\|\s*(created|existing duplicate)\s+([0-9a-f-]{36})\s*\|\s*(.+?)\s*$",
        note,
        re.MULTILINE | re.IGNORECASE,
    )
    if not event_match:
        return None

    event_id = event_match.group(5)
    result = {
        "event": {
            "id": event_id,
            "date": event_match.group(1),
            "time": event_match.group(2),
            "venue": event_match.group(3).strip(),
            "action": "created" if event_match.group(4).lower() == "created" else "existing",
            "venueAction": event_match.group(6).strip(),
            "url": f"https://bndy.live/g/{event_id}",
        }
    }
    if artist_match:
        result["artist"] = {
            "name": artist_match.group(1).strip(),
            "action": artist_match.group(2).strip(),
            "id": artist_match.group(3),
        }
    return result


def sanitise_public_event(value: Any, field_name: str) -> tuple[dict | None, str | None]:
    if not isinstance(value, dict):
        return None, f"publicOutcome.result.{field_name} is invalid"
    required = ("id", "date", "time", "venue", "url")
    if any(not isinstance(value.get(key), str) for key in required):
        return None, f"publicOutcome.result.{field_name} is incomplete"
    return {
        key: value[key]
        for key in (*required, "action", "venueAction")
        if isinstance(value.get(key), str) and len(value[key]) <= 1000
    }, None


def sanitise_public_result(value: Any) -> tuple[dict | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "publicOutcome.result must be an object"

    result: dict[str, Any] = {}
    artist = value.get("artist")
    if artist is not None:
        if not isinstance(artist, dict) or not isinstance(artist.get("name"), str):
            return None, "publicOutcome.result.artist is invalid"
        result["artist"] = {
            key: artist[key]
            for key in ("name", "action", "id")
            if isinstance(artist.get(key), str) and len(artist[key]) <= 500
        }

    event = value.get("event")
    if event is not None:
        public_event, event_error = sanitise_public_event(event, "event")
        if event_error:
            return None, event_error
        result["event"] = public_event

    events = value.get("events")
    if events is not None:
        if not isinstance(events, list) or not events or len(events) > 100:
            return None, "publicOutcome.result.events must contain 1 to 100 events"
        public_events = []
        for index, item in enumerate(events):
            public_event, event_error = sanitise_public_event(item, f"events[{index}]")
            if event_error:
                return None, event_error
            public_events.append(public_event)
        result["events"] = public_events

    return result or None, None


def validate_public_outcome(value: Any) -> tuple[dict | None, str | None]:
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "publicOutcome must be an object"

    state = value.get("state")
    if state not in PUBLIC_TERMINAL_STATES:
        return None, "publicOutcome.state is invalid"
    message = value.get("message")
    if message is not None and (not isinstance(message, str) or len(message) > 500):
        return None, "publicOutcome.message must be a string up to 500 characters"

    result, result_error = sanitise_public_result(value.get("result"))
    if result_error:
        return None, result_error

    outcome = {"state": state}
    if message:
        outcome["message"] = message.strip()
    if result:
        outcome["result"] = result
    return outcome, None


def public_capture_view(item: dict) -> dict:
    status = item.get("status", "unprocessed")
    state = "processing"
    message = "BNDY is processing your submission."
    result = None

    structured_outcome, _ = validate_public_outcome(item.get("publicOutcome"))
    if structured_outcome:
        state = structured_outcome["state"]
        message = structured_outcome.get("message") or {
            "added": "Added to bndy.",
            "already_exists": "This event is already in bndy.",
            "processed": "Processed by bndy.",
            "needs_review": "BNDY found useful details, but this submission needs a human check.",
            "could_not_resolve": "BNDY could not resolve this submission automatically.",
            "ignored": "This submission was not recognised as a live music event.",
        }[state]
        result = structured_outcome.get("result")

    elif status == "processed":
        note = str(item.get("note") or "")
        result = public_result_from_note(note)
        counts = re.search(r"Events:\s*(\d+) created,\s*(\d+) existing duplicates", note)
        created = int(counts.group(1)) if counts else 0
        duplicates = int(counts.group(2)) if counts else 0
        if created > 0:
            state = "added"
            message = "Added to bndy."
        elif duplicates > 0:
            state = "already_exists"
            message = "This event is already in bndy."
        else:
            state = "processed"
            message = "Processed by bndy."
    elif status in {"failed", "rejected"}:
        state = "could_not_resolve"
        message = "BNDY could not resolve this submission automatically."
    elif status == "ignored":
        state = "ignored"
        message = "This submission was not recognised as a live music event."

    return {
        "captureId": item.get("id"),
        "status": status,
        "state": state,
        "message": message,
        "receivedAt": item.get("receivedAt"),
        "updatedAt": item.get("updatedAt"),
        **({"result": result} if result else {}),
    }


class PermanentWhatsAppError(Exception):
    pass


def extract_whatsapp_messages(payload: dict) -> list[dict]:
    messages: list[dict] = []
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            continue
        for change in entry.get("changes") or []:
            value = change.get("value") if isinstance(change, dict) else None
            if not isinstance(value, dict):
                continue
            phone_number_id = str((value.get("metadata") or {}).get("phone_number_id") or "")
            for message in value.get("messages") or []:
                if not isinstance(message, dict) or not message.get("id") or not message.get("from"):
                    continue
                message_type = str(message.get("type") or "unsupported")
                content = message.get(message_type) if isinstance(message.get(message_type), dict) else {}
                text = ""
                media_id = None
                mime_type = None
                file_name = None
                if message_type == "text":
                    text = str(content.get("body") or "").strip()
                elif message_type == "image":
                    text = str(content.get("caption") or "").strip()
                    media_id = content.get("id")
                    mime_type = content.get("mime_type")
                elif message_type == "document":
                    text = str(content.get("caption") or "").strip()
                    media_id = content.get("id")
                    mime_type = content.get("mime_type")
                    file_name = content.get("filename")
                messages.append({
                    "messageId": str(message["id"]),
                    "senderRef": str(message["from"]),
                    "phoneNumberId": phone_number_id,
                    "messageType": message_type,
                    "text": text,
                    "mediaId": str(media_id) if media_id else None,
                    "mimeType": str(mime_type) if mime_type else None,
                    "fileName": str(file_name)[:255] if file_name else None,
                    "timestamp": str(message.get("timestamp") or ""),
                })
    return messages


def whatsapp_graph_request(path_or_url: str, *, method: str = "GET", body: dict | None = None, binary: bool = False):
    config = get_whatsapp_config()
    if not config:
        raise RuntimeError("WhatsApp transport is disabled")
    url = path_or_url if path_or_url.startswith("https://") else f"https://graph.facebook.com/{WHATSAPP_GRAPH_VERSION}/{path_or_url.lstrip('/')}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {config['accessToken']}")
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=15) as graph_response:
            limit = PUBLIC_MAX_IMAGE_BYTES + 1 if binary else 1024 * 1024
            response_body = graph_response.read(limit)
            if binary:
                return response_body, graph_response.headers.get("Content-Type")
            return json.loads(response_body.decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"Meta Graph API returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("Meta Graph API could not be reached") from exc


def send_whatsapp_text(recipient: str, message: str):
    config = get_whatsapp_config()
    if not config:
        raise RuntimeError("WhatsApp transport is disabled")
    return whatsapp_graph_request(
        f"{config['phoneNumberId']}/messages",
        method="POST",
        body={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "text",
            "text": {"preview_url": False, "body": message[:4096]},
        },
    )


def send_whatsapp_follow_up(recipient: str, message: str):
    """Start a web-submission follow-up with an approved Meta template.

    A person who entered a number in Chatzone has not opened a WhatsApp service
    window with BNDY, so sending free-form text here would be rejected by Meta.
    """
    config = get_whatsapp_config()
    if not config:
        raise RuntimeError("WhatsApp transport is disabled")
    if not WHATSAPP_FOLLOW_UP_TEMPLATE:
        raise RuntimeError("WhatsApp follow-up template is not configured")
    return whatsapp_graph_request(
        f"{config['phoneNumberId']}/messages",
        method="POST",
        body={
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient,
            "type": "template",
            "template": {
                "name": WHATSAPP_FOLLOW_UP_TEMPLATE,
                "language": {"code": "en_GB"},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": message[:1000]}],
                }],
            },
        },
    )


def download_whatsapp_image(message: dict) -> dict:
    media_id = message.get("mediaId")
    if not media_id:
        raise PermanentWhatsAppError("I could not retrieve that image. Please send it again as a poster or screenshot.")
    metadata = whatsapp_graph_request(str(media_id))
    mime_type = str(metadata.get("mime_type") or message.get("mimeType") or "")
    if mime_type not in ALLOWED_IMAGE_TYPES:
        raise PermanentWhatsAppError("I can currently read JPG, PNG, WEBP and GIF images. Please resend it in one of those formats.")
    declared_size = int(metadata.get("file_size") or 0)
    if declared_size > PUBLIC_MAX_IMAGE_BYTES:
        raise PermanentWhatsAppError("That image is over 5 MB. Please send a smaller version.")
    media_url = metadata.get("url")
    if not isinstance(media_url, str) or not media_url.startswith("https://"):
        raise RuntimeError("Meta did not return a safe media URL")
    image_bytes, actual_type = whatsapp_graph_request(media_url, binary=True)
    if not image_bytes or len(image_bytes) > PUBLIC_MAX_IMAGE_BYTES:
        raise PermanentWhatsAppError("That image is empty or over 5 MB. Please send a smaller version.")
    if actual_type and actual_type.split(";")[0].strip() != mime_type:
        raise RuntimeError("Downloaded WhatsApp media type did not match its metadata")

    suffix = {
        "image/jpeg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }[mime_type]
    key = f"{WHATSAPP_IMAGE_PREFIX}{datetime.now(timezone.utc).strftime('%Y/%m/%d')}/{uuid.uuid4()}.{suffix}"
    S3.put_object(Bucket=IMAGE_BUCKET, Key=key, Body=image_bytes, ContentType=mime_type)
    return {
        "type": "image",
        "bucket": IMAGE_BUCKET,
        "key": key,
        "mimeType": mime_type,
        "size": len(image_bytes),
        **({"originalName": message["fileName"]} if message.get("fileName") else {}),
    }


def queue_whatsapp_result(capture_id: str, delay_seconds: int = 5):
    if not WHATSAPP_ENABLED or not WHATSAPP_QUEUE_URL:
        return
    SQS.send_message(
        QueueUrl=WHATSAPP_QUEUE_URL,
        DelaySeconds=max(0, min(delay_seconds, 900)),
        MessageBody=json.dumps({"action": "result", "captureId": capture_id}),
    )


def handle_whatsapp_webhook(event: dict, method: str):
    try:
        config = get_whatsapp_config()
    except Exception:
        return response(503, {"error": "whatsapp_not_configured"})
    if not config or not WHATSAPP_QUEUE_URL:
        return response(503, {"error": "whatsapp_disabled"})

    if method == "GET":
        query = event.get("queryStringParameters") or {}
        supplied_token = str(query.get("hub.verify_token") or "")
        if query.get("hub.mode") == "subscribe" and secrets.compare_digest(supplied_token, config["verifyToken"]):
            challenge = str(query.get("hub.challenge") or "")
            return {
                "statusCode": 200,
                "headers": {"content-type": "text/plain", "cache-control": "no-store"},
                "body": challenge,
            }
        return response(403, {"error": "verification_failed"})

    body_bytes = raw_body(event)
    if not verify_whatsapp_signature(body_bytes, headers(event).get("x-hub-signature-256"), config["appSecret"]):
        return response(401, {"error": "invalid_signature"})
    try:
        payload = json.loads(body_bytes.decode("utf-8") or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError):
        return response(400, {"error": "invalid_json"})

    for message in extract_whatsapp_messages(payload):
        if message["phoneNumberId"] != config["phoneNumberId"]:
            continue
        record_id = f"wa-msg#{message['messageId']}"
        now = now_iso()
        try:
            TABLE.put_item(
                Item={
                    "id": record_id,
                    "recordType": "whatsapp_message",
                    "status": "queued",
                    "receivedAt": now,
                    "updatedAt": now,
                    "messageType": message["messageType"],
                    "senderHash": hashlib.sha256(message["senderRef"].encode("utf-8")).hexdigest(),
                    "expiresAt": int(time.time()) + 30 * 24 * 60 * 60,
                },
                ConditionExpression="attribute_not_exists(id)",
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                continue
            raise
        try:
            SQS.send_message(
                QueueUrl=WHATSAPP_QUEUE_URL,
                MessageBody=json.dumps({"action": "ingest", "message": message}),
            )
        except Exception:
            TABLE.delete_item(Key={"id": record_id})
            raise

    return response(200, {"ok": True})


def create_whatsapp_capture(message: dict) -> dict:
    message_id = str(message["messageId"])
    sender = str(message["senderRef"])
    message_type = str(message.get("messageType") or "unsupported")
    text = str(message.get("text") or "").strip()
    capture_id = transport_capture_id("whatsapp", message_id)
    item = get_capture(capture_id)
    if not item:
        media = None
        if message_type == "image" or (message_type == "document" and message.get("mimeType") in ALLOWED_IMAGE_TYPES):
            media = download_whatsapp_image(message)
        elif message_type != "text":
            raise PermanentWhatsAppError("Send me a poster, screenshot, link or event message and I will pass it to bndy.")
        if not text and not media:
            raise PermanentWhatsAppError("That message was empty. Send a poster, link or a few event details.")

        now = now_iso()
        item = {
            "id": capture_id,
            "capturedAt": now,
            "receivedAt": now,
            "updatedAt": now,
            "sharedText": text or None,
            "sharedUrl": first_url(text),
            "mimeType": media.get("mimeType") if media else "text/plain",
            "sourceApp": "whatsapp",
            "suggestedEntityType": "event",
            "status": "unprocessed",
            "rawPayload": {"transport": "whatsapp", "messageId": message_id, "messageType": message_type},
            "media": media,
            "transportIdempotencyKey": f"whatsapp:{message_id}",
        }
        item = {key: value for key, value in item.items() if value is not None}
        try:
            TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(id)")
            queue_capture_for_processing(capture_id)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise
            item = get_capture(capture_id) or item

    now = now_iso()

    reply_id = f"wa-reply#{capture_id}"
    try:
        TABLE.put_item(
            Item={
                "id": reply_id,
                "recordType": "whatsapp_reply",
                "captureId": capture_id,
                "recipient": sender,
                "receivedAt": now,
                "updatedAt": now,
                "expiresAt": int(time.time()) + 30 * 24 * 60 * 60,
            },
            ConditionExpression="attribute_not_exists(id)",
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise

    inbound_id = f"wa-msg#{message_id}"
    inbound = TABLE.get_item(Key={"id": inbound_id}, ConsistentRead=True).get("Item") or {}
    if not inbound.get("ackSentAt"):
        send_whatsapp_text(sender, "Got it. bndy is checking the gig now. I will reply here when it is done.")
        TABLE.update_item(
            Key={"id": inbound_id},
            UpdateExpression="SET #status = :captured, captureId = :captureId, ackSentAt = :sentAt, updatedAt = :sentAt",
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={":captured": "captured", ":captureId": capture_id, ":sentAt": now_iso()},
        )
    return item


def build_whatsapp_result_message(item: dict) -> str:
    public = public_capture_view(item)
    state = public["state"]
    event = (public.get("result") or {}).get("event")
    artist = (public.get("result") or {}).get("artist")
    if state == "added" and event:
        name = artist.get("name") if artist else "The gig"
        return f"Done. {name} at {event['venue']} on {event['date']} is now on bndy: {event['url']}"
    if state == "already_exists" and event:
        return f"Good spot. That gig is already on bndy: {event['url']}"
    if state == "needs_review":
        return "I found useful gig details, but this one needs a human check before it can be added. bndy has kept your submission."
    if state == "could_not_resolve":
        return "I could not add this one automatically. Try a clearer poster or include the artist, venue, date and time."
    if state == "ignored":
        return "I could not find a live gig in that message. Try sending a poster, event link, artist, venue and date."
    return "bndy has finished checking your submission. Thanks for sending it."


def deliver_whatsapp_result(capture_id: str):
    reply_id = f"wa-reply#{capture_id}"
    reply = TABLE.get_item(Key={"id": reply_id}, ConsistentRead=True).get("Item")
    if not reply or reply.get("resultSentAt"):
        return
    item = get_capture(capture_id)
    if not item or public_capture_view(item)["state"] == "processing":
        raise RuntimeError("WhatsApp result is not terminal yet")

    now_epoch = int(time.time())
    lease_until = now_epoch + 60
    try:
        locked = TABLE.update_item(
            Key={"id": reply_id},
            UpdateExpression="SET replyLeaseUntil = :leaseUntil, updatedAt = :updatedAt",
            ExpressionAttributeValues={
                ":leaseUntil": lease_until,
                ":now": now_epoch,
                ":updatedAt": now_iso(),
            },
            ConditionExpression=(
                "attribute_exists(id) AND attribute_not_exists(resultSentAt) AND "
                "(attribute_not_exists(replyLeaseUntil) OR replyLeaseUntil < :now)"
            ),
            ReturnValues="ALL_NEW",
        )["Attributes"]
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return
        raise

    recipient = locked.get("recipient")
    if not recipient:
        return
    try:
        send_whatsapp_text(str(recipient), build_whatsapp_result_message(item))
    except Exception:
        TABLE.update_item(Key={"id": reply_id}, UpdateExpression="REMOVE replyLeaseUntil")
        raise
    TABLE.update_item(
        Key={"id": reply_id},
        UpdateExpression="SET resultSentAt = :sentAt, updatedAt = :sentAt REMOVE recipient, replyLeaseUntil",
        ExpressionAttributeValues={":sentAt": now_iso()},
    )


def whatsapp_worker_handler(event, _context):
    failures = []
    for record in event.get("Records") or []:
        item_identifier = str(record.get("messageId") or "unknown")
        try:
            envelope = json.loads(record.get("body") or "{}")
            if envelope.get("action") == "ingest":
                try:
                    create_whatsapp_capture(envelope["message"])
                except PermanentWhatsAppError as exc:
                    message = envelope.get("message") or {}
                    if message.get("senderRef"):
                        send_whatsapp_text(str(message["senderRef"]), str(exc))
                    TABLE.update_item(
                        Key={"id": f"wa-msg#{message.get('messageId')}"},
                        UpdateExpression="SET #status = :failed, updatedAt = :updatedAt",
                        ExpressionAttributeNames={"#status": "status"},
                        ExpressionAttributeValues={":failed": "failed", ":updatedAt": now_iso()},
                    )
            elif envelope.get("action") == "result":
                deliver_whatsapp_result(str(envelope["captureId"]))
            else:
                raise ValueError("Unknown WhatsApp queue action")
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "whatsappQueueMessage": item_identifier}))
            failures.append({"itemIdentifier": item_identifier})
    return {"batchItemFailures": failures}


def lambda_handler(event, _context):
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "")
    path = request.get("path", "")

    if method == "GET" and path == "/health":
        return response(200, {
            "ok": True,
            "service": "bndy-capture",
            "time": now_iso(),
            "whatsapp": "enabled" if WHATSAPP_ENABLED else "disabled",
        })

    if path == "/v1/whatsapp/webhook" and method in {"GET", "POST"}:
        try:
            return handle_whatsapp_webhook(event, method)
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "whatsappWebhook": True}))
            return response(500, {"error": "internal_error"})

    # Public Dropzone transport. It can submit evidence, but it cannot read private capture
    # notes, claim work, mutate statuses, or use the service bearer token.
    if path.startswith("/v1/public/"):
        if not public_origin_allowed(event):
            return response(403, {"error": "origin_not_allowed"})
        try:
            if method == "POST" and path == "/v1/public/uploads/image":
                return create_public_image_upload(event, parse_body(event))

            if method == "POST" and path == "/v1/public/captures":
                if not enforce_public_rate_limit(event):
                    return response(429, {"error": "rate_limited", "message": "Too many submissions. Please try again shortly."})
                item, error = validate_public_capture(parse_body(event))
                if error:
                    return response(400, {"error": "invalid_capture", "message": error})
                try:
                    TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(id)")
                    queue_capture_for_processing(item["id"])
                    return response(201, public_capture_view(item))
                except ClientError as exc:
                    if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                        raise
                    existing = get_capture(item["id"])
                    if not existing or existing.get("sourceApp") != "chatzone":
                        raise
                    return response(200, {**public_capture_view(existing), "replayed": True})

            capture_id = (event.get("pathParameters") or {}).get("id")
            if method == "POST" and capture_id and path.endswith(f"/v1/public/captures/{capture_id}/follow-up"):
                if not enforce_public_rate_limit(event):
                    return response(429, {"error": "rate_limited", "message": "Too many requests. Please try again shortly."})
                return save_public_follow_up(capture_id, parse_body(event))

            if method == "GET" and capture_id and path.endswith(f"/v1/public/captures/{capture_id}"):
                item = get_capture(capture_id)
                if not item or item.get("sourceApp") != "chatzone":
                    return response(404, {"error": "not_found"})
                return response(200, public_capture_view(item))

            return response(404, {"error": "not_found"})
        except json.JSONDecodeError:
            return response(400, {"error": "invalid_json"})
        except ValueError as exc:
            return response(400, {"error": "bad_request", "message": str(exc)})
        except Exception as exc:
            print(json.dumps({"error": type(exc).__name__, "message": str(exc), "public": True}))
            return response(500, {"error": "internal_error"})

    if not authorised(event):
        return response(401, {"error": "unauthorised"})

    try:
        if method == "POST" and path == "/v1/uploads/image":
            return create_image_upload(parse_body(event))

        if method == "POST" and path == "/v1/captures":
            body = parse_body(event)
            item, error = validate_create(body)
            if error:
                return response(400, {"error": "invalid_capture", "message": error})
            TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(id)")
            queue_capture_for_processing(item["id"])
            return response(201, item)

        if method == "GET" and path == "/v1/captures":
            query = event.get("queryStringParameters") or {}
            status = query.get("status")
            limit = max(1, min(int(query.get("limit", "100")), 500))
            if status:
                if status not in VALID_STATUSES:
                    return response(400, {"error": "invalid_status"})
                result = TABLE.query(
                    IndexName="status-receivedAt-index",
                    KeyConditionExpression=Key("status").eq(status),
                    ScanIndexForward=False,
                    Limit=limit,
                )
            else:
                result = TABLE.scan(
                    Limit=limit,
                    FilterExpression="attribute_not_exists(recordType)",
                )
                result["Items"] = sorted(
                    result.get("Items", []), key=lambda item: item.get("receivedAt", ""), reverse=True
                )
            return response(200, {"items": result.get("Items", [])})

        capture_id = (event.get("pathParameters") or {}).get("id")
        if not capture_id:
            return response(404, {"error": "not_found"})

        if method == "GET" and path.endswith(f"/v1/captures/{capture_id}"):
            item = get_capture(capture_id)
            return response(200, item) if item else response(404, {"error": "not_found"})

        if method == "GET" and path.endswith(f"/v1/captures/{capture_id}/follow-up"):
            item = get_capture_follow_up(capture_id)
            return response(200, item or {"captureId": capture_id, "notificationStatus": "not_requested"})

        if method == "PATCH" and path.endswith(f"/v1/captures/{capture_id}/follow-up"):
            body = parse_body(event)
            status = body.get("notificationStatus")
            if status not in {"pending", "sent", "failed", "transport_unavailable"}:
                return response(400, {"error": "invalid_notification_status"})
            item = get_capture_follow_up(capture_id)
            if not item:
                return response(404, {"error": "not_found"})
            values = {":status": status, ":updatedAt": now_iso()}
            expression = "SET notificationStatus = :status, updatedAt = :updatedAt"
            error = body.get("notificationError")
            if isinstance(error, str) and error.strip():
                expression += ", notificationError = :error"
                values[":error"] = error.strip()[:500]
            result = TABLE.update_item(
                Key={"id": follow_up_key(capture_id)},
                UpdateExpression=expression,
                ExpressionAttributeValues=values,
                ReturnValues="ALL_NEW",
            )
            return response(200, result["Attributes"])

        if method == "GET" and path.endswith(f"/v1/captures/{capture_id}/media"):
            item = get_capture(capture_id)
            return response(200, capture_media_view(item)) if item else response(404, {"error": "not_found"})

        if method == "POST" and path.endswith(f"/v1/captures/{capture_id}/review"):
            return review_capture(capture_id, parse_body(event))

        if method == "POST" and path.endswith(f"/v1/captures/{capture_id}/notify"):
            follow_up = get_capture_follow_up(capture_id)
            if not follow_up:
                return response(404, {"error": "not_found"})
            if follow_up.get("method") != "whatsapp":
                return response(409, {"error": "wrong_follow_up_method"})
            if not WHATSAPP_ENABLED:
                return response(409, {"error": "whatsapp_disabled"})
            body = parse_body(event)
            message = str(body.get("message") or "").strip()
            if not message or len(message) > 1000:
                return response(400, {"error": "invalid_message"})
            try:
                send_whatsapp_follow_up(str(follow_up["contact"]).lstrip("+"), message)
            except RuntimeError as exc:
                if "template is not configured" in str(exc):
                    return response(409, {"error": "whatsapp_follow_up_not_configured"})
                raise
            TABLE.update_item(
                Key={"id": follow_up_key(capture_id)},
                UpdateExpression="SET notificationStatus = :sent, notifiedAt = :now, updatedAt = :now",
                ExpressionAttributeValues={":sent": "sent", ":now": now_iso()},
            )
            return response(200, {"sent": True, "method": "whatsapp"})

        if method == "PATCH" and path.endswith("/claim"):
            body = parse_body(event)
            expected_status = body.get("expectedStatus", "unprocessed")
            worker_id = body.get("workerId")
            lease_until = body.get("leaseUntil")

            if expected_status != "unprocessed":
                return response(400, {"error": "invalid_expected_status", "message": "Only unprocessed captures may be claimed"})
            if not isinstance(worker_id, str) or not worker_id.strip():
                return response(400, {"error": "invalid_worker_id"})
            if not isinstance(lease_until, str) or not lease_until.strip():
                return response(400, {"error": "invalid_lease_until"})

            updated_at = now_iso()
            try:
                result = TABLE.update_item(
                    Key={"id": capture_id},
                    UpdateExpression=(
                        "SET #status = :processing, updatedAt = :updatedAt, "
                        "processingWorkerId = :workerId, processingStartedAt = :startedAt, "
                        "leaseUntil = :leaseUntil ADD processingAttempt :one"
                    ),
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={
                        ":processing": "processing",
                        ":expected": expected_status,
                        ":updatedAt": updated_at,
                        ":workerId": worker_id.strip(),
                        ":startedAt": updated_at,
                        ":leaseUntil": lease_until,
                        ":one": 1,
                    },
                    ConditionExpression=(
                        "attribute_exists(id) AND (#status = :expected OR "
                        "(#status = :processing AND processingWorkerId = :workerId))"
                    ),
                    ReturnValues="ALL_NEW",
                )
                return response(200, result["Attributes"])
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    current = get_capture(capture_id)
                    if not current:
                        return response(404, {"error": "not_found"})
                    return response(409, {
                        "error": "already_claimed",
                        "status": current.get("status"),
                        "processingWorkerId": current.get("processingWorkerId"),
                        "leaseUntil": current.get("leaseUntil"),
                    })
                raise

        if method == "PATCH" and path.endswith("/status"):
            body = parse_body(event)
            status = body.get("status")
            if status not in VALID_STATUSES:
                return response(400, {"error": "invalid_status"})
            public_outcome, outcome_error = validate_public_outcome(body.get("publicOutcome"))
            if outcome_error:
                return response(400, {"error": "invalid_public_outcome", "message": outcome_error})
            updated_at = now_iso()
            update_expression = "SET #status = :status, updatedAt = :updatedAt"
            expression_values = {":status": status, ":updatedAt": updated_at}
            if public_outcome:
                update_expression += ", publicOutcome = :publicOutcome"
                expression_values[":publicOutcome"] = public_outcome
            result = TABLE.update_item(
                Key={"id": capture_id},
                UpdateExpression=update_expression,
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues=expression_values,
                ConditionExpression="attribute_exists(id)",
                ReturnValues="ALL_NEW",
            )
            if result["Attributes"].get("sourceApp") == "whatsapp" and (
                status in {"failed", "rejected", "ignored"} or (status == "processed" and public_outcome)
            ):
                queue_whatsapp_result(capture_id)
            return response(200, result["Attributes"])

        if method == "POST" and path.endswith("/notes"):
            body = parse_body(event)
            note = body.get("note")
            if not isinstance(note, str) or not note.strip():
                return response(400, {"error": "invalid_note"})
            current = get_capture(capture_id)
            if not current:
                return response(404, {"error": "not_found"})
            combined = f"{current.get('note')}\n\n{note.strip()}" if current.get("note") else note.strip()
            result = TABLE.update_item(
                Key={"id": capture_id},
                UpdateExpression="SET note = :note, updatedAt = :updatedAt",
                ExpressionAttributeValues={":note": combined, ":updatedAt": now_iso()},
                ReturnValues="ALL_NEW",
            )
            if (
                result["Attributes"].get("sourceApp") == "whatsapp"
                and result["Attributes"].get("status") in {"processed", "failed", "rejected", "ignored"}
            ):
                queue_whatsapp_result(capture_id)
            return response(200, result["Attributes"])

        return response(404, {"error": "not_found"})

    except json.JSONDecodeError:
        return response(400, {"error": "invalid_json"})
    except ValueError as exc:
        return response(400, {"error": "bad_request", "message": str(exc)})
    except TABLE.meta.client.exceptions.ConditionalCheckFailedException:
        return response(404, {"error": "not_found"})
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}))
        return response(500, {"error": "internal_error"})
