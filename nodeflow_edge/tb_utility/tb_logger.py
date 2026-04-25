#     Copyright 2026. ThingsBoard
#     Adapted for Nodeflow Edge — ThingsBoard server dependencies removed.
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
from time import monotonic
from threading import RLock
from os import path

from nodeflow_edge.tb_utility.tb_rotating_file_handler import TimedRotatingFileHandler
from nodeflow_edge.gateway.statistics.statistics_service import StatisticsService

TRACE_LOGGING_LEVEL = 5
logging.addLevelName(TRACE_LOGGING_LEVEL, "TRACE")


def init_logger(gateway, name, level, enable_remote_logging=False, is_connector_logger=False,
                is_converter_logger=False, attr_name=None):
    """
    For creating a Logger with all config automatically.
    Nodeflow Edge version: local logging only (no remote TB server logging).
    """

    log = logging.getLogger(name)
    log.is_connector_logger = is_connector_logger
    log.is_converter_logger = is_converter_logger

    if attr_name:
        log.attr_name = attr_name + '_ERRORS_COUNT'

    if TbLogger.is_main_module_logger(name, attr_name, is_converter_logger):
        file_handler = None

        if is_connector_logger:
            file_handler = TimedRotatingFileHandler.get_connector_file_handler(log.name + '_connector')

        if is_converter_logger:
            file_handler = TimedRotatingFileHandler.get_converter_file_handler(log.name)

        if file_handler:
            log.addHandler(file_handler)
    else:
        main_file_handler = TimedRotatingFileHandler.get_time_rotating_file_handler_by_logger_name(attr_name)
        if main_file_handler and main_file_handler not in log.handlers:
            log.addHandler(main_file_handler)

    # Add a console handler if none exists
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        console_handler = logging.StreamHandler(stdout)
        console_handler.setFormatter(logging.Formatter(
            '%(asctime)s - |%(levelname)s| [%(name)s] - %(module)s - %(lineno)d - %(message)s'))
        log.addHandler(console_handler)

    log_level_conf = level
    if log_level_conf:
        log_level = logging.getLevelName(log_level_conf)

        try:
            log.setLevel(log_level)
        except ValueError:
            log.setLevel(100)

    return log


class TbLogger(logging.Logger):
    ALL_ERRORS_COUNT = 0
    IS_ALL_ERRORS_COUNT_RESET = False
    RESET_ERRORS_PERIOD = 60
    SEND_ERRORS_PERIOD = 5

    __PREVIOUS_ALL_ERRORS_COUNT = -1
    __PREVIOUS_BATCH_TO_SEND = {}

    ERRORS_MUTEX = RLock()
    ERRORS_BATCH = {}

    PREVIOUS_ERRORS_SENT_TIME = 0
    PREVIOUS_ERRORS_RESET_TIME = 0

    def __init__(self, name, level=logging.NOTSET, is_connector_logger=False, is_converter_logger=False,
                 attr_name=None):
        super(TbLogger, self).__init__(name=name, level=level)
        self.propagate = True
        self.parent = self.root
        self.__is_connector_logger = is_connector_logger
        self.__is_converter_logger = is_converter_logger
        self.__previous_reset_errors_time = TbLogger.PREVIOUS_ERRORS_RESET_TIME
        logging.Logger.trace = TbLogger.trace

        self.errors = 0

        if attr_name:
            self.attr_name = attr_name + '_ERRORS_COUNT'
        else:
            self.attr_name = self.name + '_ERRORS_COUNT'

        self._is_on_init_state = True

    @property
    def is_connector_logger(self):
        return self.__is_connector_logger

    @is_connector_logger.setter
    def is_connector_logger(self, value):
        self.__is_connector_logger = value

    @property
    def is_converter_logger(self):
        return self.__is_converter_logger

    @is_converter_logger.setter
    def is_converter_logger(self, value):
        self.__is_converter_logger = value

    def reset(self):
        with TbLogger.ERRORS_MUTEX:
            TbLogger.ALL_ERRORS_COUNT = max(0, TbLogger.ALL_ERRORS_COUNT - self.errors)
        self.errors = 0
        self._update_errors_batch()

    def stop(self):
        with TbLogger.ERRORS_MUTEX:
            TbLogger.ERRORS_BATCH.pop(self.attr_name, None)
        self.reset()
        self.handlers.clear()

    def _send_errors(self):
        with TbLogger.ERRORS_MUTEX:
            TbLogger.ERRORS_BATCH[self.attr_name] = 0
        self._is_on_init_state = False

    def trace(self, msg, *args, **kwargs):
        if self.isEnabledFor(TRACE_LOGGING_LEVEL):
            self._log(TRACE_LOGGING_LEVEL, msg, args, **kwargs)

    def error(self, msg, *args, **kwargs):
        attr_name = kwargs.pop('attr_name', None)
        kwargs['stacklevel'] = 2
        super(TbLogger, self).error(msg, *args, **kwargs)

        if self.__is_connector_logger:
            StatisticsService.count_connector_message(self.name, 'connectorsErrors')
        if self.__is_converter_logger:
            StatisticsService.count_connector_message(self.name, 'convertersErrors')

        self._add_error()
        self._update_errors_batch(error_attr_name=attr_name)

    def exception(self, msg, *args, **kwargs) -> None:
        attr_name = kwargs.pop('attr_name', None)
        kwargs['stacklevel'] = 2
        super(TbLogger, self).exception(msg, *args, **kwargs)

        if self.__is_connector_logger:
            StatisticsService.count_connector_message(self.name, 'connectorsErrors')
        if self.__is_converter_logger:
            StatisticsService.count_connector_message(self.name, 'convertersErrors')

        self._add_error()
        self._update_errors_batch(error_attr_name=attr_name)

    def _add_error(self):
        with TbLogger.ERRORS_MUTEX:
            TbLogger.ALL_ERRORS_COUNT += 1
            if TbLogger.PREVIOUS_ERRORS_RESET_TIME != self.__previous_reset_errors_time:
                self.__previous_reset_errors_time = TbLogger.PREVIOUS_ERRORS_RESET_TIME
                self.errors = 0
        self.errors += 1

    def _update_errors_batch(self, error_attr_name=None):
        if error_attr_name:
            error_attr_name = error_attr_name + '_ERRORS_COUNT'
        else:
            error_attr_name = self.attr_name
        TbLogger.ERRORS_BATCH[error_attr_name] = max(0, self.errors)

    @classmethod
    def send_errors_if_needed(cls, gateway):
        # Nodeflow Edge: no remote error reporting to TB server
        current_monotonic = monotonic()
        if (current_monotonic - cls.PREVIOUS_ERRORS_RESET_TIME >= cls.RESET_ERRORS_PERIOD
                or cls.PREVIOUS_ERRORS_RESET_TIME == 0):
            cls.PREVIOUS_ERRORS_RESET_TIME = current_monotonic
            with cls.ERRORS_MUTEX:
                for key in cls.ERRORS_BATCH:
                    cls.ERRORS_BATCH[key] = 0
                cls.ALL_ERRORS_COUNT = 0

    @staticmethod
    def is_main_module_logger(name, attr_name, is_converter_logger):
        return name == attr_name or attr_name is None or (name != attr_name and is_converter_logger)

    @staticmethod
    def update_file_handlers():
        for logger in logging.Logger.manager.loggerDict.values():
            if hasattr(logger, 'is_connector_logger') or hasattr(logger, 'is_converter_logger'):
                file_handler_filter = list(filter(lambda handler: isinstance(handler, TimedRotatingFileHandler),
                                                  logger.handlers))
                if len(file_handler_filter):
                    old_file_handler = file_handler_filter[0]

                    new_file_handler = None
                    filename = old_file_handler.baseFilename.split(path.sep)[-1].split('.')[0]
                    if logger.is_connector_logger:
                        new_file_handler = TimedRotatingFileHandler.get_connector_file_handler(filename) # noqa

                    if logger.is_converter_logger:
                        new_file_handler = TimedRotatingFileHandler.get_converter_file_handler(filename) # noqa

                    if new_file_handler:
                        logger.addHandler(new_file_handler)
                        logger.removeHandler(old_file_handler)

    @staticmethod
    def check_and_update_file_handlers_class_name(config):
        for handler_config in config.get('handlers', {}).values():
            if handler_config.get('class', '') == 'nodeflow_edge.tb_utility.tb_handler.TimedRotatingFileHandler':
                handler_config['class'] = 'nodeflow_edge.tb_utility.tb_rotating_file_handler.TimedRotatingFileHandler' # noqa


logging.setLoggerClass(TbLogger)
