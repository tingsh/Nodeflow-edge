"""
Nodeflow Edge MQTT Publisher

Bidirectional MQTT client for the Nodeflow Edge gateway.
- Publishes telemetry, attributes, logs, and RPC responses to Nodeflow Cloud.
- Subscribes to inbound topics for remote config, RPC requests, and attribute pushes.

Features:
- Auto-reconnect with exponential backoff
- QoS 1 for reliable delivery
- Local queue buffering on disconnect
- Inbound message routing via registered callbacks
- Optional TLS support
"""

import json
import logging
import os
import threading
from queue import Queue, Full, Empty
from time import sleep, time
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt

log = logging.getLogger("nodeflow_edge.mqtt_publisher")


class NodeflowMqttPublisher:
    """Bidirectional MQTT client for the Nodeflow Edge gateway."""

    def __init__(self, config: dict, serial_number: str = "", config_path: str = ""):
        self._host = config.get("host", "localhost")
        self._telemetry_topic = config.get("topic", "v1/gateway/telemetry")
        self._username = config.get("username", "")
        self._password = config.get("password", "")
        self._qos = config.get("qos", 1)
        self._client_id = config.get("client_id", "nodeflow-edge")
        self._tls = config.get("tls", None)
        self._max_queue_size = config.get("max_queue_size", 10000)
        self._reconnect_delay_min = config.get("reconnect_delay_min", 1)
        self._reconnect_delay_max = config.get("reconnect_delay_max", 60)
        self._serial_number = serial_number
        self._config_path = config_path

        # Provisioning-mode tracking
        self._consecutive_failures = 0
        self._activation_message_shown = False

        # Default port: 8883 when TLS is configured, 1883 otherwise
        self._port = config.get("port", 8883 if self._tls else 1883)

        self._connected = False
        self._stopped = False
        self._queue = Queue(maxsize=self._max_queue_size)

        # Inbound message routing: topic -> list of callbacks
        self._subscriptions: Dict[str, List[Callable]] = {}
        self._subscription_lock = threading.Lock()

        # Create MQTT client (paho-mqtt v2.x API)
        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=self._client_id,
            protocol=mqtt.MQTTv311
        )

        if self._username:
            self._client.username_pw_set(self._username, self._password)

        # ─── TLS configuration ───────────────────────────────────────
        self._configure_tls()

        # ─── Last Will and Testament (LWT) ───────────────────────────
        self._configure_lwt()

        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(
            min_delay=self._reconnect_delay_min,
            max_delay=self._reconnect_delay_max
        )

        self._publish_thread = None

    def _configure_tls(self):
        """
        Configure TLS based on the 'tls' block in config.

        Supported modes:
          - absent / None:  No TLS (dev/local only)
          - "one-way":      Server cert verification only (standard)
          - "mutual":       mTLS — both server and client certs (enterprise)
        """
        if not self._tls:
            return

        mode = self._tls.get("mode", "one-way")
        ca_certs = self._tls.get("ca_certs")

        if not ca_certs:
            log.error("TLS enabled but 'ca_certs' path is missing from config.")
            return

        if not os.path.isfile(ca_certs):
            log.error("TLS CA certificate file not found: %s", ca_certs)
            return

        if mode == "one-way":
            self._client.tls_set(ca_certs=ca_certs)
            log.info("TLS configured in one-way mode (server verification only).")

        elif mode == "mutual":
            certfile = self._tls.get("certfile")
            keyfile = self._tls.get("keyfile")

            if not certfile or not keyfile:
                log.error("mTLS mode requires 'certfile' and 'keyfile' in tls config.")
                return

            for path, label in [(certfile, "certfile"), (keyfile, "keyfile")]:
                if not os.path.isfile(path):
                    log.error("mTLS %s not found: %s", label, path)
                    return

            self._client.tls_set(
                ca_certs=ca_certs,
                certfile=certfile,
                keyfile=keyfile,
            )
            log.info("TLS configured in mutual mode (mTLS — client cert verified).")

        else:
            log.error("Unknown TLS mode '%s'. Expected 'one-way' or 'mutual'.", mode)

    def _configure_lwt(self):
        """
        Set MQTT Last Will and Testament (LWT).
        If the gateway disconnects unexpectedly, the broker publishes this
        'offline' status message automatically on v1/gateway/attributes.
        """
        if not self._serial_number:
            return

        lwt_payload = json.dumps({
            "serial_number": self._serial_number,
            "ts": int(time() * 1000),
            "attributes": {"status": "offline"}
        })
        self._client.will_set(
            topic="v1/gateway/attributes",
            payload=lwt_payload,
            qos=1,
            retain=False,
        )
        log.info("LWT configured — broker will publish offline status on unexpected disconnect.")

    def connect(self):
        """Connect to the MQTT broker and start the background publish loop."""
        log.info("Connecting to MQTT broker at %s:%d ...", self._host, self._port)

        # Auto-subscribe to provision topic for credential rotation
        if self._serial_number:
            provision_topic = f"v1/gateway/{self._serial_number}/provision"
            self.subscribe(provision_topic, self._on_provision_message)

        try:
            self._client.connect(self._host, self._port, keepalive=60)
            self._client.loop_start()
        except Exception as e:
            log.error("Failed to connect to MQTT broker: %s", e)
            # loop_start will handle reconnection attempts
            self._client.loop_start()

        self._stopped = False
        self._publish_thread = threading.Thread(
            target=self._publish_loop, name="MQTT-Publisher", daemon=True
        )
        self._publish_thread.start()

    def disconnect(self):
        """Gracefully disconnect from the broker."""
        log.info("Disconnecting from MQTT broker...")
        self._stopped = True
        # Flush remaining messages
        self._flush_queue()
        self._client.loop_stop()
        self._client.disconnect()
        log.info("MQTT publisher stopped.")

    # ─── Subscription management ─────────────────────────────────────

    def subscribe(self, topic: str, callback: Callable):
        """
        Register a callback for messages on the given MQTT topic.
        The callback signature should be: callback(topic: str, payload: dict)
        Subscriptions are (re-)issued on every connect event.
        """
        with self._subscription_lock:
            if topic not in self._subscriptions:
                self._subscriptions[topic] = []
            self._subscriptions[topic].append(callback)

        # If already connected, subscribe immediately
        if self._connected:
            self._client.subscribe(topic, qos=self._qos)
            log.info("Subscribed to topic: %s", topic)

    def _resubscribe_all(self):
        """Re-subscribe to all registered topics (called on reconnect)."""
        with self._subscription_lock:
            for topic in self._subscriptions:
                self._client.subscribe(topic, qos=self._qos)
                log.info("Re-subscribed to topic: %s", topic)

    # ─── Multi-topic publish helpers ───────────────────────────────────

    def publish(self, payload: dict, topic: Optional[str] = None):
        """
        Queue a payload dict for publishing.
        If topic is None, publishes to the default telemetry topic.
        """
        item = (topic or self._telemetry_topic, payload)
        try:
            self._queue.put_nowait(item)
        except Full:
            log.warning("MQTT publish queue is full (%d messages). Dropping oldest message.",
                        self._max_queue_size)
            try:
                self._queue.get_nowait()  # Drop oldest
            except Empty:
                pass
            self._queue.put_nowait(item)

    def publish_telemetry(self, payload: dict):
        """Publish to the telemetry topic."""
        self.publish(payload, self._telemetry_topic)

    def publish_attributes(self, payload: dict):
        """Publish to the attributes topic."""
        self.publish(payload, "v1/gateway/attributes")

    def publish_logs(self, payload: dict):
        """Publish to the logs topic."""
        self.publish(payload, "v1/gateway/logs")

    def publish_rpc_response(self, payload: dict):
        """Publish to the RPC response topic."""
        self.publish(payload, "v1/gateway/rpc/response")

    def is_connected(self):
        return self._connected

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0 or rc == mqtt.MQTT_ERR_SUCCESS:
            log.info("Connected to MQTT broker at %s:%d", self._host, self._port)
            self._connected = True
            self._consecutive_failures = 0
            self._activation_message_shown = False
            # Re-subscribe to all registered topics on (re)connect
            self._resubscribe_all()
        else:
            log.error("MQTT connection failed with code %s", rc)
            self._connected = False
            self._consecutive_failures += 1
            self._show_activation_message_if_needed()

    def _on_disconnect(self, client, userdata, flags, rc, properties=None):
        self._connected = False
        if rc != 0 and rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("Unexpected MQTT disconnect (rc=%s). Will auto-reconnect.", rc)
            self._consecutive_failures += 1
            self._show_activation_message_if_needed()
        else:
            log.info("MQTT disconnected cleanly.")

    def _show_activation_message_if_needed(self):
        """Show a friendly activation message after repeated connection failures."""
        if self._consecutive_failures >= 3 and not self._activation_message_shown:
            self._activation_message_shown = True
            msg = (
                "\n"
                "==========================================================\n"
                "  AWAITING CLOUD ACTIVATION\n"
                "\n"
                "  Your Nodeflow Edge gateway is ready to connect.\n"
                "  Enter your Serial Number and Claim Code at:\n"
                "\n"
                "    https://app.nodeflow.io\n"
                "\n"
                f"  Serial Number: {self._serial_number}\n"
                "  (Claim Code is on the sticker on your gateway)\n"
                "=========================================================="
            )
            log.warning(msg)
            # Also print to stdout for users with a monitor connected
            print(msg)

    def _on_publish(self, client, userdata, mid, rc=None, properties=None):
        log.debug("Message %d published successfully.", mid)

    def _on_message(self, client, userdata, msg):
        """Route inbound messages to registered callbacks."""
        topic = msg.topic
        try:
            payload = json.loads(msg.payload.decode())
        except Exception as e:
            log.error("Failed to decode inbound MQTT message on %s: %s", topic, e)
            return

        log.debug("Inbound message on %s: %s", topic, payload)

        with self._subscription_lock:
            callbacks = list(self._subscriptions.get(topic, []))

        if not callbacks:
            log.warning("No handler registered for topic %s", topic)
            return

        for cb in callbacks:
            try:
                cb(topic, payload)
            except Exception as e:
                log.exception("Error in message handler for topic %s: %s", topic, e)

    def _publish_loop(self):
        """Background thread that drains the queue and publishes messages."""
        while not self._stopped:
            try:
                item = self._queue.get(timeout=0.5)
            except Empty:
                continue

            # Items are now (topic, payload) tuples
            if isinstance(item, tuple) and len(item) == 2:
                topic, payload = item
            else:
                # Backward compatibility: bare dict goes to telemetry topic
                topic = self._telemetry_topic
                payload = item

            json_payload = json.dumps(payload)

            if self._connected:
                result = self._client.publish(
                    topic, json_payload, qos=self._qos
                )
                if result.rc != mqtt.MQTT_ERR_SUCCESS:
                    log.warning("Publish failed (rc=%d) on %s, re-queuing.", result.rc, topic)
                    self._requeue((topic, payload))
            else:
                log.debug("Not connected — buffering message locally.")
                self._requeue((topic, payload))
                sleep(1)  # Wait before retrying

    def _requeue(self, item):
        """Put a message back on the queue for retry."""
        try:
            self._queue.put_nowait(item)
        except Full:
            log.warning("Queue full during requeue. Message dropped.")

    def _flush_queue(self):
        """Attempt to publish remaining messages before shutdown."""
        flush_count = 0
        while not self._queue.empty() and self._connected:
            try:
                item = self._queue.get_nowait()
                if isinstance(item, tuple) and len(item) == 2:
                    topic, payload = item
                else:
                    topic = self._telemetry_topic
                    payload = item
                json_payload = json.dumps(payload)
                self._client.publish(topic, json_payload, qos=self._qos)
                flush_count += 1
            except Empty:
                break
        if flush_count:
            log.info("Flushed %d messages before shutdown.", flush_count)

    # ─── Credential rotation (provision topic) ────────────────────────

    def _on_provision_message(self, topic: str, payload: dict):
        """Handle inbound provisioning commands (e.g. password rotation)."""
        action = payload.get("action")
        if action == "rotate_password":
            new_password = payload.get("new_password")
            if not new_password:
                log.error("Provision rotate_password: missing 'new_password'")
                return
            self._rotate_password(new_password)
        else:
            log.warning("Unknown provision action: %s", action)

    def _rotate_password(self, new_password: str):
        """
        Rotate MQTT credentials:
        1. Update config.json on disk
        2. Update in-memory password
        3. Disconnect and reconnect with new credentials
        """
        log.info("MQTT password rotation initiated...")

        # 1. Update config.json on disk
        if self._config_path:
            try:
                with open(self._config_path, 'r') as f:
                    config = json.load(f)
                config["mqtt"]["password"] = new_password
                tmp_path = self._config_path + ".tmp"
                with open(tmp_path, 'w') as f:
                    json.dump(config, f, indent=2)
                os.replace(tmp_path, self._config_path)
                log.info("Updated config.json with new MQTT password")
            except Exception as e:
                log.error("Failed to update config.json during password rotation: %s", e)
                return

        # 2. Update in-memory password
        self._password = new_password
        self._client.username_pw_set(self._username, self._password)

        # 3. Disconnect (paho will auto-reconnect with the new credentials)
        log.info("Disconnecting to apply new credentials...")
        try:
            self._client.disconnect()
        except Exception:
            pass
        # paho-mqtt's loop_start() handles auto-reconnect
