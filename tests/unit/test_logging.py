import io
import json
import logging

from lb.logging import JsonFormatter, new


def test_formatter_produces_valid_json():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="hello", args=None, exc_info=None,
    )
    output = formatter.format(record)
    parsed = json.loads(output)
    assert parsed["msg"] == "hello"
    assert parsed["level"] == "INFO"
    assert "time" in parsed


def test_formatter_includes_extra_fields():
    formatter = JsonFormatter()
    record = logging.LogRecord(
        name="test", level=logging.INFO, pathname="", lineno=0,
        msg="routing", args=None, exc_info=None,
    )
    record.backend = "http://localhost:9001"
    record.algorithm = "round_robin"
    parsed = json.loads(formatter.format(record))
    assert parsed["backend"] == "http://localhost:9001"
    assert parsed["algorithm"] == "round_robin"


def test_new_writes_json_to_stream():
    logger = new(level="debug", name="test-logger")
    stream = io.StringIO()
    logger.handlers[0].stream = stream

    logger.info("test message", extra={"foo": "bar"})

    output = stream.getvalue().strip()
    parsed = json.loads(output)
    assert parsed["msg"] == "test message"
    assert parsed["foo"] == "bar"


def test_new_defaults_unknown_level_to_info():
    logger = new(level="bogus-level", name="test-logger-2")
    assert logger.level == logging.INFO


def test_new_does_not_duplicate_handlers_on_repeat_calls():
    logger1 = new(level="info", name="test-logger-3")
    logger2 = new(level="info", name="test-logger-3")
    assert logger1 is logger2
    assert len(logger2.handlers) == 1
