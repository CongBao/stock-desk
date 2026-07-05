from collections import UserList
from collections.abc import Iterator, Mapping, Sequence, Set
import logging
from typing import Any, overload

import pytest

from stock_desk.security.redaction import (
    REDACTED_MARKER,
    RedactingFilter,
    SecretRedactor,
)


SECRET = "secret-value"


class TokenSequence(Sequence[str]):
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...

    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)


class NonReconstructableSequence(Sequence[str]):
    def __init__(self, values: list[str], *, required: bool) -> None:
        self._values = list(values)
        self.required = required

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...

    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)


class TokenSet(Set[str]):
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def __contains__(self, value: object) -> bool:
        return value in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


class CyclicSet(Set[Any]):
    def __contains__(self, value: object) -> bool:
        return value is self or value == SECRET

    def __iter__(self) -> Iterator[Any]:
        return iter((SECRET, self))

    def __len__(self) -> int:
        return 2


class SecretStringObject:
    def __str__(self) -> str:
        return f"str:{SECRET}"

    def __repr__(self) -> str:
        return f"repr:{SECRET}"


class UnrenderableObject:
    def __str__(self) -> str:
        raise RuntimeError(f"cannot render {SECRET}")


class FullyUnrenderableObject:
    def __str__(self) -> str:
        raise RuntimeError(f"cannot stringify {SECRET}")

    def __repr__(self) -> str:
        raise RuntimeError(f"cannot represent {SECRET}")


class SecretStringError(RuntimeError):
    def __str__(self) -> str:
        return f"hostile exception {SECRET}"


class HashableSecretMapping(Mapping[str, str]):
    def __init__(self) -> None:
        self._values = {"credential": SECRET}

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    __hash__ = object.__hash__


class HostileSequence(Sequence[str]):
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    @overload
    def __getitem__(self, index: int) -> str: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[str]: ...

    def __getitem__(self, index: int | slice) -> str | Sequence[str]:
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __str__(self) -> str:
        return f"hostile sequence {SECRET}"

    def __repr__(self) -> str:
        return f"hostile sequence {SECRET}"


class HostileSet(Set[str]):
    def __init__(self, values: list[str]) -> None:
        self._values = list(values)

    def __contains__(self, value: object) -> bool:
        return value in self._values

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __str__(self) -> str:
        return f"hostile set {SECRET}"

    def __repr__(self) -> str:
        return f"hostile set {SECRET}"


class HostileMapping(Mapping[str, str]):
    def __init__(self) -> None:
        self._values = {"credential": SECRET}

    def __getitem__(self, key: str) -> str:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __str__(self) -> str:
        return f"hostile mapping {SECRET}"

    def __repr__(self) -> str:
        return f"hostile mapping {SECRET}"


class HostileList(list[str]):
    def __str__(self) -> str:
        return f"hostile list {SECRET}"

    def __repr__(self) -> str:
        return f"hostile list {SECRET}"


class HostileFrozenSet(frozenset[str]):
    def __str__(self) -> str:
        return f"hostile frozen set {SECRET}"

    def __repr__(self) -> str:
        return f"hostile frozen set {SECRET}"


class HostileStr(str):
    def __len__(self) -> int:
        raise RuntimeError(f"hostile length {SECRET}")

    def __str__(self) -> str:
        return f"hostile str {SECRET}"

    def __repr__(self) -> str:
        return f"hostile str {SECRET}"

    def encode(self, *_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError(f"hostile encode {SECRET}")


class HostileBytes(bytes):
    def __str__(self) -> str:
        return f"hostile bytes {SECRET}"

    def __repr__(self) -> str:
        return f"hostile bytes {SECRET}"

    def decode(self, *_args: object, **_kwargs: object) -> str:
        raw = bytes.__bytes__(self).decode("utf-8")
        return HostileStr(raw)


class HostileInt(int):
    def __str__(self) -> str:
        return f"hostile int {SECRET}"

    def __repr__(self) -> str:
        return f"hostile int {SECRET}"

    def __int__(self) -> int:
        raise RuntimeError(f"hostile int conversion {SECRET}")


class HostileFloat(float):
    def __str__(self) -> str:
        return f"hostile float {SECRET}"

    def __repr__(self) -> str:
        return f"hostile float {SECRET}"

    def __float__(self) -> float:
        raise RuntimeError(f"hostile float conversion {SECRET}")


class HostileComplex(complex):
    def __str__(self) -> str:
        return f"hostile complex {SECRET}"

    def __repr__(self) -> str:
        return f"hostile complex {SECRET}"

    def __complex__(self) -> complex:
        raise RuntimeError(f"hostile complex conversion {SECRET}")


def _render_log(
    redactor: SecretRedactor,
    message: object,
    *args: object,
    exc_info: bool = False,
    extra: dict[str, Any] | None = None,
    level: int = logging.ERROR,
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
    if level == logging.INFO:
        logger.info(message, *args, exc_info=exc_info, extra=extra)
    else:
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


def test_clean_normalizes_userlist_and_custom_sequences_to_builtin_lists() -> None:
    redactor = SecretRedactor([SECRET])
    user_list = UserList([SECRET, "safe"])
    custom = TokenSequence([SECRET, "safe"])

    cleaned_user_list = redactor.clean(user_list)
    cleaned_custom = redactor.clean(custom)

    assert type(cleaned_user_list) is list
    assert type(cleaned_custom) is list
    assert list(cleaned_user_list) == [REDACTED_MARKER, "safe"]
    assert list(cleaned_custom) == [REDACTED_MARKER, "safe"]
    assert list(user_list) == [SECRET, "safe"]
    assert list(custom) == [SECRET, "safe"]


def test_clean_falls_back_to_list_for_sequences_that_cannot_be_reconstructed() -> None:
    redactor = SecretRedactor([SECRET])
    original = NonReconstructableSequence([SECRET, "safe"], required=True)

    cleaned = redactor.clean(original)

    assert cleaned == [REDACTED_MARKER, "safe"]
    assert list(original) == [SECRET, "safe"]


def test_clean_handles_a_cyclic_userlist_without_leaking() -> None:
    redactor = SecretRedactor([SECRET])
    cyclic: UserList[Any] = UserList([SECRET])
    cyclic.append(cyclic)

    cleaned = redactor.clean(cyclic)

    assert type(cleaned) is list
    assert SECRET not in repr(cleaned)
    assert "[REDACTION_CYCLE]" in repr(cleaned)


def test_clean_supports_frozenset_and_custom_abstract_set() -> None:
    redactor = SecretRedactor([SECRET])
    frozen = frozenset((SECRET, "safe"))
    custom = TokenSet([SECRET, "safe"])

    cleaned_frozen = redactor.clean(frozen)
    cleaned_custom = redactor.clean(custom)

    assert isinstance(cleaned_frozen, frozenset)
    assert type(cleaned_custom) is set
    assert cleaned_frozen == frozenset((REDACTED_MARKER, "safe"))
    assert set(cleaned_custom) == {REDACTED_MARKER, "safe"}
    assert SECRET in frozen
    assert SECRET in custom


def test_clean_handles_a_cyclic_abstract_set_without_leaking() -> None:
    redactor = SecretRedactor([SECRET])

    cleaned = redactor.clean(CyclicSet())

    assert SECRET not in repr(cleaned)
    assert "[REDACTION_CYCLE]" in repr(cleaned)


def test_clean_falls_back_when_set_items_become_unhashable() -> None:
    redactor = SecretRedactor([SECRET])
    mapping = HashableSecretMapping()

    cleaned_set = redactor.clean({mapping})
    cleaned_frozen = redactor.clean(frozenset((mapping,)))

    assert isinstance(cleaned_set, list)
    assert isinstance(cleaned_frozen, list)
    assert SECRET not in repr(cleaned_set)
    assert SECRET not in repr(cleaned_frozen)
    assert mapping["credential"] == SECRET


def test_clean_fails_closed_when_mapping_keys_become_unhashable() -> None:
    redactor = SecretRedactor([SECRET])
    mapping_key = HashableSecretMapping()
    original = {mapping_key: "safe value"}

    cleaned = redactor.clean(original)

    assert isinstance(cleaned, dict)
    assert SECRET not in repr(cleaned)
    assert list(cleaned.values()) == ["safe value"]
    assert mapping_key in original


def test_clean_sanitizes_exception_arguments() -> None:
    redactor = SecretRedactor([SECRET])
    error = ValueError("request failed", {"token": SECRET})

    cleaned = redactor.clean(error)

    assert type(cleaned) is RuntimeError
    assert SECRET not in str(cleaned)
    assert "ValueError" in str(cleaned)
    assert SECRET in str(error)


def test_clean_discards_hostile_user_defined_container_and_exception_types() -> None:
    redactor = SecretRedactor([SECRET])

    cleaned_sequence = redactor.clean(HostileSequence([SECRET]))
    cleaned_set = redactor.clean(HostileSet([SECRET]))
    cleaned_mapping = redactor.clean(HostileMapping())
    cleaned_error = redactor.clean(SecretStringError())

    assert type(cleaned_sequence) is list
    assert type(cleaned_set) is set
    assert type(cleaned_mapping) is dict
    assert type(cleaned_error) is RuntimeError
    for cleaned in (
        cleaned_sequence,
        cleaned_set,
        cleaned_mapping,
        cleaned_error,
    ):
        assert SECRET not in str(cleaned)
        assert SECRET not in repr(cleaned)


def test_clean_discards_hostile_subclasses_of_builtin_containers() -> None:
    redactor = SecretRedactor([SECRET])

    cleaned_list = redactor.clean(HostileList([SECRET]))
    cleaned_frozen = redactor.clean(HostileFrozenSet((SECRET,)))

    assert type(cleaned_list) is list
    assert type(cleaned_frozen) is frozenset
    assert SECRET not in repr(cleaned_list)
    assert SECRET not in repr(cleaned_frozen)


@pytest.mark.parametrize(
    ("value", "expected_type", "expected_value"),
    [
        (HostileStr(""), str, ""),
        (HostileStr("ordinary"), str, "ordinary"),
        (HostileStr(SECRET), str, REDACTED_MARKER),
        (HostileBytes(b""), bytes, b""),
        (HostileBytes(b"ordinary"), bytes, b"ordinary"),
        (HostileBytes(SECRET.encode()), bytes, REDACTED_MARKER.encode()),
        (HostileInt(7), int, 7),
        (HostileFloat(1.5), float, 1.5),
        (HostileComplex(1, 2), complex, complex(1, 2)),
    ],
    ids=[
        "empty-str",
        "nonempty-str",
        "secret-str",
        "empty-bytes",
        "nonempty-bytes",
        "secret-bytes",
        "int",
        "float",
        "complex",
    ],
)
def test_clean_normalizes_hostile_scalar_subclasses_to_exact_builtins(
    value: object,
    expected_type: type[object],
    expected_value: object,
) -> None:
    redactor = SecretRedactor([SECRET])

    cleaned = redactor.clean(value)

    assert type(cleaned) is expected_type
    assert cleaned == expected_value
    assert SECRET not in repr(cleaned)


def test_register_normalizes_a_hostile_string_subclass() -> None:
    redactor = SecretRedactor([])

    redactor.register(HostileStr(SECRET))

    assert redactor.clean(SECRET) == REDACTED_MARKER


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


@pytest.mark.parametrize(
    ("message", "args"),
    [
        (SecretStringObject(), ()),
        ("object=%s", (SecretStringObject(),)),
        ("object=%r", (SecretStringObject(),)),
    ],
)
def test_logging_filter_sanitizes_objects_stringified_by_formatter(
    message: object,
    args: tuple[object, ...],
) -> None:
    redactor = SecretRedactor([SECRET])

    output, record = _render_log(redactor, message, *args, level=logging.INFO)

    assert SECRET not in output
    assert SECRET not in record.getMessage()
    assert REDACTED_MARKER in output
    assert record.args == ()
    assert record.levelname == "INFO"


def test_logging_filter_survives_an_object_that_cannot_be_rendered() -> None:
    redactor = SecretRedactor([SECRET])

    output, record = _render_log(redactor, UnrenderableObject())

    assert SECRET not in output
    assert SECRET not in record.getMessage()
    assert "UNRENDERABLE" in output


def test_logging_filter_sanitizes_unsupported_extra_objects() -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.extra-object",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "safe",
            "args": (),
            "credential": SecretStringObject(),
        }
    )

    RedactingFilter(redactor).filter(record)
    output = logging.Formatter("%(message)s|%(credential)s").format(record)

    assert SECRET not in output
    assert REDACTED_MARKER in output


def test_logging_filter_fails_closed_for_unrenderable_extra_objects() -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.unrenderable-extra",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "safe",
            "args": (),
            "credential": FullyUnrenderableObject(),
        }
    )

    RedactingFilter(redactor).filter(record)
    output = logging.Formatter("%(message)s|%(credential)s").format(record)

    assert SECRET not in output
    assert "UNRENDERABLE" in output


def test_logging_filter_handles_unhashable_cleaned_extra_containers() -> None:
    redactor = SecretRedactor([SECRET])
    mapping = HashableSecretMapping()
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.extra-container",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "safe",
            "args": (),
            "payload_set": {mapping},
            "payload_map": {mapping: "safe value"},
        }
    )

    RedactingFilter(redactor).filter(record)
    output = logging.Formatter("%(message)s|%(payload_set)s|%(payload_map)s").format(
        record
    )

    assert SECRET not in output
    assert REDACTED_MARKER in output


def test_logging_filter_discards_hostile_types_from_extra_fields() -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.hostile-types",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "safe",
            "args": (),
            "hostile_sequence": HostileSequence([SECRET]),
            "hostile_set": HostileSet([SECRET]),
            "hostile_mapping": HostileMapping(),
            "hostile_error": SecretStringError(),
        }
    )

    RedactingFilter(redactor).filter(record)
    output = logging.Formatter(
        "%(message)s|%(hostile_sequence)s|%(hostile_set)s|"
        "%(hostile_mapping)s|%(hostile_error)s"
    ).format(record)

    assert SECRET not in output
    assert type(getattr(record, "hostile_sequence")) is list
    assert type(getattr(record, "hostile_set")) is set
    assert type(getattr(record, "hostile_mapping")) is dict
    assert type(getattr(record, "hostile_error")) is RuntimeError


def test_logging_filter_normalizes_scalar_subclasses_for_custom_formatters() -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.hostile-scalars",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "msg": "safe",
            "args": (),
            "empty_text": HostileStr(""),
            "text": HostileStr(SECRET),
            "empty_blob": HostileBytes(b""),
            "blob": HostileBytes(SECRET.encode()),
            "integer": HostileInt(7),
            "decimal": HostileFloat(1.5),
            "number": HostileComplex(1, 2),
        }
    )

    RedactingFilter(redactor).filter(record)
    output = logging.Formatter(
        "%(message)s|%(empty_text)s|%(text)s|%(empty_blob)s|%(blob)s|"
        "%(integer)d|%(decimal).2f|%(number)s"
    ).format(record)

    assert SECRET not in output
    assert REDACTED_MARKER in output
    assert type(getattr(record, "empty_text")) is str
    assert type(getattr(record, "text")) is str
    assert type(getattr(record, "empty_blob")) is bytes
    assert type(getattr(record, "blob")) is bytes
    assert type(getattr(record, "integer")) is int
    assert type(getattr(record, "decimal")) is float
    assert type(getattr(record, "number")) is complex
    assert "|7|1.50|(1+2j)" in output


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


def test_logging_filter_sanitizes_an_exception_with_custom_stringification() -> None:
    redactor = SecretRedactor([SECRET])
    error = SecretStringError()
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.hostile-exception",
            "levelno": logging.ERROR,
            "levelname": "ERROR",
            "msg": "failed",
            "args": (),
            "exc_info": (SecretStringError, error, None),
        }
    )

    RedactingFilter(redactor).filter(record)
    output = logging.Formatter("%(message)s").format(record)

    assert SECRET not in output
    assert REDACTED_MARKER in output


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


def test_logging_filter_clears_every_cached_and_dynamic_message_field() -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.all-fields",
            "levelno": logging.ERROR,
            "levelname": "ERROR",
            "msg": "failure token=%s",
            "args": (SECRET,),
            "message": f"cached message {SECRET}",
            "asctime": f"cached time {SECRET}",
            "exc_info": (
                RuntimeError,
                RuntimeError(f"exception {SECRET}"),
                None,
            ),
            "exc_text": f"cached exception {SECRET}",
            "stack_info": f"stack {SECRET}",
        }
    )

    RedactingFilter(redactor).filter(record)

    assert SECRET not in record.getMessage()
    assert SECRET not in repr(record.args)
    assert SECRET not in record.message
    assert SECRET not in record.asctime
    assert record.exc_text is None
    assert record.exc_info is not None
    assert SECRET not in str(record.exc_info[1])
    assert record.stack_info is not None
    assert SECRET not in record.stack_info


def test_logging_filter_sanitizes_string_metadata_used_by_custom_formatters() -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {
            "name": f"logger.{SECRET}",
            "levelno": logging.INFO,
            "levelname": "INFO",
            "pathname": f"/tmp/{SECRET}/worker.py",
            "filename": f"{SECRET}.py",
            "module": f"module-{SECRET}",
            "funcName": f"call-{SECRET}",
            "threadName": f"thread-{SECRET}",
            "processName": f"process-{SECRET}",
            "msg": "safe message",
            "args": (),
            "thread": 7,
            "process": 11,
        }
    )
    formatter = logging.Formatter(
        "%(name)s|%(pathname)s|%(filename)s|%(module)s|%(funcName)s|"
        "%(threadName)s|%(processName)s|%(message)s|%(thread)d|%(process)d"
    )

    RedactingFilter(redactor).filter(record)
    output = formatter.format(record)

    assert SECRET not in output
    assert record.thread == 7
    assert record.process == 11


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
