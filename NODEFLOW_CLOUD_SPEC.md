# Nodeflow Cloud — Bidirectional MQTT Implementation Spec

This document is a complete implementation guide for adding bidirectional MQTT support to the **Nodeflow Cloud** Django backend. The edge gateway (Nodeflow Edge) has already implemented its side. Your job is to build the cloud side so that the two systems can communicate bidirectionally over MQTT.

## Context

**Nodeflow Edge** is a Python-based IoT gateway running on Raspberry Pi CM4 devices. It reads data from industrial equipment (Modbus, OPC-UA, etc.) and publishes to a Mosquitto MQTT broker. The cloud side is a Django application (this codebase) that consumes MQTT messages and provides a web dashboard.

Currently, communication is **one-way** (edge → cloud via `v1/gateway/telemetry`). This spec adds **four new features** that require bidirectional MQTT:

1. **Remote Logging** — edge sends gateway logs to the cloud for dashboard viewing
2. **Attribute Sync** — edge publishes gateway status/heartbeat; cloud can push attributes to edge
3. **Remote Config** — cloud pushes config updates to the edge (add/remove/update connectors)
4. **RPC Commands** — cloud sends commands to the edge (ping, reboot, restart connectors, etc.)

---

## Mosquitto Broker Configuration

The broker must be configured for **TLS encryption** and **per-gateway authentication** before deployment. All Nodeflow Edge gateways connect to the broker over the public internet.

### One-Way TLS (Standard — All Customers)

```ini
# /etc/mosquitto/mosquitto.conf
listener 8883
cafile   /etc/mosquitto/certs/ca.crt
certfile /etc/mosquitto/certs/server.crt
keyfile  /etc/mosquitto/certs/server.key
require_certificate false

allow_anonymous false
password_file /etc/mosquitto/passwd
acl_file /etc/mosquitto/acl
```

- **Port 8883** — standard MQTT over TLS
- The server certificate can be obtained via Let's Encrypt (if using a domain like `mqtt.nodeflow.io`) or a self-signed CA
- Each edge gateway bundles the CA certificate at `/opt/nodeflow-edge/certs/ca.crt` to verify the server

### Mutual TLS / mTLS (Enterprise Add-On)

For enterprise customers who enable mTLS, add:

```ini
require_certificate true
use_identity_as_username true
```

When mTLS is active, the broker uses the client certificate's Common Name (CN) as the MQTT username — no password is needed. Django should detect which gateways are mTLS-enabled and configure Mosquitto accordingly (e.g., via a dynamic auth plugin or per-listener config).

### ACL File Template (One Entry Per Gateway)

```
user NF-EDGE-001
topic readwrite v1/gateway/telemetry
topic readwrite v1/gateway/logs
topic readwrite v1/gateway/attributes
topic readwrite v1/gateway/rpc/response
topic readwrite v1/gateway/NF-EDGE-001/#
```

Each gateway can only publish/subscribe to its own serial-number-scoped topics plus the shared inbound topics.

---

## Per-Gateway Credentials & mTLS (Django Model Changes)

Add the following fields to the `Gateway` Django model:

```python
class Gateway(models.Model):
    ...
    mqtt_username   = models.CharField(max_length=100, unique=True)
    mqtt_password   = models.CharField(max_length=255)  # store bcrypt hash
    tls_mode        = models.CharField(
        max_length=10,
        choices=[("none", "None"), ("one-way", "One-Way TLS"), ("mutual", "Mutual TLS")],
        default="one-way"
    )
    # mTLS only — store cert PEM strings (populated when enterprise enables mTLS)
    client_cert_pem = models.TextField(blank=True)
    client_key_pem  = models.TextField(blank=True)
```

**On gateway registration (Django):**
1. Auto-generate `mqtt_username = serial_number`
2. Generate a random 32-char password, store bcrypt hash
3. Append entry to Mosquitto passwd file: `mosquitto_passwd -b /etc/mosquitto/passwd <user> <pass>`
4. Append ACL entry for the gateway
5. Reload Mosquitto: `systemctl reload mosquitto`

**mTLS cert issuance (enterprise feature):**
- Add a `POST /api/gateways/{id}/enable-mtls/` endpoint
- Django generates a client keypair + cert signed by the platform CA
- Stores `client_cert_pem`, `client_key_pem` on the `Gateway` record
- Pushes a remote config update to the gateway via `v1/gateway/{serial}/config` with `tls.mode = "mutual"` and the cert/key paths
- Gateway cert files are delivered as part of the config push (base64-encoded in the payload) or via a secure download URL

---

## MQTT Topic Contract

### Edge → Cloud (Inbound to Django)

| Topic | Purpose | Who subscribes |
|-------|---------|---------------|
| `v1/gateway/telemetry` | Telemetry data (already working) | `mqtt_consumer.py` |
| `v1/gateway/logs` | Gateway log entries | `mqtt_consumer.py` (new) |
| `v1/gateway/attributes` | Gateway attributes / heartbeat | `mqtt_consumer.py` (new) |
| `v1/gateway/rpc/response` | RPC command results | `mqtt_consumer.py` (new) |

### Cloud → Edge (Outbound from Django)

| Topic | Purpose | Who publishes |
|-------|---------|--------------|
| `v1/gateway/{serial_number}/config` | Push config updates to a specific gateway | New `mqtt_publisher.py` service |
| `v1/gateway/{serial_number}/rpc/request` | Send RPC commands to a specific gateway | New `mqtt_publisher.py` service |
| `v1/gateway/{serial_number}/attributes/request` | Push attributes to a specific gateway | New `mqtt_publisher.py` service |

**Note:** `{serial_number}` is the gateway's unique identifier (e.g., `NF-EDGE-001`), stored in the `Gateway.serial_number` field.

---

## Payload Schemas

### 1. Telemetry (already implemented — no changes)

**Topic:** `v1/gateway/telemetry`

```json
{
  "serial_number": "NF-EDGE-001",
  "ts": 1714000000000,
  "values": {
    "device_name": "Power Meter 1",
    "active_power": 450.2,
    "voltage": 230.1,
    "status": "active"
  }
}
```

### 2. Remote Logging

**Topic:** `v1/gateway/logs` (edge → cloud)

```json
{
  "serial_number": "NF-EDGE-001",
  "ts": 1714000000000,
  "logs": [
    {
      "ts": 1714000000000,
      "level": "INFO",
      "logger": "nodeflow_edge.connectors.modbus",
      "message": "2024-04-25 10:00:00 - INFO - [modbus_connector] - modbus_connector - 142 - Polled 3 registers",
      "module": "modbus_connector",
      "line": 142
    },
    {
      "ts": 1714000000500,
      "level": "WARNING",
      "logger": "nodeflow_edge.gateway",
      "message": "2024-04-25 10:00:01 - WARNING - [nodeflow_gateway] - nodeflow_gateway - 55 - Connection timeout",
      "module": "nodeflow_gateway",
      "line": 55
    }
  ]
}
```

**Fields:**
- `serial_number` — identifies the gateway
- `ts` — batch timestamp (ms since epoch)
- `logs` — array of log entries (batched, typically 1–20 per message)
- Each log entry: `ts` (ms), `level` (DEBUG/INFO/WARNING/ERROR/CRITICAL), `logger` (Python logger name), `message` (formatted log string), `module`, `line`

### 3. Attribute Sync

**Topic:** `v1/gateway/attributes` (edge → cloud)

```json
{
  "serial_number": "NF-EDGE-001",
  "ts": 1714000000000,
  "attributes": {
    "firmware_version": "0.1.0",
    "ip_address": "192.168.1.50",
    "uptime_seconds": 3600,
    "python_version": "3.9.18",
    "platform": "Linux-5.15.0-armv7l-with-glibc2.31",
    "connected_devices": ["Power Meter 1", "Temp Sensor 2"],
    "active_connectors": ["Modbus TCP Connector"],
    "device_count": 2,
    "status": "online",
    "active_interface": "wwan0",
    "failover_count": 2,
    "ethernet_status": "zombie",
    "wifi_status": "down",
    "fourg_status": "up",
    "signal_strength": -75,
    "config_update_status": "success",
    "config_update_request_id": "uuid-string",
    "config_update_error": null,
    "config_version_ts": 1714000000000
  }
}
```

**Behavior:**
- Published on startup and every 60 seconds (heartbeat)
- Published after config update (contains `config_update_status` fields)
- `status` will be `"offline"` in the final message before shutdown

**LWT (Last Will and Testament) — Offline Detection:**

The `status: offline` message on `v1/gateway/attributes` may arrive in two ways:

1. **Graceful shutdown** — the gateway publishes it explicitly before stopping
2. **LWT (crash / power loss)** — the broker publishes it automatically after `keepalive × 1.5` seconds (~90s)

The Django consumer **must handle both identically**: update `Gateway.status = "offline"` and `Gateway.last_seen`. Do NOT filter out or deduplicate these — always process them. The cloud should also implement its own heartbeat timeout (e.g., mark offline if no heartbeat in 120 seconds) as a safety net for edge cases where LWT does not fire (clean TCP close without a final offline message).

**Topic:** `v1/gateway/{serial_number}/attributes/request` (cloud → edge)

```json
{
  "request_id": "abc-123-uuid",
  "attributes": {
    "custom_label": "Factory Floor A"
  }
}
```

### 4. Remote Config

**Topic:** `v1/gateway/{serial_number}/config` (cloud → edge)

```json
{
  "request_id": "uuid-string",
  "action": "full_update",
  "config": {
    "gateway": { "serial_number": "NF-EDGE-001" },
    "mqtt": {
      "host": "mqtt.nodeflow.io",
      "port": 8883,
      "topic": "v1/gateway/telemetry",
      "qos": 1,
      "username": "NF-EDGE-001",
      "password": "provisioned-password",
      "tls": {
        "mode": "one-way",
        "ca_certs": "/opt/nodeflow-edge/certs/ca.crt"
      }
    },
    "features": { ... },
    "connectors": [ ... ]
  }
}
```

**mTLS upgrade push** — when an enterprise customer enables mTLS from the dashboard, push a config update with:

```json
"tls": {
  "mode": "mutual",
  "ca_certs": "/opt/nodeflow-edge/certs/ca.crt",
  "certfile": "/opt/nodeflow-edge/certs/client.crt",
  "keyfile":  "/opt/nodeflow-edge/certs/client.key"
}
```

The gateway will validate the cert files exist before applying the new TLS mode. The client cert and key must be deployed to the gateway before or alongside this config push (e.g., via a secure download endpoint or base64-encoded in the payload).

**Supported actions:**

| Action | `config` field contains | What it does |
|--------|------------------------|-------------|
| `full_update` | Complete config.json content | Replace entire config, restart all connectors |
| `connector_update` | Single connector object `{"type": "modbus", "name": "...", "config": {...}}` | Update one connector, restart all |
| `connector_add` | Single connector object | Add a new connector, restart all |
| `connector_remove` | `{"name": "Connector Name"}` | Remove a connector by name, restart all |

**Edge response:** The edge publishes an acknowledgement via `v1/gateway/attributes`:
```json
{
  "serial_number": "NF-EDGE-001",
  "ts": 1714000000000,
  "attributes": {
    "config_update_status": "success",
    "config_update_request_id": "uuid-string",
    "config_update_error": null,
    "config_version_ts": 1714000000000
  }
}
```

On failure, `config_update_status` is `"failed"` and `config_update_error` contains the error message.

### 5. RPC Commands

**Topic:** `v1/gateway/{serial_number}/rpc/request` (cloud → edge)

```json
{
  "request_id": "uuid-string",
  "method": "ping",
  "params": {}
}
```

**Supported methods:**

| Method | Params | Description |
|--------|--------|-------------|
| `ping` | `{}` | Connectivity check, returns `{"pong": true, "ts": ..., "uptime_seconds": ...}` |
| `get_config` | `{}` | Returns `{"config": {...}}` — full current config.json |
| `get_status` | `{}` | Returns `{"serial_number": ..., "uptime_seconds": ..., "mqtt_connected": ..., "device_count": ..., "devices": [...], "connectors": [...]}` |
| `get_devices` | `{}` | Returns `{"devices": {...}}` — device registry |
| `set_log_level` | `{"level": "DEBUG", "logger": "nodeflow_edge.gateway"}` | Change runtime log level. `logger` is optional (defaults to root) |
| `restart_connector` | `{"name": "Modbus TCP Connector"}` | Restart a specific connector by name |
| `restart_all` | `{}` | Restart all connectors |
| `reboot` | `{"delay_seconds": 5}` | Reboot the host (requires root/systemd) |
| `write_device` | `{"device_name": "VFD Motor 1", "functionCode": 6, "address": 2, "value": 1500, "type": "16uint"}` | Write to a physical device register (Modbus FC 5/6/15/16) |
| `read_device` | `{"device_name": "Power Meter 1", "functionCode": 3, "address": 3060, "objectsCount": 2, "type": "32float"}` | On-demand read from a physical device (Modbus FC 1/2/3/4) |

**Topic:** `v1/gateway/rpc/response` (edge → cloud)

```json
{
  "serial_number": "NF-EDGE-001",
  "request_id": "uuid-string",
  "method": "ping",
  "ts": 1714000000000,
  "status": "success",
  "result": { "pong": true, "ts": 1714000000000, "uptime_seconds": 3600 },
  "error": null
}
```

On failure: `status` is `"error"` and `error` contains the error message.

### Device Control Examples

These are the most important RPC methods for end-users. They allow controlling physical devices (PLCs, VFDs, motors, pumps) from the cloud dashboard.

**Write to a VFD** — set motor speed to 1500 RPM:
```json
{
  "request_id": "uuid-123",
  "method": "write_device",
  "params": {
    "device_name": "VFD Motor 1",
    "functionCode": 6,
    "address": 2,
    "value": 1500,
    "type": "16uint",
    "objectsCount": 1
  }
}
```

Response:
```json
{
  "serial_number": "NF-EDGE-001",
  "request_id": "uuid-123",
  "method": "write_device",
  "ts": 1714000000000,
  "status": "success",
  "result": {
    "device_name": "VFD Motor 1",
    "operation": "write",
    "functionCode": 6,
    "address": 2,
    "value": 1500,
    "response": {"success": true}
  },
  "error": null
}
```

**On-demand read** — read active power from a meter:
```json
{
  "request_id": "uuid-456",
  "method": "read_device",
  "params": {
    "device_name": "Power Meter 1",
    "functionCode": 3,
    "address": 3060,
    "objectsCount": 2,
    "type": "32float"
  }
}
```

**write_device params reference:**

| Param | Required | Description |
|-------|----------|-------------|
| `device_name` | Yes | Name of the device as registered in the gateway |
| `functionCode` | Yes | Modbus write FC: 5 (single coil), 6 (single register), 15 (multi coil), 16 (multi register) |
| `address` | Yes | Register/coil address |
| `value` | Yes | Value to write |
| `type` | No | Data type for encoding (e.g., `16uint`, `16int`, `32float`) |
| `objectsCount` | No | Number of registers (default 1) |
| `timeout` | No | Timeout in seconds (default 5.0) |

**read_device params reference:**

| Param | Required | Description |
|-------|----------|-------------|
| `device_name` | Yes | Name of the device as registered in the gateway |
| `functionCode` | Yes | Modbus read FC: 1 (coils), 2 (discrete inputs), 3 (holding registers), 4 (input registers) |
| `address` | Yes | Register/coil address |
| `objectsCount` | No | Number of registers to read (default 1) |
| `type` | No | Data type for decoding (e.g., `32float`, `16uint`) |
| `timeout` | No | Timeout in seconds (default 5.0) |

---

## Django Implementation Guide

### Step 1: New Models

Create these in the appropriate apps.

**`apps/telemetry/models.py`** — add:

```python
class GatewayLog(models.Model):
    """Log entries received from edge gateways."""
    gateway = models.ForeignKey('devices.Gateway', on_delete=models.CASCADE, related_name='logs')
    timestamp = models.DateTimeField(db_index=True)
    level = models.CharField(max_length=20)  # INFO, WARNING, ERROR, etc.
    logger_name = models.CharField(max_length=200)
    message = models.TextField()
    module = models.CharField(max_length=100, blank=True)
    line = models.IntegerField(null=True, blank=True)

    class Meta:
        ordering = ['-timestamp']
        indexes = [
            models.Index(fields=['gateway', 'timestamp']),
            models.Index(fields=['gateway', 'level']),
        ]

    def __str__(self):
        return f"[{self.level}] {self.gateway.serial_number} @ {self.timestamp}"
```

**`apps/devices/models.py`** — add to `Gateway` model or create new model:

```python
class GatewayConfig(models.Model):
    """Tracks config versions pushed to gateways."""
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name='config_history')
    config_json = models.JSONField()
    pushed_at = models.DateTimeField(auto_now_add=True)
    request_id = models.UUIDField(unique=True)
    status = models.CharField(max_length=20, default='pending',
                              choices=[('pending', 'Pending'), ('success', 'Success'), ('failed', 'Failed')])
    error_message = models.TextField(blank=True)
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-pushed_at']

class RpcCommand(models.Model):
    """Tracks RPC commands sent to gateways."""
    gateway = models.ForeignKey(Gateway, on_delete=models.CASCADE, related_name='rpc_commands')
    request_id = models.UUIDField(unique=True)
    method = models.CharField(max_length=50)
    params = models.JSONField(default=dict)
    sent_at = models.DateTimeField(auto_now_add=True)
    status = models.CharField(max_length=20, default='pending',
                              choices=[('pending', 'Pending'), ('success', 'Success'), ('error', 'Error'), ('timeout', 'Timeout')])
    result = models.JSONField(null=True, blank=True)
    error_message = models.TextField(blank=True)
    responded_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ['-sent_at']
```

### Step 2: Update `Gateway` model

Add these fields to the existing `Gateway` model in `apps/devices/models.py`:

```python
# Add to the existing Gateway model:
ip_address = models.GenericIPAddressField(null=True, blank=True)
uptime_seconds = models.IntegerField(null=True, blank=True)
python_version = models.CharField(max_length=20, blank=True)
platform_info = models.CharField(max_length=200, blank=True)
active_connectors = models.JSONField(default=list, blank=True)
connected_devices = models.JSONField(default=list, blank=True)
```

### Step 3: Create `apps/telemetry/mqtt_publisher.py`

This is a new service that publishes messages FROM the cloud TO the edge gateways.

```python
import json
import logging
import uuid
from django.conf import settings
import paho.mqtt.client as mqtt

logger = logging.getLogger('iot_platform')

# Singleton MQTT client for publishing
_client = None
_connected = False

def get_mqtt_client():
    """Get or create the singleton MQTT publish client."""
    global _client, _connected
    if _client is not None:
        return _client

    _client = mqtt.Client(
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
        client_id=settings.MQTT_PUBLISHER_CLIENT_ID,  # e.g., "nodeflow-cloud-publisher"
        protocol=mqtt.MQTTv311
    )

    def on_connect(client, userdata, flags, rc, properties=None):
        global _connected
        if rc == 0:
            _connected = True
            logger.info("MQTT publisher connected to broker")
        else:
            logger.error("MQTT publisher connection failed: %s", rc)

    def on_disconnect(client, userdata, flags, rc, properties=None):
        global _connected
        _connected = False
        logger.warning("MQTT publisher disconnected")

    _client.on_connect = on_connect
    _client.on_disconnect = on_disconnect

    try:
        _client.connect(settings.MQTT_BROKER_HOST, settings.MQTT_BROKER_PORT, 60)
        _client.loop_start()
    except Exception as e:
        logger.error("Failed to connect MQTT publisher: %s", e)

    return _client

def publish_config_update(gateway, action, config):
    """
    Push a config update to a gateway.
    
    Args:
        gateway: Gateway model instance
        action: 'full_update', 'connector_update', 'connector_add', 'connector_remove'
        config: dict — config content appropriate for the action
    
    Returns:
        GatewayConfig instance
    """
    from apps.devices.models import GatewayConfig
    
    request_id = uuid.uuid4()
    topic = f"v1/gateway/{gateway.serial_number}/config"
    payload = {
        "request_id": str(request_id),
        "action": action,
        "config": config,
    }

    # Store in DB
    config_record = GatewayConfig.objects.create(
        gateway=gateway,
        config_json=config,
        request_id=request_id,
    )

    # Publish
    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(payload), qos=1)
    logger.info("Published config update to %s (request_id=%s, rc=%s)",
                gateway.serial_number, request_id, result.rc)

    return config_record

def publish_rpc_command(gateway, method, params=None):
    """
    Send an RPC command to a gateway.
    
    Args:
        gateway: Gateway model instance
        method: str — command method name
        params: dict — command parameters
    
    Returns:
        RpcCommand instance
    """
    from apps.devices.models import RpcCommand
    
    request_id = uuid.uuid4()
    topic = f"v1/gateway/{gateway.serial_number}/rpc/request"
    payload = {
        "request_id": str(request_id),
        "method": method,
        "params": params or {},
    }

    # Store in DB
    rpc_record = RpcCommand.objects.create(
        gateway=gateway,
        request_id=request_id,
        method=method,
        params=params or {},
    )

    # Publish
    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(payload), qos=1)
    logger.info("Published RPC command '%s' to %s (request_id=%s, rc=%s)",
                method, gateway.serial_number, request_id, result.rc)

    return rpc_record

def publish_attribute_push(gateway, attributes):
    """
    Push attributes to a gateway.
    
    Args:
        gateway: Gateway model instance
        attributes: dict — key-value pairs to push
    """
    request_id = str(uuid.uuid4())
    topic = f"v1/gateway/{gateway.serial_number}/attributes/request"
    payload = {
        "request_id": request_id,
        "attributes": attributes,
    }

    client = get_mqtt_client()
    result = client.publish(topic, json.dumps(payload), qos=1)
    logger.info("Published attribute push to %s (request_id=%s)", gateway.serial_number, request_id)
```

### Step 4: Update `mqtt_consumer.py`

Modify `apps/telemetry/management/commands/mqtt_consumer.py` to subscribe to the 3 new inbound topics and handle them.

```python
# In the on_connect method, add these subscriptions:
client.subscribe("v1/gateway/telemetry")    # existing
client.subscribe("v1/gateway/logs")          # NEW
client.subscribe("v1/gateway/attributes")    # NEW
client.subscribe("v1/gateway/rpc/response")  # NEW

# In on_message, route by topic:
def on_message(self, client, userdata, msg):
    try:
        payload = json.loads(msg.payload.decode())
        
        if msg.topic == "v1/gateway/telemetry":
            self._handle_telemetry(payload)
        elif msg.topic == "v1/gateway/logs":
            self._handle_logs(payload)
        elif msg.topic == "v1/gateway/attributes":
            self._handle_attributes(payload)
        elif msg.topic == "v1/gateway/rpc/response":
            self._handle_rpc_response(payload)
        else:
            logger.warning("Unknown topic: %s", msg.topic)
    except Exception as e:
        logger.error("Error processing MQTT message on %s: %s", msg.topic, e)
```

#### Handler: `_handle_telemetry` (existing logic, no changes needed)

#### Handler: `_handle_logs`

```python
def _handle_logs(self, payload):
    """Ingest log entries from a gateway."""
    from apps.devices.models import Gateway
    from apps.telemetry.models import GatewayLog
    
    gateway_sn = payload.get('serial_number')
    if not gateway_sn:
        return
    
    try:
        gateway = Gateway.objects.get(serial_number=gateway_sn)
    except Gateway.DoesNotExist:
        logger.warning("Gateway %s not found for log ingestion", gateway_sn)
        return
    
    log_entries = payload.get('logs', [])
    log_objects = []
    for entry in log_entries:
        ts = entry.get('ts')
        dt = timezone.datetime.fromtimestamp(ts / 1000.0, tz=timezone.utc) if ts else timezone.now()
        
        log_objects.append(GatewayLog(
            gateway=gateway,
            timestamp=dt,
            level=entry.get('level', 'INFO'),
            logger_name=entry.get('logger', ''),
            message=entry.get('message', ''),
            module=entry.get('module', ''),
            line=entry.get('line'),
        ))
    
    if log_objects:
        GatewayLog.objects.bulk_create(log_objects)
        logger.debug("Ingested %d log entries from %s", len(log_objects), gateway_sn)
```

#### Handler: `_handle_attributes`

```python
def _handle_attributes(self, payload):
    """Update gateway status from heartbeat attributes."""
    from apps.devices.models import Gateway, GatewayConfig
    
    gateway_sn = payload.get('serial_number')
    if not gateway_sn:
        return
    
    try:
        gateway = Gateway.objects.get(serial_number=gateway_sn)
    except Gateway.DoesNotExist:
        logger.warning("Gateway %s not found for attribute sync", gateway_sn)
        return
    
    attrs = payload.get('attributes', {})
    
    # Update gateway fields
    update_fields = ['last_seen']
    gateway.last_seen = timezone.now()
    
    if 'status' in attrs:
        gateway.status = attrs['status']
        update_fields.append('status')
    if 'firmware_version' in attrs:
        gateway.firmware_version = attrs['firmware_version']
        update_fields.append('firmware_version')
    if 'ip_address' in attrs:
        gateway.ip_address = attrs['ip_address']
        update_fields.append('ip_address')
    if 'uptime_seconds' in attrs:
        gateway.uptime_seconds = attrs['uptime_seconds']
        update_fields.append('uptime_seconds')
    if 'connected_devices' in attrs:
        gateway.connected_devices = attrs['connected_devices']
        update_fields.append('connected_devices')
    if 'active_connectors' in attrs:
        gateway.active_connectors = attrs['active_connectors']
        update_fields.append('active_connectors')
    
    gateway.save(update_fields=update_fields)
    
    # Handle config update acknowledgement
    config_request_id = attrs.get('config_update_request_id')
    if config_request_id:
        try:
            config_record = GatewayConfig.objects.get(request_id=config_request_id)
            config_record.status = attrs.get('config_update_status', 'unknown')
            config_record.error_message = attrs.get('config_update_error', '') or ''
            config_record.acknowledged_at = timezone.now()
            config_record.save()
            logger.info("Config update %s acknowledged: %s", config_request_id, config_record.status)
        except GatewayConfig.DoesNotExist:
            pass
    
    logger.debug("Updated attributes for gateway %s", gateway_sn)
```

#### Handler: `_handle_rpc_response`

```python
def _handle_rpc_response(self, payload):
    """Process RPC command response from a gateway."""
    from apps.devices.models import RpcCommand
    
    request_id = payload.get('request_id')
    if not request_id:
        return
    
    try:
        rpc_record = RpcCommand.objects.get(request_id=request_id)
        rpc_record.status = payload.get('status', 'unknown')
        rpc_record.result = payload.get('result')
        rpc_record.error_message = payload.get('error', '') or ''
        rpc_record.responded_at = timezone.now()
        rpc_record.save()
        logger.info("RPC response for %s (%s): %s", request_id, payload.get('method'), rpc_record.status)
    except RpcCommand.DoesNotExist:
        logger.warning("RPC response for unknown request_id: %s", request_id)
```

### Step 5: Django Settings

Add to `settings.py`:

```python
# MQTT Configuration
MQTT_BROKER_HOST = env('MQTT_BROKER_HOST', default='localhost')
MQTT_BROKER_PORT = env.int('MQTT_BROKER_PORT', default=1883)
MQTT_CONSUMER_CLIENT_ID = env('MQTT_CONSUMER_CLIENT_ID', default='nodeflow-cloud-consumer')
MQTT_PUBLISHER_CLIENT_ID = env('MQTT_PUBLISHER_CLIENT_ID', default='nodeflow-cloud-publisher')
```

### Step 6: API Endpoints / Views

Add views for the dashboard to trigger cloud → edge actions.

**`apps/devices/views.py`** — add these views:

```python
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from apps.teams.decorators import login_and_team_required

@login_and_team_required
@require_POST
def gateway_send_rpc(request, team_slug, pk):
    """Send an RPC command to a gateway from the dashboard."""
    from apps.telemetry.mqtt_publisher import publish_rpc_command
    
    gateway = Gateway.objects.get(pk=pk, team=request.team)
    method = request.POST.get('method')
    params = json.loads(request.POST.get('params', '{}'))
    
    rpc = publish_rpc_command(gateway, method, params)
    
    return JsonResponse({
        'request_id': str(rpc.request_id),
        'method': method,
        'status': 'sent'
    })

@login_and_team_required
@require_POST
def gateway_push_config(request, team_slug, pk):
    """Push a config update to a gateway."""
    from apps.telemetry.mqtt_publisher import publish_config_update
    
    gateway = Gateway.objects.get(pk=pk, team=request.team)
    action = request.POST.get('action', 'full_update')
    config = json.loads(request.POST.get('config'))
    
    config_record = publish_config_update(gateway, action, config)
    
    return JsonResponse({
        'request_id': str(config_record.request_id),
        'action': action,
        'status': 'sent'
    })

@login_and_team_required
def gateway_logs(request, team_slug, pk):
    """View gateway logs."""
    from apps.telemetry.models import GatewayLog
    
    gateway = Gateway.objects.get(pk=pk, team=request.team)
    level = request.GET.get('level')
    
    logs = GatewayLog.objects.filter(gateway=gateway)
    if level:
        logs = logs.filter(level=level)
    logs = logs[:200]
    
    return render(request, 'devices/gateway_logs.html', {
        'gateway': gateway,
        'logs': logs,
    })

@login_and_team_required
def gateway_rpc_history(request, team_slug, pk):
    """View RPC command history for a gateway."""
    from apps.devices.models import RpcCommand
    
    gateway = Gateway.objects.get(pk=pk, team=request.team)
    commands = RpcCommand.objects.filter(gateway=gateway)[:50]
    
    return render(request, 'devices/gateway_rpc_history.html', {
        'gateway': gateway,
        'commands': commands,
    })
```

**`apps/devices/urls.py`** — add:

```python
path('gateways/<int:pk>/rpc/', views.gateway_send_rpc, name='gateway_send_rpc'),
path('gateways/<int:pk>/config/', views.gateway_push_config, name='gateway_push_config'),
path('gateways/<int:pk>/logs/', views.gateway_logs, name='gateway_logs'),
path('gateways/<int:pk>/rpc-history/', views.gateway_rpc_history, name='gateway_rpc_history'),
```

### Step 7: Run Migrations

```bash
python manage.py makemigrations devices telemetry
python manage.py migrate
```

---

## Testing

### Manual Test: Ping a Gateway

```python
# In Django shell:
from apps.devices.models import Gateway
from apps.telemetry.mqtt_publisher import publish_rpc_command

gw = Gateway.objects.get(serial_number='NF-EDGE-001')
rpc = publish_rpc_command(gw, 'ping')
print(f"Sent ping, request_id={rpc.request_id}")

# Wait a few seconds, then check:
rpc.refresh_from_db()
print(f"Status: {rpc.status}, Result: {rpc.result}")
```

### Manual Test: Push Config Update

```python
from apps.telemetry.mqtt_publisher import publish_config_update

new_connector = {
    "type": "modbus",
    "name": "New Modbus Device",
    "config": {
        "master": {
            "slaves": [{
                "host": "192.168.1.200",
                "port": 502,
                "type": "tcp",
                "unitId": 2,
                "deviceName": "VFD Motor 1",
                "pollPeriod": 5000,
                "timeseries": [
                    {"tag": "motor_speed", "type": "16uint", "functionCode": 3, "address": 2}
                ]
            }]
        }
    }
}
config = publish_config_update(gw, 'connector_add', new_connector)
```

### Manual Test: View Logs

After the gateway has been running for a while:
```python
from apps.telemetry.models import GatewayLog
GatewayLog.objects.filter(gateway=gw).order_by('-timestamp')[:10]
```

---

## Summary Checklist

### Security & Broker Configuration
- [ ] Configure Mosquitto with TLS (port 8883, server cert + key, CA cert)
- [ ] Add `password_file` and `acl_file` to `mosquitto.conf`; set `allow_anonymous false`
- [ ] Add `mqtt_username`, `mqtt_password`, `tls_mode`, `client_cert_pem`, `client_key_pem` fields to `Gateway` model
- [ ] Auto-generate MQTT credentials on gateway registration
- [ ] Add `POST /api/gateways/{id}/enable-mtls/` endpoint (enterprise feature)
- [ ] mTLS: cert issuance flow (generate, sign with CA, store, push config update to edge)
- [ ] Handle LWT offline messages in `mqtt_consumer` identically to graceful shutdown
- [ ] Implement cloud-side heartbeat timeout (mark offline if no heartbeat in 120s)
- [ ] Include `tls` block in all `full_update` config pushes
- [ ] Display `firmware_version` (from heartbeat attributes) on gateway dashboard

### Django Backend
- [ ] Add `GatewayLog` model to `apps/telemetry/models.py`
- [ ] Add `GatewayConfig` and `RpcCommand` models to `apps/devices/models.py`
- [ ] Add new fields to `Gateway` model (`ip_address`, `uptime_seconds`, etc.)
- [ ] Create `apps/telemetry/mqtt_publisher.py` with publish functions
- [ ] Update `mqtt_consumer.py` to subscribe to 3 new topics and handle them
- [ ] Add `MQTT_PUBLISHER_CLIENT_ID` to Django settings
- [ ] Add API views for dashboard (send RPC, push config, view logs)
- [ ] Add URL routes
- [ ] Run migrations
- [ ] Create templates for gateway logs and RPC history pages
- [ ] Create device control UI ("Send Command" panel on device detail page)
- [ ] Test end-to-end with a running Nodeflow Edge gateway

---

## Device Control UI Guidance

The dashboard should provide a **"Send Command"** panel on the device detail page. Suggested layout:

### Device Detail Page — Control Panel

**Read Section:**
- Dropdown: Function Code (1=Coils, 2=Discrete, 3=Holding Registers, 4=Input Registers)
- Input: Register Address
- Input: Objects Count (default 1)
- Input: Data Type (dropdown: 16uint, 16int, 32float, etc.)
- Button: "Read Now"
- Shows: last read result + timestamp

**Write Section:**
- Dropdown: Function Code (5=Single Coil, 6=Single Register, 15=Multi Coil, 16=Multi Register)
- Input: Register Address
- Input: Value
- Input: Data Type (dropdown)
- Button: "Write" (with confirmation dialog)
- Shows: write result + timestamp

**Quick Actions (pre-configured from device template register map):**
- For devices with a known register map (from `DeviceTemplate`), display friendly buttons:
  - "Set Speed: [input] RPM" → writes to the speed register
  - "Start/Stop" toggle → writes 1/0 to the control coil
  - "Read All Registers" → batch read of all mapped registers

### API Endpoints for Device Control

Add to `apps/devices/views.py`:

```python
@login_and_team_required
@require_POST
def device_send_command(request, team_slug, gateway_pk, device_pk):
    """Send a write/read command to a device via its gateway."""
    from apps.telemetry.mqtt_publisher import publish_rpc_command

    gateway = Gateway.objects.get(pk=gateway_pk, team=request.team)
    device = Device.objects.get(pk=device_pk, gateway=gateway)

    method = request.POST.get('method')  # 'write_device' or 'read_device'
    params = json.loads(request.POST.get('params', '{}'))
    params['device_name'] = device.name  # Inject the device name

    rpc = publish_rpc_command(gateway, method, params)

    return JsonResponse({
        'request_id': str(rpc.request_id),
        'method': method,
        'device': device.name,
        'status': 'sent'
    })
```

Add URL:
```python
path('gateways/<int:gateway_pk>/devices/<int:device_pk>/command/',
     views.device_send_command, name='device_send_command'),
```
