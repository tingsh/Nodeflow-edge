"""
Nodeflow Edge Remote Config Handler

Subscribes to `v1/gateway/{serial_number}/config` for inbound config updates
from Nodeflow Cloud. On receiving a new config:
1. Validates the incoming JSON structure
2. Backs up the current config
3. Writes the new config to disk
4. Hot-reloads affected connectors (stop old → start new)
5. Publishes an acknowledgement attribute

Inbound Config Payload Schema (cloud → edge):
{
    "request_id": "uuid-string",
    "action": "full_update" | "connector_update" | "connector_add" | "connector_remove",
    "config": { ... }  // Full or partial config depending on action
}

Acknowledgement Attribute (edge → cloud):
{
    "serial_number": "NF-EDGE-001",
    "ts": 1714000000000,
    "attributes": {
        "config_update_status": "success" | "failed",
        "config_update_request_id": "uuid-string",
        "config_update_error": "..." | null,
        "config_version_ts": 1714000000000
    }
}
"""

import json
import logging
import os
import shutil
from time import time
from typing import Optional

log = logging.getLogger("nodeflow_edge.remote_config")

MAX_BACKUPS = 10


class RemoteConfigHandler:
    """Handles remote configuration updates from Nodeflow Cloud."""

    def __init__(self, gateway, publisher, serial_number: str,
                 config_path: str, config: Optional[dict] = None):
        self._gateway = gateway
        self._publisher = publisher
        self._serial_number = serial_number
        self._config_path = config_path
        self._handler_config = config or {}

        self._enabled = self._handler_config.get("enabled", True)
        self._inbound_topic = f"v1/gateway/{self._serial_number}/config"
        self._backup_dir = self._handler_config.get(
            "backup_dir",
            os.path.join(os.path.dirname(self._config_path), "config_backups")
        )

    def start(self):
        """Subscribe to the config update topic."""
        if not self._enabled:
            log.info("Remote config handler is disabled.")
            return

        self._publisher.subscribe(self._inbound_topic, self._on_config_update)
        log.info("Remote config handler started, listening on: %s", self._inbound_topic)

    def stop(self):
        """No persistent resources to clean up."""
        pass

    def _on_config_update(self, topic: str, payload: dict):
        """Handle an inbound config update message."""
        request_id = payload.get("request_id", "unknown")
        action = payload.get("action", "full_update")
        new_config = payload.get("config")

        log.info("Received config update (request_id=%s, action=%s)", request_id, action)

        try:
            if not new_config:
                raise ValueError("Config update payload is missing 'config' field")

            if action == "full_update":
                self._apply_full_update(new_config)
            elif action == "connector_update":
                self._apply_connector_update(new_config)
            elif action == "connector_add":
                self._apply_connector_add(new_config)
            elif action == "connector_remove":
                self._apply_connector_remove(new_config)
            else:
                raise ValueError(f"Unknown config action: {action}")

            self._send_ack(request_id, success=True)
            log.info("Config update applied successfully (request_id=%s)", request_id)

        except Exception as e:
            log.exception("Failed to apply config update (request_id=%s): %s", request_id, e)
            self._send_ack(request_id, success=False, error=str(e))

    def _apply_full_update(self, new_config: dict):
        """Replace the entire config.json and hot-reload all connectors."""
        # Validate required sections
        if "gateway" not in new_config:
            raise ValueError("Config missing 'gateway' section")
        if "mqtt" not in new_config:
            raise ValueError("Config missing 'mqtt' section")

        # Backup current config
        self._create_backup()

        # Write new config
        self._write_config(new_config)

        # Hot-reload connectors
        self._gateway._stop_connectors()
        self._gateway._config = new_config
        self._gateway._start_connectors()

        log.info("Full config update applied — %d connectors restarted",
                 len(self._gateway._connectors))

    def _apply_connector_update(self, connector_config: dict):
        """Update a specific connector's configuration."""
        connector_name = connector_config.get("name")
        if not connector_name:
            raise ValueError("Connector update missing 'name' field")

        # Load current config
        current_config = self._read_config()
        connectors = current_config.get("connectors", [])

        # Find and replace the connector config
        found = False
        for i, conn in enumerate(connectors):
            if conn.get("name") == connector_name:
                connectors[i] = connector_config
                found = True
                break

        if not found:
            raise ValueError(f"Connector '{connector_name}' not found in current config")

        # Backup, write, and reload
        self._create_backup()
        current_config["connectors"] = connectors
        self._write_config(current_config)

        # Hot-reload: stop all connectors and restart
        self._gateway._stop_connectors()
        self._gateway._config = current_config
        self._gateway._start_connectors()

        log.info("Connector '%s' updated and reloaded", connector_name)

    def _apply_connector_add(self, connector_config: dict):
        """Add a new connector to the config and start it."""
        connector_name = connector_config.get("name")
        if not connector_name:
            raise ValueError("Connector add missing 'name' field")

        current_config = self._read_config()
        connectors = current_config.get("connectors", [])

        # Check for duplicates
        for conn in connectors:
            if conn.get("name") == connector_name:
                raise ValueError(f"Connector '{connector_name}' already exists")

        connectors.append(connector_config)

        # Backup and write
        self._create_backup()
        current_config["connectors"] = connectors
        self._write_config(current_config)

        # Hot-reload all connectors
        self._gateway._stop_connectors()
        self._gateway._config = current_config
        self._gateway._start_connectors()

        log.info("Connector '%s' added and started", connector_name)

    def _apply_connector_remove(self, connector_config: dict):
        """Remove a connector from the config and stop it."""
        connector_name = connector_config.get("name")
        if not connector_name:
            raise ValueError("Connector remove missing 'name' field")

        current_config = self._read_config()
        connectors = current_config.get("connectors", [])

        original_count = len(connectors)
        connectors = [c for c in connectors if c.get("name") != connector_name]

        if len(connectors) == original_count:
            raise ValueError(f"Connector '{connector_name}' not found")

        # Backup and write
        self._create_backup()
        current_config["connectors"] = connectors
        self._write_config(current_config)

        # Hot-reload
        self._gateway._stop_connectors()
        self._gateway._config = current_config
        self._gateway._start_connectors()

        log.info("Connector '%s' removed", connector_name)

    def _read_config(self) -> dict:
        """Read the current config from disk."""
        with open(self._config_path, 'r') as f:
            return json.load(f)

    def _write_config(self, config: dict):
        """Write config to disk atomically."""
        tmp_path = self._config_path + ".tmp"
        with open(tmp_path, 'w') as f:
            json.dump(config, f, indent=2)
        os.replace(tmp_path, self._config_path)
        log.debug("Config written to %s", self._config_path)

    def _create_backup(self):
        """Backup the current config file with a timestamp."""
        if not os.path.exists(self._config_path):
            return

        os.makedirs(self._backup_dir, exist_ok=True)

        ts = int(time())
        backup_name = f"config_backup_{ts}.json"
        backup_path = os.path.join(self._backup_dir, backup_name)

        shutil.copy2(self._config_path, backup_path)
        log.info("Config backed up to %s", backup_path)

        # Prune old backups
        self._prune_backups()

    def _prune_backups(self):
        """Keep only the most recent MAX_BACKUPS backup files."""
        if not os.path.exists(self._backup_dir):
            return

        backups = sorted([
            f for f in os.listdir(self._backup_dir)
            if f.startswith("config_backup_") and f.endswith(".json")
        ])

        while len(backups) > MAX_BACKUPS:
            old = backups.pop(0)
            os.remove(os.path.join(self._backup_dir, old))
            log.debug("Pruned old backup: %s", old)

    def _send_ack(self, request_id: str, success: bool, error: str = None):
        """Publish a config update acknowledgement as a gateway attribute."""
        payload = {
            "serial_number": self._serial_number,
            "ts": int(time() * 1000),
            "attributes": {
                "config_update_status": "success" if success else "failed",
                "config_update_request_id": request_id,
                "config_update_error": error,
                "config_version_ts": int(time() * 1000),
            }
        }
        self._publisher.publish_attributes(payload)
