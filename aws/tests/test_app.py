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

    def get_item(self, Key, ConsistentRead=False):
        item = self.items.get(Key["id"])
        return {"Item": item.copy()} if item else {}


class InMemoryClarificationTable(InMemoryWebhookTable):
    def update_item(self, Key, ExpressionAttributeValues, **kwargs):
        item = self.items[Key["id"]]
        if ":unprocessed" in ExpressionAttributeValues:
            item["status"] = ExpressionAttributeValues[":unprocessed"]
            item["publicClarification"] = ExpressionAttributeValues[":clarification"]
            item["updatedAt"] = ExpressionAttributeValues[":updatedAt"]
            for field in ("publicOutcome", "processingWorkerId", "processingStartedAt", "leaseUntil"):
                item.pop(field, None)
        if ":note" in ExpressionAttributeValues:
            item["note"] = ExpressionAttributeValues[":note"]
            item["updatedAt"] = ExpressionAttributeValues[":updatedAt"]
        return {"Attributes": item.copy()}


class RecordingQueue:
    def __init__(self):
        self.messages = []

    def send_message(self, **message):
        self.messages.append(message)
        return {"MessageId": str(len(self.messages))}


class RecordingClaimTable(FakeTable):
    def __init__(self):
        self.update = None

    def update_item(self, **kwargs):
        self.update = kwargs
        return {"Attributes": {"id": kwargs["Key"]["id"], "status": "processing"}}


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

    def test_public_outcome_preserves_safe_location_clarification_and_artist_link_request(self):
        outcome, error = capture_app.validate_public_outcome({
            "state": "needs_review",
            "result": {"artist": {"name": "One For The Road"}},
            "clarification": {
                "type": "confirm_new_artist",
                "artistName": "One For The Road",
                "location": "Northwich, Cheshire",
                "prompt": "Is this a different Cheshire artist?",
            },
            "requestArtistLinks": True,
        })
        self.assertIsNone(error)
        self.assertEqual(outcome["clarification"]["location"], "Northwich, Cheshire")
        self.assertTrue(outcome["requestArtistLinks"])

    def test_multi_event_public_outcome_preserves_every_safe_gig(self):
        events = [{
            "id": f"event-{index}",
            "date": date,
            "time": time,
            "venue": venue,
            "url": f"https://bndy.live/g/event-{index}",
            "action": "created",
            "private": "do not expose",
        } for index, (date, time, venue) in enumerate([
            ("2026-10-03", "21:00", "The Lion Hotel"),
            ("2026-11-15", "19:00", "Lambs Wharf"),
            ("2026-12-19", "21:00", "The Red Lion"),
        ], start=1)]

        outcome, error = capture_app.validate_public_outcome({
            "state": "added",
            "message": "3 gigs added to bndy.",
            "result": {
                "artist": {"name": "One for the Road"},
                "events": events,
            },
        })

        self.assertIsNone(error)
        self.assertEqual(len(outcome["result"]["events"]), 3)
        self.assertEqual(outcome["result"]["events"][1]["venue"], "Lambs Wharf")
        self.assertNotIn("private", outcome["result"]["events"][0])

    def test_follow_up_contacts_are_normalised(self):
        self.assertEqual(capture_app.normalise_follow_up_contact("email", " Person@Example.COM "), "person@example.com")
        self.assertEqual(capture_app.normalise_follow_up_contact("whatsapp", "07700 900 123"), "+447700900123")
        self.assertIsNone(capture_app.normalise_follow_up_contact("email", "not-an-email"))

    def test_artist_links_require_public_https_urls(self):
        self.assertEqual(capture_app.normalise_artist_links([
            "https://instagram.com/onefortheroad",
            "https://onefortheroad.example",
        ]), [
            "https://instagram.com/onefortheroad",
            "https://onefortheroad.example",
        ])
        self.assertIsNone(capture_app.normalise_artist_links(["http://example.com"]))
        self.assertIsNone(capture_app.normalise_artist_links(["https://127.0.0.1/profile"]))

    def test_artist_links_create_an_idempotent_child_capture_for_safe_enrichment(self):
        original_table = capture_app.TABLE
        original_sqs = capture_app.SQS
        original_queue = capture_app.CAPTURE_QUEUE_URL
        table = InMemoryWebhookTable()
        queue = RecordingQueue()
        table.items["capture-added"] = {
            "id": "capture-added",
            "sourceApp": "chatzone",
            "status": "processed",
            "publicOutcome": {
                "state": "added",
                "requestArtistLinks": True,
                "result": {"artist": {"id": "artist-cheshire", "name": "One For The Road", "action": "created"}},
            },
        }
        capture_app.TABLE = table
        capture_app.SQS = queue
        capture_app.CAPTURE_QUEUE_URL = "https://sqs.example.test/captures"
        try:
            result = capture_app.save_public_artist_links("capture-added", {
                "urls": ["https://instagram.com/onefortheroad"],
            })
        finally:
            capture_app.TABLE = original_table
            capture_app.SQS = original_sqs
            capture_app.CAPTURE_QUEUE_URL = original_queue

        self.assertEqual(result["statusCode"], 200)
        child = next(item for key, item in table.items.items() if key != "capture-added")
        self.assertEqual(child["sourceApp"], "chatzone-artist-links")
        self.assertEqual(child["rawPayload"]["artistLinkEnrichment"]["targetArtistId"], "artist-cheshire")
        self.assertEqual(len(queue.messages), 1)

    def test_submitter_can_confirm_a_distinct_same_name_artist_and_retry(self):
        original_table = capture_app.TABLE
        original_sqs = capture_app.SQS
        original_queue = capture_app.CAPTURE_QUEUE_URL
        table = InMemoryClarificationTable()
        queue = RecordingQueue()
        table.items["capture-review"] = {
            "id": "capture-review",
            "sourceApp": "chatzone",
            "status": "failed",
            "publicOutcome": {
                "state": "needs_review",
                "clarification": {
                    "type": "confirm_new_artist",
                    "artistName": "One For The Road",
                    "location": "Northwich, Cheshire",
                    "prompt": "Is this a different Cheshire artist?",
                },
            },
        }
        capture_app.TABLE = table
        capture_app.SQS = queue
        capture_app.CAPTURE_QUEUE_URL = "https://sqs.example.test/captures"
        try:
            result = capture_app.save_public_clarification("capture-review", {
                "type": "confirm_new_artist",
                "confirmed": True,
            })
        finally:
            capture_app.TABLE = original_table
            capture_app.SQS = original_sqs
            capture_app.CAPTURE_QUEUE_URL = original_queue

        self.assertEqual(result["statusCode"], 200)
        self.assertEqual(table.items["capture-review"]["status"], "unprocessed")
        self.assertTrue(table.items["capture-review"]["publicClarification"]["confirmed"])
        self.assertEqual(len(queue.messages), 1)

    def test_public_follow_up_response_never_echoes_contact(self):
        original_table = capture_app.TABLE
        table = InMemoryWebhookTable()
        table.items["capture-review"] = {
            "id": "capture-review",
            "sourceApp": "chatzone",
            "status": "failed",
            "publicOutcome": {"state": "needs_review"},
        }
        capture_app.TABLE = table
        try:
            result = capture_app.save_public_follow_up("capture-review", {
                "method": "email",
                "value": "person@example.com",
                "consent": True,
            })
        finally:
            capture_app.TABLE = original_table

        self.assertEqual(result["statusCode"], 200)
        self.assertNotIn("person@example.com", result["body"])
        self.assertEqual(table.items["followup#capture-review"]["contact"], "person@example.com")

    def test_public_follow_up_retry_is_idempotent_but_cannot_replace_contact(self):
        original_table = capture_app.TABLE
        table = InMemoryWebhookTable()
        table.items["capture-review"] = {
            "id": "capture-review",
            "sourceApp": "chatzone",
            "status": "failed",
            "publicOutcome": {"state": "needs_review"},
        }
        capture_app.TABLE = table
        try:
            first = capture_app.save_public_follow_up("capture-review", {
                "method": "email", "value": "person@example.com", "consent": True,
            })
            replay = capture_app.save_public_follow_up("capture-review", {
                "method": "email", "value": "person@example.com", "consent": True,
            })
            replacement = capture_app.save_public_follow_up("capture-review", {
                "method": "email", "value": "other@example.com", "consent": True,
            })
        finally:
            capture_app.TABLE = original_table

        self.assertEqual(first["statusCode"], 200)
        self.assertEqual(replay["statusCode"], 200)
        self.assertEqual(replacement["statusCode"], 409)
        self.assertEqual(table.items["followup#capture-review"]["contact"], "person@example.com")

    def test_immediate_dispatch_uses_capture_queue(self):
        original_sqs = capture_app.SQS
        original_queue = capture_app.CAPTURE_QUEUE_URL
        queue = RecordingQueue()
        capture_app.SQS = queue
        capture_app.CAPTURE_QUEUE_URL = "https://sqs.example.test/captures"
        try:
            capture_app.queue_capture_for_processing("capture-1")
        finally:
            capture_app.SQS = original_sqs
            capture_app.CAPTURE_QUEUE_URL = original_queue
        self.assertEqual(json.loads(queue.messages[0]["MessageBody"]), {"captureId": "capture-1"})

    def test_claim_is_reentrant_only_for_the_same_delivery_worker(self):
        original_table = capture_app.TABLE
        table = RecordingClaimTable()
        capture_app.TABLE = table
        try:
            result = capture_app.lambda_handler({
                "headers": {"Authorization": "Bearer test-token"},
                "requestContext": {"http": {"method": "PATCH", "path": "/v1/captures/capture-1/claim"}},
                "pathParameters": {"id": "capture-1"},
                "body": json.dumps({
                    "expectedStatus": "unprocessed",
                    "workerId": "bndy-capture-processor:sqs-message-1",
                    "leaseUntil": "2026-08-31T13:00:00Z",
                }),
            }, None)
        finally:
            capture_app.TABLE = original_table

        self.assertEqual(result["statusCode"], 200)
        self.assertIn("processingWorkerId = :workerId", table.update["ConditionExpression"])
        self.assertEqual(table.update["ExpressionAttributeValues"][":workerId"], "bndy-capture-processor:sqs-message-1")


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
