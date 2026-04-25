"""
Nodeflow Edge Discovery Service

Scans physical interfaces for connected Modbus devices and reports
findings to Nodeflow Cloud via MQTT attributes.

Supports:
- Modbus RTU scanning (serial ports, configurable slave range + baud rates)
- Modbus TCP scanning (local subnet, port 502)
- Device identification via FC43 MEI and vendor-specific registers
"""

import glob
import logging
import socket
import struct
import threading
from time import time, sleep
from typing import Optional

log = logging.getLogger("nodeflow_edge.discovery")

# Known vendor identification registers
VENDOR_ID_REGISTERS = {
    "eastron": {
        "address": 64515, "count": 1, "fc": 3,
        "models": {0x0089: "SDM630", 0x0001: "SDM120", 0x0070: "SDM72D"},
    },
    "abb": {"address": 1, "count": 1, "fc": 3},
    "schneider": {"address": 29, "count": 1, "fc": 3},
}


class DiscoveryService:
    """
    Scans physical interfaces for connected Modbus devices
    and reports findings to Nodeflow Cloud via MQTT attributes.
    """

    def __init__(self, gateway, publisher, serial_number: str, config: Optional[dict] = None):
        self._gateway = gateway
        self._publisher = publisher
        self._serial_number = serial_number
        self._config = config or {}

        self._enabled = self._config.get("enabled", False)
        self._scan_on_boot = self._config.get("scan_on_boot", True)
        self._rtu_slave_range = self._config.get("rtu_slave_range", [1, 32])
        self._rtu_baud_rates = self._config.get("rtu_baud_rates", [9600, 19200])
        self._tcp_subnet_scan = self._config.get("tcp_subnet_scan", True)
        self._tcp_scan_timeout_ms = self._config.get("tcp_scan_timeout_ms", 500)

        self._scan_thread = None
        self._stopped = False
        self._last_report = None

    def start(self):
        """If scan_on_boot is True, run a scan after a short delay."""
        if not self._enabled:
            log.info("Discovery service is disabled.")
            return

        if self._scan_on_boot:
            self._scan_thread = threading.Thread(
                target=self._boot_scan, name="DiscoveryBootScan", daemon=True
            )
            self._scan_thread.start()

    def stop(self):
        """Cancel any pending scans."""
        self._stopped = True

    def _boot_scan(self):
        """Run a scan after a delay to let MQTT connection establish."""
        sleep(10)
        if self._stopped:
            return
        log.info("Running boot discovery scan...")
        self.scan(scan_type="boot")

    def scan(self, scan_type: str = "manual") -> dict:
        """
        Execute a full discovery scan. Returns the discovery report dict.
        """
        log.info("Starting discovery scan (type=%s)...", scan_type)
        scan_ts = int(time() * 1000)

        interfaces = []
        discovered_devices = []
        errors = []

        # 1. Enumerate and scan serial interfaces for Modbus RTU
        serial_ports = self._enumerate_serial_ports()
        for port in serial_ports:
            interfaces.append({"name": port, "type": "serial", "label": self._serial_label(port)})
            try:
                devices = self._scan_rtu_interface(port)
                discovered_devices.extend(devices)
            except Exception as e:
                log.warning("Error scanning RTU port %s: %s", port, e)
                errors.append({"interface": port, "error": str(e)})

        # 2. Scan local network for Modbus TCP
        if self._tcp_subnet_scan:
            interfaces.append({"name": "eth0", "type": "ethernet", "label": "LAN"})
            try:
                tcp_devices = self._scan_tcp_network()
                discovered_devices.extend(tcp_devices)
            except Exception as e:
                log.warning("Error during TCP subnet scan: %s", e)
                errors.append({"interface": "tcp", "error": str(e)})

        report = {
            "scan_ts": scan_ts,
            "scan_type": scan_type,
            "interfaces": interfaces,
            "discovered_devices": discovered_devices,
            "errors": errors,
        }

        self._last_report = report
        log.info(
            "Discovery scan complete: %d devices found across %d interfaces",
            len(discovered_devices), len(interfaces),
        )

        # Publish report as attribute to Cloud
        self._publish_report(report)

        return report

    def get_last_report(self) -> Optional[dict]:
        return self._last_report

    # ─── Serial/RTU scanning ──────────────────────────────────────────

    def _enumerate_serial_ports(self) -> list:
        """Find serial ports on the system."""
        patterns = ["/dev/ttyUSB*", "/dev/ttyAMA*", "/dev/ttyACM*", "/dev/ttyS*"]
        ports = []
        for pattern in patterns:
            ports.extend(glob.glob(pattern))
        # Filter out common non-RS485 ports
        ports = [p for p in ports if "ttyS0" not in p]  # ttyS0 is usually console
        log.debug("Enumerated serial ports: %s", ports)
        return sorted(ports)

    def _scan_rtu_interface(self, port: str) -> list:
        """Scan a serial port for Modbus RTU slaves."""
        try:
            from pymodbus.client import ModbusSerialClient
        except ImportError:
            log.warning("pymodbus not available, skipping RTU scan for %s", port)
            return []

        devices = []
        slave_min, slave_max = self._rtu_slave_range

        for baud_rate in self._rtu_baud_rates:
            if self._stopped:
                break
            try:
                client = ModbusSerialClient(
                    port=port, baudrate=baud_rate,
                    timeout=0.3, stopbits=1, bytesize=8, parity="N",
                )
                if not client.connect():
                    continue

                for slave_id in range(slave_min, slave_max + 1):
                    if self._stopped:
                        break
                    try:
                        result = client.read_holding_registers(0, 1, slave=slave_id)
                        if result and not result.isError():
                            ident = self._identify_device_rtu(client, slave_id)
                            device = {
                                "interface": port,
                                "connection": "modbus_rtu",
                                "slave_id": slave_id,
                                "baud_rate": baud_rate,
                                "signature": ident.get("signature", "Unknown Modbus Device"),
                                "identification": ident if ident.get("vendor") else None,
                                "registers_found": self._count_registers(client, slave_id),
                            }
                            devices.append(device)
                            log.info("Found RTU device: %s at %s slave %d @ %d baud",
                                     device["signature"], port, slave_id, baud_rate)
                    except Exception:
                        pass

                client.close()
            except Exception as e:
                log.debug("RTU scan error on %s @ %d: %s", port, baud_rate, e)

        return devices

    # ─── TCP scanning ─────────────────────────────────────────────────

    def _scan_tcp_network(self) -> list:
        """Scan local subnet for Modbus TCP devices on port 502."""
        try:
            from pymodbus.client import ModbusTcpClient
        except ImportError:
            log.warning("pymodbus not available, skipping TCP scan")
            return []

        devices = []
        timeout_s = self._tcp_scan_timeout_ms / 1000.0

        # Get local IP and compute /24 subnet
        local_ip = self._get_local_ip()
        if not local_ip:
            log.warning("Could not determine local IP for TCP scan")
            return devices

        subnet_base = ".".join(local_ip.split(".")[:3])
        log.info("Scanning TCP subnet %s.0/24 for Modbus devices...", subnet_base)

        for host_part in range(1, 255):
            if self._stopped:
                break
            ip = f"{subnet_base}.{host_part}"
            if ip == local_ip:
                continue
            try:
                sock = socket.create_connection((ip, 502), timeout=timeout_s)
                sock.close()
                # Port 502 open — try Modbus read
                client = ModbusTcpClient(ip, port=502, timeout=1)
                if client.connect():
                    result = client.read_holding_registers(0, 1, slave=1)
                    if result and not result.isError():
                        ident = self._identify_device_tcp(client, 1)
                        device = {
                            "interface": f"{ip}:502",
                            "connection": "modbus_tcp",
                            "slave_id": 1,
                            "signature": ident.get("signature", "Unknown Modbus Device"),
                            "identification": ident if ident.get("vendor") else None,
                            "registers_found": self._count_registers(client, 1),
                        }
                        devices.append(device)
                        log.info("Found TCP device: %s at %s", device["signature"], ip)
                    client.close()
            except (socket.timeout, ConnectionRefusedError, OSError):
                pass

        return devices

    # ─── Device identification ────────────────────────────────────────

    def _identify_device_rtu(self, client, slave_id: int) -> dict:
        """Attempt to identify a Modbus RTU device."""
        return self._identify_device(client, slave_id)

    def _identify_device_tcp(self, client, slave_id: int) -> dict:
        """Attempt to identify a Modbus TCP device."""
        return self._identify_device(client, slave_id)

    def _identify_device(self, client, slave_id: int) -> dict:
        """
        Attempt to identify a Modbus device via multiple methods:
        1. Modbus Device Identification (FC 43, MEI type 14)
        2. Vendor-specific ID registers
        3. Register count probing
        """
        # Method 1: FC43 MEI device identification
        try:
            from pymodbus.mei_message import ReadDeviceInformationRequest
            request = ReadDeviceInformationRequest(read_code=0x01, slave=slave_id)
            response = client.execute(request)
            if response and not response.isError() and hasattr(response, "information"):
                info = response.information
                vendor = info.get(0x00, b"").decode(errors="ignore").strip()
                model = info.get(0x01, b"").decode(errors="ignore").strip()
                if vendor or model:
                    return {
                        "vendor": vendor or "Unknown",
                        "model": model or "Unknown",
                        "signature": f"{vendor} {model}".strip(),
                    }
        except Exception:
            pass

        # Method 2: Vendor-specific ID registers
        for vendor_name, reg_info in VENDOR_ID_REGISTERS.items():
            try:
                result = client.read_holding_registers(
                    reg_info["address"], reg_info["count"], slave=slave_id
                )
                if result and not result.isError():
                    raw_val = result.registers[0]
                    models = reg_info.get("models", {})
                    model = models.get(raw_val, None)
                    if model:
                        return {
                            "vendor": vendor_name.capitalize(),
                            "model": model,
                            "signature": f"{vendor_name.capitalize()} {model}",
                            "raw_id": hex(raw_val),
                        }
            except Exception:
                pass

        return {"vendor": None, "model": None, "signature": "Unknown Modbus Device"}

    def _count_registers(self, client, slave_id: int) -> int:
        """Probe how many holding registers are readable (rough estimate)."""
        counts = [10, 50, 100, 200, 500]
        last_good = 0
        for count in counts:
            try:
                result = client.read_holding_registers(0, count, slave=slave_id)
                if result and not result.isError():
                    last_good = count
                else:
                    break
            except Exception:
                break
        return last_good

    # ─── Helpers ──────────────────────────────────────────────────────

    def _get_local_ip(self) -> Optional[str]:
        """Get the local IP address of the default interface."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return None

    @staticmethod
    def _serial_label(port: str) -> str:
        """Generate a human-readable label for a serial port."""
        if "ttyUSB" in port:
            idx = port.replace("/dev/ttyUSB", "")
            return f"RS485-{int(idx) + 1}" if idx.isdigit() else f"USB-Serial"
        elif "ttyAMA" in port:
            return "Built-in UART"
        elif "ttyACM" in port:
            return "USB-ACM"
        return port.split("/")[-1]

    def _publish_report(self, report: dict):
        """Publish the discovery report as a gateway attribute."""
        payload = {
            "serial_number": self._serial_number,
            "ts": int(time() * 1000),
            "attributes": {
                "status": "online",
                "discovery_report": report,
            },
        }
        self._publisher.publish_attributes(payload)
        log.info("Published discovery report to Cloud (%d devices)", len(report.get("discovered_devices", [])))
