"""
whatsapp_adapter.py - Flask webhook that connects WhatsApp Cloud API to
the Brain. Run behind any reverse proxy/HTTPS endpoint Meta can reach
(e.g. nginx + certbot on your Oracle VM).
"""
import hashlib
import hmac
import logging
import requests
from flask import Flask, request

logger = logging.getLogger("whatsapp_adapter")

GRAPH_API_URL = "https://graph.facebook.com/v20.0"


class WhatsAppAdapter:
    def __init__(self, token: str, phone_number_id: str, verify_token: str,
                 allowed_numbers: set, brain, app_secret: str | None = None):
        self.token = token
        self.phone_number_id = phone_number_id
        self.verify_token = verify_token
        self.allowed_numbers = allowed_numbers
        self.brain = brain
        self.app_secret = app_secret  # set this to enable signature validation
        self.app = Flask(__name__)
        self._known_numbers = set()
        self._register_routes()

    def _is_allowed(self, number: str) -> bool:
        if not self.allowed_numbers:
            return True
        return number in self.allowed_numbers

    def _signature_valid(self, raw_body: bytes) -> bool:
        """
        Verifies the X-Hub-Signature-256 header Meta sends on every webhook
        POST, proving the request really came from Meta and wasn't forged
        by someone who guessed your webhook URL. Skipped (with a warning)
        if you haven't set WHATSAPP_APP_SECRET - fine for quick local
        testing, but set it before exposing this publicly.
        """
        if not self.app_secret:
            return True  # not configured - caller already warned at startup
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not signature.startswith("sha256="):
            return False
        expected = hmac.new(
            self.app_secret.encode("utf-8"), raw_body, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature[len("sha256="):], expected)

    def _register_routes(self):
        @self.app.route("/webhook", methods=["GET"])
        def verify():
            mode = request.args.get("hub.mode")
            token = request.args.get("hub.verify_token")
            challenge = request.args.get("hub.challenge")
            if mode == "subscribe" and token == self.verify_token:
                return challenge, 200
            return "Forbidden", 403

        @self.app.route("/webhook", methods=["POST"])
        def receive():
            if not self._signature_valid(request.get_data()):
                logger.warning("Rejected webhook POST with invalid signature.")
                return "Forbidden", 403

            data = request.get_json(silent=True) or {}
            try:
                entry = data["entry"][0]
                changes = entry["changes"][0]
                value = changes["value"]
                messages = value.get("messages")
                if not messages:
                    return "ok", 200  # status updates, etc - ignore
                msg = messages[0]
                from_number = msg["from"]
                text = msg.get("text", {}).get("body", "")
            except (KeyError, IndexError):
                return "ok", 200

            if not self._is_allowed(from_number):
                self._send(from_number, "Not authorized.")
                return "ok", 200

            self._known_numbers.add(from_number)
            try:
                reply = self.brain.handle(text)
                if not reply:
                    reply = "(No response generated - this is a bug)"
                self._send(from_number, reply)
                logger.info(f"WhatsApp {from_number}: {text[:50]} -> OK")
            except Exception as e:
                logger.error(f"Failed to handle WhatsApp message from {from_number}: {e}", exc_info=True)
                self._send(from_number, f"Error: {e}")
            return "ok", 200

        @self.app.route("/health", methods=["GET"])
        def health():
            return "ok", 200

    def _send(self, to_number: str, text: str):
        url = f"{GRAPH_API_URL}/{self.phone_number_id}/messages"
        headers = {"Authorization": f"Bearer {self.token}"}
        payload = {
            "messaging_product": "whatsapp",
            "to": to_number,
            "type": "text",
            "text": {"body": text[:4096]},  # WhatsApp message length limit
        }
        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=15)
            if resp.status_code >= 400:
                logger.error(f"WhatsApp send failed: {resp.status_code} {resp.text}")
            else:
                logger.debug(f"WhatsApp message sent to {to_number}")
        except requests.RequestException as e:
            logger.error(f"WhatsApp send error: {e}", exc_info=True)

    def send_to_all_known(self, text: str):
        """Used by the scheduler to push proactive notifications."""
        success_count = 0
        for number in self._known_numbers:
            try:
                self._send(number, text)
                success_count += 1
            except Exception as e:
                logger.warning(f"Failed to notify WhatsApp number {number}: {e}")
        logger.info(f"Sent notification to {success_count}/{len(self._known_numbers)} WhatsApp numbers")

    def run(self, host="0.0.0.0", port=8080):
        logger.info(f"Starting WhatsApp webhook on {host}:{port}")
        try:
            self.app.run(host=host, port=port)
        except Exception as e:
            logger.error(f"WhatsApp webhook failed: {e}", exc_info=True)
        finally:
            logger.info("WhatsApp webhook stopped")
