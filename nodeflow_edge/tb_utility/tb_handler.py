#     Copyright 2026. ThingsBoard
#     Adapted for Nodeflow Edge — remote logging to TB server removed.
#
#     Licensed under the Apache License, Version 2.0 (the "License");
#     you may not use this file except in compliance with the License.
#     You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#     Unless required by applicable law or agreed to in writing, software
#     distributed under the License is distributed on an "AS IS" BASIS,
#     WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#     See the License for the specific language governing permissions and
#     limitations under the License.

import logging
from sys import stdout

from nodeflow_edge.tb_utility.tb_logger import TbLogger

logging.setLoggerClass(TbLogger)


class TBRemoteLoggerHandler(logging.Handler):
    """No-op handler kept for backward compatibility with connector code that may reference it."""

    def __init__(self, gateway=None):
        super().__init__()
        self.loggers = {}
        self.activated = False

    def add_logger(self, name, level):
        self.loggers[name] = (logging.getLogger(name), level)

    def update_logger(self, name, level):
        if name in self.loggers:
            self.loggers[name] = (self.loggers[name][0], level)

    def remove_logger(self, name):
        self.loggers.pop(name, None)

    def handle(self, record):
        pass

    def activate(self):
        self.activated = True

    def deactivate(self):
        self.activated = False

    @staticmethod
    def set_default_handler():
        logging.setLoggerClass(TbLogger)
        for logger_name in ('service', 'extension', 'tb_connection', 'storage'):
            logger = logging.getLogger(logger_name)
            handler = logging.StreamHandler(stdout)
            handler.setFormatter(logging.Formatter(
                '[STREAM ONLY] %(asctime)s - %(levelname)s - [%(filename)s] - %(module)s - %(lineno)d - %(message)s'))
            logger.addHandler(handler)

    @staticmethod
    def get_logger_level_id(log_level):
        if isinstance(log_level, str):
            return 100 if log_level == 'NONE' else logging._nameToLevel.get(log_level, 100)
        return log_level
