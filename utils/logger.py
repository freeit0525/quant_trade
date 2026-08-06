"""日志工具模块 - 提供统一的日志配置和获取接口"""

import logging
import sys
from config.settings import LogConfig


def get_logger(name: str, config: LogConfig | None = None) -> logging.Logger:
    """
    获取配置好的logger实例

    Args:
        name: logger名称，通常使用模块名
        config: 日志配置，为None时使用默认配置

    Returns:
        配置好的Logger实例
    """
    if config is None:
        config = LogConfig()

    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, config.level.upper(), logging.INFO))

    # 避免重复添加handler
    if logger.handlers:
        return logger

    formatter = logging.Formatter(config.format)

    # 控制台handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    # 文件handler（如果配置了日志文件）
    if config.file:
        file_handler = logging.FileHandler(config.file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger
