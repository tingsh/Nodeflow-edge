Project Goal: You are building Novena Gateway, a Python-based IoT gateway software designed to run on industrial hardware (Raspberry Pi CM4, Teltonika, etc.). Your goal is to securely read data from local industrial equipment (Modbus TCP/RTU) and publish it to a cloud MQTT broker.

Architecture: This project is a strategic fork of the open-source thingsboard-gateway. We are utilizing their robust protocol connectors (Modbus, OPC-UA), but we are stripping out their server dependencies. We do NOT connect to a Java ThingsBoard server. We connect to a custom Django backend (Novena Hub).

Integration Contract with Novena Hub: You must format all outbound MQTT payloads to match the exact schema expected by the Novena Hub MQTT Consumer.

MQTT Broker Details:

Mosquitto Broker running at the cloud IP address.
Port: 1883
Topic: v1/gateway/telemetry
Auth: Username/Password OR Access Token (to be configured).
Required Outbound JSON Payload Schema:

json
{
  "serial_number": "NF-EDGE-001",  // The unique identifier of this gateway hardware
  "values": {
    "device_name": "Power Meter 1", // The name of the specific equipment polled
    "active_power": 450.2,          // Floating point data
    "voltage": 230.1,
    "status": "active"
  }
}
Primary Tasks for this Workspace:
Initialize a clean Python environment.
Extract the necessary Modbus polling logic from the TB Gateway source.
Implement a standard paho-mqtt publisher that uses the schema above.
Build a local configuration JSON file format (config.json) where mapping Modbus registers to keys (e.g., Register 3060 -> active_power) is defined.
Make it runnable as a systemd background service.