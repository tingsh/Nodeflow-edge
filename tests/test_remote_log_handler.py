"""Unit tests for the RemoteLogHandler."""

import sys
import os
import logging
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from nodeflow_edge.gateway.remote_log_handler import RemoteLogHandler


class TestRemoteLogHandler(unittest.TestCase):

    def setUp(self):
        self.mock_publisher = MagicMock()
        self.handler = RemoteLogHandler(
            publisher=self.mock_publisher,
            serial_number="NF-TEST-001",
            config={"enabled": True, "min_level": "INFO", "batch_size": 5,
                     "flush_interval_seconds": 999}
        )

    def test_emit_buffers_log_records(self):
        """Log records should be buffered until flush."""
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO,
            pathname="test.py", lineno=10,
            msg="Test message", args=(), exc_info=None
        )
        self.handler.emit(record)

        self.assertEqual(len(self.handler._buffer), 1)
        self.assertEqual(self.handler._buffer[0]["level"], "INFO")
        self.assertIn("Test message", self.handler._buffer[0]["message"])

    def test_emit_skips_below_min_level(self):
        """DEBUG records should be skipped when min_level is INFO."""
        record = logging.LogRecord(
            name="test.logger", level=logging.DEBUG,
            pathname="test.py", lineno=10,
            msg="Debug msg", args=(), exc_info=None
        )
        self.handler.emit(record)
        self.assertEqual(len(self.handler._buffer), 0)

    def test_emit_skips_own_logger(self):
        """Logs from the MQTT publisher should be skipped to prevent infinite loops."""
        record = logging.LogRecord(
            name="nodeflow_edge.mqtt_publisher", level=logging.INFO,
            pathname="test.py", lineno=10,
            msg="Publishing", args=(), exc_info=None
        )
        self.handler.emit(record)
        self.assertEqual(len(self.handler._buffer), 0)

    def test_flush_publishes_batch(self):
        """Flush should publish buffered logs via the publisher."""
        for i in range(3):
            record = logging.LogRecord(
                name="test", level=logging.WARNING,
                pathname="test.py", lineno=i,
                msg=f"Warning {i}", args=(), exc_info=None
            )
            self.handler.emit(record)

        self.handler._flush()

        self.mock_publisher.publish_logs.assert_called_once()
        payload = self.mock_publisher.publish_logs.call_args[0][0]
        self.assertEqual(payload["serial_number"], "NF-TEST-001")
        self.assertEqual(len(payload["logs"]), 3)
        self.assertIn("ts", payload)

    def test_flush_clears_buffer(self):
        """After flush, buffer should be empty."""
        record = logging.LogRecord(
            name="test", level=logging.ERROR,
            pathname="test.py", lineno=1,
            msg="Error", args=(), exc_info=None
        )
        self.handler.emit(record)
        self.handler._flush()

        self.assertEqual(len(self.handler._buffer), 0)

    def test_disabled_handler_does_not_buffer(self):
        """When disabled, emit should not buffer records."""
        handler = RemoteLogHandler(
            publisher=self.mock_publisher,
            serial_number="NF-TEST-001",
            config={"enabled": False}
        )
        record = logging.LogRecord(
            name="test", level=logging.ERROR,
            pathname="test.py", lineno=1,
            msg="Error", args=(), exc_info=None
        )
        handler.emit(record)
        self.assertEqual(len(handler._buffer), 0)


if __name__ == "__main__":
    unittest.main()
