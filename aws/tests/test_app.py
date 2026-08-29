import hashlib
import hmac
import importlib.util
import json
import os
import sys
import types
import unittest
from pathlib import Path


class FakeClientError(Exception):
    def __init__(self, code="ConditionalCheckFailedException"):
        self.response = {"Error": {"Code": code}}
        super().__init__(code)


class FakeTable:
    class Meta:
        class Client:
            class Exceptions:
                ConditionalCheckFailedException = FakeClientError

            exceptions = Exceptions()

        client = Client()

    meta = Meta()


class InMemoryWebhookTable(FakeTable):
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        if ConditionExpression and Item["id"] in self.items:
            raise FakeClientError()
        self.items[Item["id"]] = Item.copy()

    def delete_item(self, Key):
        self.items.pop(Key["id"], None)


class RecordingQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, **message):
        self.messages.append(message)
        return {"MessageId": str(len(self.messages))}


class FakeResource:
    def Table(self, _name):
        return FakeTable()


class FakeKey:
    def __init__(self, value):
        self.value = value

    def eq(self, value):
        return (self.value, value)


fake_boto3 = types.ModuleType("boto3")
fake_boto3.resource = lambda _name: FakeResource()
fake_boto3.client = lambda _name: object()
fake_conditions = types.ModuleType("boto3.dynamodb.conditions")
fake_conditions.Key = FakeKey
fake_dynamodb = types.ModuleType("boto3.dynamodb")
fake_dynamodb.conditions = fake_conditions
fake_botocore_exceptions = types.ModuleType("botocore.exceptions")
fake_botocore_exceptions.ClientError = FakeClientError
fake_botocore = types.ModuleType("botocore")
fake_botocore.exceptions = fake_botocore_exceptions

sys.modules.setdefault("boto3", fake_boto3)
sys.modules.setdefault("boto3.dynamodb", fake_dynamodb)
sys.modules.setdefault("boto3.dynamodb.conditions", fake_conditions)
sys.modules.setdefault("botocore", fake_botocore)
sys.modules.setdefault("botocore.exceptions", fake_botocore_exceptions)

os.environ.setdefault("CAPTURES_TABLE", "test-captures")
os.environ.setdefault("CAPTURE_TOKEN", "test-token")
os.environ.setdefault("IMAGE_BUCKET", "test-images")
os.environ.setdefault("WHATSAPP_ENABLED", "false")

spec = importlib.util.spec_from_file_location(
    "capture_app",
    Path(__file__).resolve().parents[1] / "src" / "app.py",
)
capture_app = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(capture_app)


class CaptureContractTests(unittest.TestCase):
    def test_transport_capture_ids_are_stable_and_channel_scoped(self):
        first = capture_app.transport_capture_id("web", "submission-123")
        self.assertEqual(first, capture_app.transport_capture_id("web", "submission-123"))
        self.assertNotEqual(first, capture_app.transport_capture_id("whatsapp", "submission-123"))

    def test_public_capture_uses_client_submission_id_for_idempotency(self):
        item, error = capture_app.validate_public_capture({
            "sharedText": "Band at Venue on Friday",
            "clientSubmissionId": "web-submission-123",
        })
        self.assertIsNone(error)
        self.assertEqual(item["id"], capture_app.transport_capture_id("web", "web-submission-123"))
        self.assertEqual(item["transportIdempotencyKey"], "web:web-submission-123")

    def test_public_capture_rejects_unsafe_client_submission_id(self):
        item, error = capture_app.validate_public_capture({
            "sharedText": "Band at Venue on Friday",
            "clientSubmissionId": "../../bad",
        })
        self.assertIsNone(item)
        self.assertIn("safe characters", error)

    def test_structured_public_outcome_is_sanitised_and_preferred(self):
        item = {
            "id": "capture-1",
            "status": "processed",
            "note": "legacy note that cannot be parsed",
            "publicOutcome": {
                "state": "added",
                "message": "Added safely.",
                "result": {
                    "artist": {"name": "The Torrists", "private": "do not expose"},
                    "event": {
                        "id": "event-1",
                        "date": "2026-09-26",
                        "time": "21:00",
                        "venue": "The Club",
                        "url": "https://bndy.live/g/event-1",
                        "private": "do not expose",
                    },
                },
            },
        }
        public = capture_app.public_capture_view(item)
        self.assertEqual(public["state"], "added")
        self.assertEqual(public["message"], "Added safely.")
        self.assertNotIn("private", public["result"]["artist"])
        self.assertNotIn("private", public["result"]["event"])

    def test_needs_review_is_a_safe_public_terminal_outcome(self):
        outcome, error = capture_app.validate_public_outcome({
            "state": "needs_review",
            "message": "The artist identity needs a human check.",
        })
        self.assertIsNone(error)
        self.assertEqual(outcome, {
            "state": "needs_review",
            "message": "The artist identity needs a human check.",
        })


class WhatsAppContractTests(unittest.TestCase):
    def setUp(self):
        self.original_table = capture_app.TABLE
        self.original_sqs = capture_app.SQS
        capture_app.WHATSAPP_ENABLED = True
        capture_app.WHATSAPP_QUEUE_URL = "https://sqs.example.test/queue"
        capture_app.WHATSAPP_SECRET_ARN = "arn:aws:secretsmanager:eu-west-2:123456789012:secret:test"
        capture_app._WHATSAPP_CONFIG = {
            "verifyToken": "verify-me",
            "appSecret": "app-secret",
            "accessToken": "access-token",
            "phoneNumberId": "phone-123",
        }

    def tearDown(self):
        capture_app.TABLE = self.original_table
        capture_app.SQS = self.original_sqs
        capture_app.WHATSAPP_ENABLED = False
        capture_app.WHATSAPP_QUEUE_URL = ""
        capture_app.WHATSAPP_SECRET_ARN = ""
        capture_app._WHATSAPP_CONFIG = None

    def test_signature_verification_uses_raw_body(self):
        body = b'{"object":"whatsapp_business_account"}'
        digest = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
        self.assertTrue(capture_app.verify_whatsapp_signature(body, f"sha256={digest}", "app-secret"))
        self.assertFalse(capture_app.verify_whatsapp_signature(body + b" ", f"sha256={digest}", "app-secret"))

    def test_extracts_text_and_image_without_status_callbacks(self):
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "phone-123"},
                        "messages": [
                            {"id": "wamid.text", "from": "447700900000", "type": "text", "text": {"body": "Band at Venue"}},
                            {"id": "wamid.image", "from": "447700900000", "type": "image", "image": {"id": "media-1", "mime_type": "image/jpeg", "caption": "Tonight"}},
                        ],
                        "statuses": [{"id": "outbound-status"}],
                    },
                }],
            }],
        }
        messages = capture_app.extract_whatsapp_messages(payload)
        self.assertEqual([message["messageId"] for message in messages], ["wamid.text", "wamid.image"])
        self.assertEqual(messages[1]["mediaId"], "media-1")
        self.assertEqual(messages[1]["text"], "Tonight")

    def test_meta_webhook_verification_returns_exact_challenge(self):
        result = capture_app.handle_whatsapp_webhook({
            "queryStringParameters": {
                "hub.mode": "subscribe",
                "hub.verify_token": "verify-me",
                "hub.challenge": "challenge-456",
            },
        }, "GET")
        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(result["body"], "challenge-456")

    def test_invalid_webhook_signature_fails_closed(self):
        result = capture_app.handle_whatsapp_webhook({
            "headers": {"x-hub-signature-256": "sha256=wrong"},
            "body": json.dumps({"entry": []}),
        }, "POST")
        self.assertEqual(result["statusCode"], 401)

    def test_duplicate_webhook_delivery_enqueues_once_without_storing_sender(self):
        capture_app.TABLE = InMemoryWebhookTable()
        capture_app.SQS = RecordingQueue()
        payload = {
            "entry": [{
                "changes": [{
                    "value": {
                        "metadata": {"phone_number_id": "phone-123"},
                        "messages": [{
                            "id": "wamid.once",
                            "from": "447700900000",
                            "type": "text",
                            "text": {"body": "Band at Venue on Friday"},
                        }],
                    },
                }],
            }],
        }
        body = json.dumps(payload, separators=(",", ":"))
        digest = hmac.new(b"app-secret", body.encode(), hashlib.sha256).hexdigest()
        event = {"body": body, "headers": {"X-Hub-Signature-256": f"sha256={digest}"}}

        first = capture_app.handle_whatsapp_webhook(event, "POST")
        second = capture_app.handle_whatsapp_webhook(event, "POST")

        self.assertEqual(first["statusCode"], 200)
        self.assertEqual(second["statusCode"], 200)
        self.assertEqual(len(capture_app.SQS.messages), 1)
        stored = capture_app.TABLE.items["wa-msg#wamid.once"]
        self.assertNotIn("senderRef", stored)
        self.assertEqual(stored["senderHash"], hashlib.sha256(b"447700900000").hexdigest())

    def test_result_message_links_added_gig(self):
        message = capture_app.build_whatsapp_result_message({
            "id": "capture-1",
            "status": "processed",
            "publicOutcome": {
                "state": "added",
                "result": {
                    "artist": {"name": "The Torrists"},
                    "event": {
                        "id": "event-1",
                        "date": "2026-09-26",
                        "time": "21:00",
                        "venue": "The Club",
                        "url": "https://bndy.live/g/event-1",
                    },
                },
            },
        })
        self.assertIn("The Torrists", message)
        self.assertIn("https://bndy.live/g/event-1", message)

    def test_outbound_messages_use_the_meta_messages_endpoint(self):
        calls = []
        original_request = capture_app.whatsapp_graph_request
        capture_app.whatsapp_graph_request = lambda path, **kwargs: calls.append((path, kwargs)) or {}
        try:
            capture_app.send_whatsapp_text("447700900000", "Thanks")
        finally:
            capture_app.whatsapp_graph_request = original_request

        self.assertEqual(calls[0][0], "phone-123/messages")
        self.assertEqual(calls[0][1]["method"], "POST")


if __name__ == "__main__":
    unittest.main()
