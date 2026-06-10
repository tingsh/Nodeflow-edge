"""
Nodeflow Edge Payload Formatter

Converts ConvertedData objects from TB-style connectors into the
Nodeflow Cloud JSON schema:

{
    "serial_number": "NF-EDGE-001",
    "values": {
        "device_name": "Power Meter 1",
        "active_power": 450.2,
        "voltage": 230.1,
        "status": "active"
    }
}
"""

import logging
from typing import List

from nodeflow_edge.gateway.entities.converted_data import ConvertedData
from nodeflow_edge.gateway.entities.datapoint_key import DatapointKey

log = logging.getLogger("nodeflow_edge.payload_formatter")


class PayloadFormatter:
    """Transforms ConvertedData from connectors into Nodeflow Cloud payloads."""

    def __init__(self, serial_number: str):
        self._serial_number = serial_number

    def format(self, converted_data: ConvertedData) -> List[dict]:
        """
        Convert a ConvertedData object into one or more Nodeflow Cloud payloads.

        Returns a list of payload dicts ready for MQTT publishing.
        Each telemetry entry becomes a separate payload; attributes are
        sent as a single payload.
        """
        payloads = []
        device_id = getattr(converted_data, "device_id", None)

        # Process telemetry entries
        for telemetry_entry in converted_data.telemetry:
            values = {}
            for key, value in telemetry_entry.values.items():
                key_str = key.key if isinstance(key, DatapointKey) else str(key)
                values[key_str] = value

            values["device_name"] = converted_data.device_name

            payload = {
                "serial_number": self._serial_number,
                "ts": telemetry_entry.ts,
                "device_name": converted_data.device_name,
                "values": values,
            }
            if device_id:
                payload["device_id"] = device_id
            payloads.append(payload)

        # Process attributes (if any)
        attr_dict = converted_data.attributes.to_dict()
        if attr_dict:
            values = {}
            values.update(attr_dict)
            values["device_name"] = converted_data.device_name
            payload = {
                "serial_number": self._serial_number,
                "device_name": converted_data.device_name,
                "values": values,
            }
            if device_id:
                payload["device_id"] = device_id
            payloads.append(payload)

        if not payloads:
            log.debug("No data to format for device %s", converted_data.device_name)

        return payloads
