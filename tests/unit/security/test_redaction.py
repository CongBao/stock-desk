import logging
from typing import Any

import pytest

from stock_desk.security.redaction import (
    REDACTED_MARKER,
    RedactingFilter,
    SecretRedactor,
)


SECRET = "secret-value"


def _render_log(
    redactor: SecretRedactor,
    message: object,
    *args: object,
    exc_info: bool = False,
    extra: dict[str, Any] | None = None,
) -> tuple[str, logging.LogRecord]:
    logger = logging.getLogger(f"stock_desk.tests.redaction.{id(redactor)}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s %(name)s %(message)s"))
    handler.addFilter(RedactingFilter(redactor))
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = Capture()
    capture.addFilter(RedactingFilter(redactor))
    logger.addHandler(capture)

    from io import StringIO

    output = StringIO()
    handler.setStream(output)
    logger.addHandler(handler)
    logger.error(message, *args, exc_info=exc_info, extra=extra)
    return output.getvalue(), records[0]


def test_clean_replaces_strings_bytes_and_overlapping_secrets() -> None:
    redactor = SecretRedactor(["secret", SECRET, "", SECRET])

    assert SECRET not in redactor.clean(f"failed: {SECRET}")
    assert redactor.clean(f"failed: {SECRET}") == f"failed: {REDACTED_MARKER}"
    assert redactor.clean(SECRET.encode()) == REDACTED_MARKER.encode()
    assert SECRET not in repr(redactor)
    assert "2 secrets" in repr(redactor)


def test_register_adds_nonempty_unique_secrets_at_runtime() -> None:
    redactor = SecretRedactor([])

    redactor.register(SECRET)
    redactor.register(SECRET)
    redactor.register("")

    assert redactor.clean(SECRET) == REDACTED_MARKER
    assert "1 secrets" in repr(redactor)


def test_redaction_marker_is_stable_when_a_secret_overlaps_the_marker() -> None:
    redactor = SecretRedactor(["REDACTED"])

    first = redactor.clean("REDACTED")
    second = redactor.clean(first)

    assert first == REDACTED_MARKER
    assert second == REDACTED_MARKER


def test_clean_recurses_without_mutating_input_and_preserves_shapes() -> None:
    redactor = SecretRedactor([SECRET])
    original: dict[str, Any] = {
        f"key-{SECRET}": [SECRET, (f"x{SECRET}", {SECRET}), {"nested": SECRET}],
        "ordinary": 42,
    }

    cleaned = redactor.clean(original)

    assert SECRET not in repr(cleaned)
    assert cleaned["ordinary"] == 42
    assert isinstance(cleaned, dict)
    cleaned_key = next(key for key in cleaned if key != "ordinary")
    assert isinstance(cleaned[cleaned_key], list)
    assert isinstance(cleaned[cleaned_key][1], tuple)
    assert isinstance(cleaned[cleaned_key][1][1], set)
    assert original[f"key-{SECRET}"][0] == SECRET
    assert SECRET in repr(original)


def test_clean_sanitizes_exception_arguments() -> None:
    redactor = SecretRedactor([SECRET])
    error = ValueError("request failed", {"token": SECRET})

    cleaned = redactor.clean(error)

    assert isinstance(cleaned, ValueError)
    assert SECRET not in str(cleaned)
    assert SECRET in str(error)


def test_clean_handles_cycles_and_excessive_depth_without_leaking() -> None:
    redactor = SecretRedactor([SECRET], max_depth=8)
    cyclic: dict[str, Any] = {"token": SECRET}
    cyclic["self"] = cyclic
    nested: list[Any] = [SECRET]
    cursor = nested
    for _ in range(20):
        child: list[Any] = [SECRET]
        cursor.append(child)
        cursor = child

    cleaned_cycle = redactor.clean(cyclic)
    cleaned_depth = redactor.clean(nested)

    assert SECRET not in repr(cleaned_cycle)
    assert SECRET not in repr(cleaned_depth)
    assert "[REDACTION_CYCLE]" in repr(cleaned_cycle)
    assert "[REDACTION_DEPTH]" in repr(cleaned_depth)


def test_clean_leaves_ordinary_values_unchanged() -> None:
    redactor = SecretRedactor([SECRET])
    ordinary = {"ok": True, "count": 3, "value": None}

    assert redactor.clean(ordinary) == ordinary


def test_logging_filter_sanitizes_positional_and_mapping_arguments() -> None:
    redactor = SecretRedactor([SECRET])

    positional, positional_record = _render_log(redactor, "failed token=%s", SECRET)
    mapping, mapping_record = _render_log(
        redactor, "failed token=%(token)s", {"token": SECRET}
    )

    for output, record in (
        (positional, positional_record),
        (mapping, mapping_record),
    ):
        assert SECRET not in output
        assert SECRET not in record.getMessage()
        assert REDACTED_MARKER in output
        assert record.levelname == "ERROR"
        assert record.name.startswith("stock_desk.tests.redaction")


def test_logging_filter_sanitizes_exception_traceback_and_cached_text() -> None:
    redactor = SecretRedactor([SECRET])
    try:
        raise RuntimeError(f"provider rejected {SECRET}")
    except RuntimeError:
        output, record = _render_log(
            redactor,
            "request failed",
            exc_info=True,
        )

    assert SECRET not in output
    assert SECRET not in record.getMessage()
    assert record.exc_text is None or SECRET not in record.exc_text
    if record.exc_info is not None:
        assert SECRET not in str(record.exc_info[1])
    assert "RuntimeError" in output


def test_logging_filter_sanitizes_cached_exception_and_stack_text() -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.cached",
            "levelno": logging.ERROR,
            "levelname": "ERROR",
            "msg": "failure",
            "args": (),
            "exc_text": f"cached {SECRET}",
            "stack_info": f"stack {SECRET}",
        }
    )

    RedactingFilter(redactor).filter(record)

    assert record.exc_text is None
    assert record.stack_info is not None
    assert SECRET not in record.stack_info


def test_logging_filter_sanitizes_extra_nested_task_error() -> None:
    redactor = SecretRedactor([SECRET])

    output, record = _render_log(
        redactor,
        "task failed",
        extra={"task_error": {"provider": {"message": SECRET}}},
    )

    assert SECRET not in output
    assert SECRET not in repr(getattr(record, "task_error"))
    assert REDACTED_MARKER in repr(getattr(record, "task_error"))


def test_logging_filter_has_no_false_failure_without_secrets() -> None:
    output, record = _render_log(SecretRedactor([]), "ordinary value=%s", "safe")

    assert "ordinary value=safe" in output
    assert record.getMessage() == "ordinary value=safe"


@pytest.mark.parametrize("invalid", [None, b"bytes", 123])
def test_register_rejects_non_string_values(invalid: object) -> None:
    redactor = SecretRedactor([])

    with pytest.raises(TypeError):
        redactor.register(invalid)  # type: ignore[arg-type]
