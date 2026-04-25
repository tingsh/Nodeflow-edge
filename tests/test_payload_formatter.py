"""Unit tests for the Nodeflow Edge payload formatter."""

import sys
import os
import unittest

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodeflow_edge.gateway.payload_formatter import PayloadFormatter
from nodeflow_edge.gateway.entities.converted_data import ConvertedData
from nodeflow_edge.gateway.entities.datapoint_key import DatapointKey
from nodeflow_edge.gateway.entities.telemetry_entry import TelemetryEntry


class TestPayloadFormatter(unittest.TestCase):

    def setUp(self):
        self.formatter = PayloadFormatter("NF-EDGE-001")

    def test_basic_telemetry_payload(self):
        """Verify that a ConvertedData with telemetry produces the Nodeflow Cloud schema."""
        cd = ConvertedData("Power Meter 1", "default")
        cd.add_to_telemetry(TelemetryEntry({
            DatapointKey("active_power"): 450.2,
            DatapointKey("voltage"): 230.1,
            DatapointKey("status"): "active"
        }))

        payloads = self.formatter.format(cd)

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["serial_number"], "NF-EDGE-001")
        self.assertIn("values", payload)
        self.assertEqual(payload["values"]["device_name"], "Power Meter 1")
        self.assertAlmostEqual(payload["values"]["active_power"], 450.2)
        self.assertAlmostEqual(payload["values"]["voltage"], 230.1)
        self.assertEqual(payload["values"]["status"], "active")
        self.assertIn("ts", payload)

    def test_multiple_telemetry_entries(self):
        """Each telemetry entry should produce a separate payload."""
        cd = ConvertedData("Sensor A", "default")
        cd.add_to_telemetry(TelemetryEntry({DatapointKey("temp"): 25.0}, ts=1000))
        cd.add_to_telemetry(TelemetryEntry({DatapointKey("temp"): 26.0}, ts=2000))

        payloads = self.formatter.format(cd)

        self.assertEqual(len(payloads), 2)
        self.assertEqual(payloads[0]["ts"], 1000)
        self.assertEqual(payloads[1]["ts"], 2000)
        self.assertEqual(payloads[0]["values"]["temp"], 25.0)
        self.assertEqual(payloads[1]["values"]["temp"], 26.0)

    def test_attributes_payload(self):
        """Attributes should produce a separate payload."""
        cd = ConvertedData("Device B", "default")
        cd.add_to_attributes({DatapointKey("firmware"): "v1.2.3"})

        payloads = self.formatter.format(cd)

        self.assertEqual(len(payloads), 1)
        payload = payloads[0]
        self.assertEqual(payload["serial_number"], "NF-EDGE-001")
        self.assertEqual(payload["values"]["device_name"], "Device B")
        self.assertEqual(payload["values"]["firmware"], "v1.2.3")

    def test_empty_data(self):
        """Empty ConvertedData should produce no payloads."""
        cd = ConvertedData("Empty Device")
        payloads = self.formatter.format(cd)
        self.assertEqual(len(payloads), 0)

    def test_serial_number_propagation(self):
        """Serial number from config must appear in every payload."""
        formatter = PayloadFormatter("NF-TEST-999")
        cd = ConvertedData("X")
        cd.add_to_telemetry(TelemetryEntry({DatapointKey("val"): 1}))
        payloads = formatter.format(cd)
        self.assertEqual(payloads[0]["serial_number"], "NF-TEST-999")


if __name__ == "__main__":
    unittest.main()
