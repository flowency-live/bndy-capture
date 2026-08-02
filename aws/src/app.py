import json
import os
import re
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import boto3
from boto3.dynamodb.conditions import Key

TABLE_NAME = os.environ["CAPTURES_TABLE"]
CAPTURE_TOKEN = os.environ["CAPTURE_TOKEN"]
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
TABLE = boto3.resource("dynamodb").Table(TABLE_NAME)
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
VALID_STATUSES = {"unprocessed", "processing", "processed", "rejected", "failed", "ignored"}
VALID_ENTITY_TYPES = {"unknown", "venue", "artist", "event"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def json_default(value: Any):
    if isinstance(value, Decimal):
        return int(value) if value % 1 == 0 else float(value)
    raise TypeError(f"Unsupported value: {type(value)!r}")


def response(status: int, body: Any):
    return {
        "statusCode": status,
        "headers": {
            "content-type": "application/json",
            "access-control-allow-origin": ALLOWED_ORIGIN,
            "cache-control": "no-store",
        },
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


def authorised(event: dict) -> bool:
    headers = {str(k).lower(): str(v) for k, v in (event.get("headers") or {}).items()}
    supplied = headers.get("authorization", "")
    expected = f"Bearer {CAPTURE_TOKEN}"
    return secrets.compare_digest(supplied, expected)


def first_url(text: Any) -> str | None:
    if not isinstance(text, str):
        return None
    match = URL_PATTERN.search(text)
    return match.group(0).rstrip("),.;!?") if match else None


def validate_create(body: dict) -> tuple[dict | None, str | None]:
    shared_text = body.get("sharedText")
    shared_url = body.get("sharedUrl") or first_url(shared_text)
    raw_payload = body.get("rawPayload")

    if shared_text is not None and not isinstance(shared_text, str):
        return None, "sharedText must be a string"
    if shared_url is not None and not isinstance(shared_url, str):
        return None, "sharedUrl must be a string"
    if not shared_text and not shared_url and not raw_payload:
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
        "mimeType": body.get("mimeType") or "text/plain",
        "sourceApp": body.get("sourceApp"),
        "note": body.get("note"),
        "suggestedEntityType": entity_type,
        "status": "unprocessed",
        "rawPayload": raw_payload,
    }
    return {k: v for k, v in item.items() if v is not None}, None


def get_capture(capture_id: str):
    return TABLE.get_item(Key={"id": capture_id}, ConsistentRead=True).get("Item")


def lambda_handler(event, _context):
    request = event.get("requestContext", {}).get("http", {})
    method = request.get("method", "")
    path = request.get("path", "")

    if method == "GET" and path == "/health":
        return response(200, {"ok": True, "service": "bndy-capture", "time": now_iso()})

    if not authorised(event):
        return response(401, {"error": "unauthorised"})

    try:
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
                result = TABLE.scan(Limit=limit)
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
