"""`import requests` 的测试替身入口: 真正的实现放在 fake_requests.py。

测试时设置 PYTHONPATH=<本目录>, 被测脚本就会 import 到这里, 全程离线且行为可控。
"""
from fake_requests import (  # noqa: F401
    LAST_GOOD_DATA,
    LAST_GOOD_NODES,
    FakeError,
    Session,
    get,
)

__version__ = "fake-1.0"

exceptions = type("exceptions", (), {"FakeError": FakeError, "RequestException": FakeError})
