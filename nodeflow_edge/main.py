"""
Nodeflow Edge — Entry Point

Usage:
    python -m nodeflow_edge.main [--config /path/to/config.json]
"""

import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sys

from nodeflow_edge.gateway.nodeflow_gateway import NodeflowGateway


def setup_logging(level="INFO", log_config=None):
    log_format = '%(asctime)s - |%(levelname)s| [%(name)s] - %(message)s'
    log_level = getattr(logging, level.upper(), logging.INFO)

    handlers = [logging.StreamHandler(sys.stdout)]

    # Add rotating file handler if configured
    if log_config and log_config.get("file"):
        log_file = log_config["file"]
        max_bytes = log_config.get("max_bytes", 5 * 1024 * 1024)  # 5 MB default
        backup_count = log_config.get("backup_count", 5)

        # Ensure the log directory exists
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)

        file_handler = RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backup_count
        )
        file_handler.setFormatter(logging.Formatter(log_format))
        handlers.append(file_handler)

    logging.basicConfig(
        level=log_level,
        format=log_format,
        handlers=handlers
    )


def main():
    parser = argparse.ArgumentParser(description="Nodeflow Edge IoT Gateway")
    parser.add_argument(
        "--config", "-c",
        default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.json"),
        help="Path to config.json (default: ./config.json)"
    )
    parser.add_argument(
        "--log-level", "-l",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level (default: INFO)"
    )
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        sys.exit(1)

    # Load logging config from config.json (optional "logging" block)
    log_config = None
    try:
        with open(args.config, 'r') as f:
            config = json.load(f)
            log_config = config.get("logging")
    except Exception:
        pass

    setup_logging(args.log_level, log_config=log_config)

    gateway = NodeflowGateway(args.config)
    gateway.run()


if __name__ == "__main__":
    main()
