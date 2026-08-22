import hashlib
import json
import os
import re
import secrets
import time
import uuid
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

TABLE = boto3.resource("dynamodb").Table(TABLE_NAME)
S3 = boto3.client("s3")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VALID_STATUSES = {"unprocessed", "processing", "processed", "rejected", "failed", "ignored"}
VALID_ENTITY_TYPES = {"unknown", "venue", "artist", "event"}
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}
PUBLIC_IMAGE_PREFIX = "captures/public/"


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
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def headers(event: dict) -> dict[str, str]:
    return {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}


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
        "id": str(uuid.uuid4()),
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
    }
    return {k: v for k, v in item.items() if v is not None}, None


def get_capture(capture_id: str):
    return TABLE.get_item(Key={"id": capture_id}, ConsistentRead=True).get("Item")


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


def public_capture_view(item: dict) -> dict:
    status = item.get("status", "unprocessed")
    state = "processing"
    message = "BNDY is processing your submission."
    result = None

    if status == "processed":
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


def lambda_handler(event, _context):
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "")
    path = request.get("path", "")

    if method == "GET" and path == "/health":
        return response(200, {"ok": True, "service": "bndy-capture", "time": now_iso()})

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
                TABLE.put_item(Item=item, ConditionExpression="attribute_not_exists(id)")
                return response(201, public_capture_view(item))

            capture_id = (event.get("pathParameters") or {}).get("id")
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
                    ConditionExpression="attribute_exists(id) AND #status = :expected",
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
            updated_at = now_iso()
            result = TABLE.update_item(
                Key={"id": capture_id},
                UpdateExpression="SET #status = :status, updatedAt = :updatedAt",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={":status": status, ":updatedAt": updated_at},
                ConditionExpression="attribute_exists(id)",
                ReturnValues="ALL_NEW",
            )
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