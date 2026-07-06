from collections import UserList
from collections.abc import Iterator, Mapping, Sequence, Set
from concurrent.futures import ThreadPoolExecutor
import gc
from io import StringIO
import logging
from logging.handlers import QueueHandler, QueueListener, SocketHandler
from multiprocessing.reduction import ForkingPickler
import pickle
from queue import Queue
from threading import Barrier, Event
from typing import Any, overload
import weakref

import pytest

import stock_desk.security.redaction as redaction_module
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


class HostileExceptionMeta(type):
    def __getattribute__(cls, name: str) -> Any:
        if name == "__name__":
            raise RuntimeError(f"hostile metaclass {SECRET}")
        return super().__getattribute__(name)


class HostileMetaclassError(RuntimeError, metaclass=HostileExceptionMeta):
    def __str__(self) -> str:
        return f"hostile exception value {SECRET}"


class RaisingFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        raise RuntimeError(f"hostile formatter {SECRET}")


class HostileOutputFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return HostileStr(SECRET)


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
    redaction_module.configure_redacting_handler(handler, redactor)
    records: list[logging.LogRecord] = []

    class Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    capture = Capture()
    capture.addFilter(RedactingFilter(redactor))
    logger.addHandler(capture)

    output = StringIO()
    handler.setStream(output)
    logger.addHandler(handler)
    if level == logging.INFO:
        logger.info(message, *args, exc_info=exc_info, extra=extra)
    else:
        logger.error(message, *args, exc_info=exc_info, extra=extra)
    return output.getvalue(), records[0]


def _contains_exact(value: Any, target: str) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_exact(key, target) or _contains_exact(item, target)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple, set, frozenset)):
        return any(_contains_exact(item, target) for item in value)
    return type(value) is str and value == target


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


@pytest.mark.parametrize("secret", ["a", "abc", "•••"])
def test_register_rejects_nonempty_secrets_shorter_than_four_characters(
    secret: str,
) -> None:
    redactor = SecretRedactor([])

    with pytest.raises(ValueError, match="at least 4 characters"):
        redactor.register(secret)

    with pytest.raises(ValueError, match="at least 4 characters"):
        SecretRedactor([secret])


def test_redaction_marker_is_stable_when_a_secret_overlaps_the_marker() -> None:
    redactor = SecretRedactor(["REDACTED"])

    first = redactor.clean("REDACTED")
    second = redactor.clean(first)

    assert first == REDACTED_MARKER
    assert second == REDACTED_MARKER


@pytest.mark.parametrize("secret", ["REDACTED", "[REDACTED]", "REDACTION", "e000"])
def test_every_marker_is_resolved_without_registered_secret_collisions(
    secret: str,
) -> None:
    redactor = SecretRedactor([secret], max_depth=3)
    cyclic: dict[str, Any] = {"value": secret}
    cyclic["self"] = cyclic
    nested: list[Any] = [secret]
    cursor = nested
    for _ in range(8):
        child: list[Any] = [secret]
        cursor.append(child)
        cursor = child
    unhashable_key = HashableSecretMapping()
    unrenderable_log, _record = _render_log(redactor, UnrenderableObject())

    results = [
        redactor.clean(f"before {secret} after"),
        redactor.clean(cyclic),
        redactor.clean(nested),
        redactor.clean(FullyUnrenderableObject()),
        redactor.clean({unhashable_key: "safe"}),
        unrenderable_log,
    ]

    for result in results:
        assert secret not in str(result)
        assert secret not in repr(result)


@pytest.mark.parametrize(
    "secret",
    [f"prefix{REDACTED_MARKER}suffix", r"prefix\ue000suffix"],
    ids=["raw-marker", "repr-fragment"],
)
def test_marker_resolver_rejects_marker_representations_inside_secret(
    secret: str,
) -> None:
    redactor = SecretRedactor([secret])
    marker = redactor.redacted_marker
    representation_fragment = repr(marker)[1:-1]

    assert marker != REDACTED_MARKER
    assert marker not in secret
    assert representation_fragment not in secret
    cleaned = redactor.clean(secret)
    assert secret not in str(cleaned)
    assert secret not in repr(cleaned)


def test_current_marker_idempotence_and_unsafe_literal_replacement() -> None:
    redactor = SecretRedactor(["REDACTED"])

    first = redactor.clean("[REDACTED]")
    second = redactor.clean(first)

    assert first == second
    assert "REDACTED" not in str(first)
    assert "REDACTED" not in repr(first)


def test_top_level_audit_collapses_structural_representation_collisions() -> None:
    secret = "['ok"
    redactor = SecretRedactor([secret])

    cleaned = redactor.clean(["ok"])

    assert type(cleaned) is str
    assert secret not in str(cleaned)
    assert secret not in repr(cleaned)


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
    assert _contains_exact(cleaned, redactor.cycle_marker)


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
    assert _contains_exact(cleaned, redactor.cycle_marker)


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


def test_clean_fails_closed_for_exception_with_hostile_metaclass() -> None:
    redactor = SecretRedactor([SECRET])

    cleaned = redactor.clean(HostileMetaclassError())

    assert type(cleaned) is RuntimeError
    assert SECRET not in str(cleaned)
    assert SECRET not in repr(cleaned)


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
    assert _contains_exact(cleaned_cycle, redactor.cycle_marker)
    assert _contains_exact(cleaned_depth, redactor.depth_marker)


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
    assert redactor.unrenderable_log_marker in output


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
    assert redactor.unrenderable_value_marker in output


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
    assert _contains_exact(getattr(record, "payload_set"), redactor.redacted_marker)


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


def test_logging_filter_fails_closed_for_exception_with_hostile_metaclass() -> None:
    redactor = SecretRedactor([SECRET])
    error = HostileMetaclassError()
    record = logging.makeLogRecord(
        {
            "name": "stock_desk.tests.redaction.hostile-metaclass",
            "levelno": logging.ERROR,
            "levelname": "ERROR",
            "msg": "failed",
            "args": (),
            "exc_info": (HostileMetaclassError, error, None),
            "extra_error": error,
        }
    )

    assert RedactingFilter(redactor).filter(record) is True
    output = logging.Formatter("%(message)s|%(extra_error)s").format(record)

    assert SECRET not in output
    assert SECRET not in repr(record.exc_info)


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
    assert _contains_exact(getattr(record, "task_error"), redactor.redacted_marker)


def test_logging_filter_has_no_false_failure_without_secrets() -> None:
    output, record = _render_log(SecretRedactor([]), "ordinary value=%s", "safe")

    assert "ordinary value=safe" in output
    assert record.getMessage() == "ordinary value=safe"


def test_safe_handler_redacts_cross_field_composition_and_preserves_formatter() -> None:
    redactor = SecretRedactor(["abcdef"])
    output = StringIO()
    handler = logging.StreamHandler(output)
    delegate = logging.Formatter("%(left)s%(right)s")
    handler.setFormatter(delegate)

    configured = redaction_module.configure_redacting_handler(handler, redactor)
    logger = logging.getLogger("stock_desk.tests.redaction.cross-field")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(configured)
    logger.info("unused", extra={"left": "abc", "right": "def"})

    rendered = output.getvalue()
    assert "abcdef" not in rendered
    assert redactor.redacted_marker in rendered
    assert isinstance(handler.formatter, redaction_module.RedactingFormatter)
    assert handler.formatter.delegate is delegate
    assert any(isinstance(item, RedactingFilter) for item in handler.filters)


def test_safe_handler_redacts_formatter_literal_and_field_composition() -> None:
    redactor = SecretRedactor(["prefixsecret"])
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(logging.Formatter("prefix%(right)s"))
    redaction_module.configure_redacting_handler(handler, redactor)
    logger = logging.getLogger("stock_desk.tests.redaction.literal-field")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    logger.info("unused", extra={"right": "secret"})

    assert "prefixsecret" not in output.getvalue()
    assert redactor.redacted_marker in output.getvalue()


def test_safe_handler_redacts_composed_exception_and_stack_output() -> None:
    redactor = SecretRedactor([SECRET, "abcdef"])
    output = StringIO()
    handler = logging.StreamHandler(output)
    handler.setFormatter(
        logging.Formatter("%(message)s|%(left)s%(right)s|%(stack_info)s")
    )
    redaction_module.configure_redacting_handler(handler, redactor)
    logger = logging.getLogger("stock_desk.tests.redaction.safe-exception")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    try:
        raise RuntimeError(f"provider rejected {SECRET}")
    except RuntimeError:
        logger.error(
            "failed",
            exc_info=True,
            stack_info=True,
            extra={"left": "abc", "right": "def"},
        )

    rendered = output.getvalue()
    assert SECRET not in rendered
    assert "abcdef" not in rendered


@pytest.mark.parametrize(
    "delegate",
    [RaisingFormatter(), HostileOutputFormatter()],
    ids=["raising", "hostile-output"],
)
def test_final_formatter_fails_closed_for_hostile_delegates(
    delegate: logging.Formatter,
) -> None:
    redactor = SecretRedactor([SECRET])
    record = logging.makeLogRecord(
        {"name": "hostile", "levelno": logging.INFO, "msg": "safe", "args": ()}
    )
    formatter = redaction_module.RedactingFormatter(redactor, delegate)

    rendered = formatter.format(record)

    assert SECRET not in rendered
    assert (
        redactor.unrenderable_log_marker in rendered
        or redactor.redacted_marker in rendered
    )


def test_final_formatter_is_idempotent_for_its_resolved_marker() -> None:
    redactor = SecretRedactor([SECRET])
    formatter = redaction_module.RedactingFormatter(
        redactor, logging.Formatter("%(message)s")
    )
    record = logging.makeLogRecord(
        {"name": "idempotent", "levelno": logging.INFO, "msg": SECRET, "args": ()}
    )

    first = formatter.format(record)
    record.msg = first
    record.args = ()
    second = formatter.format(record)

    assert first == second
    assert SECRET not in second


def test_safe_handler_configuration_is_idempotent() -> None:
    redactor = SecretRedactor([SECRET])
    handler = logging.StreamHandler(StringIO())
    delegate = logging.Formatter("%(message)s")
    handler.setFormatter(delegate)

    first = redaction_module.configure_redacting_handler(handler, redactor)
    first_formatter = handler.formatter
    second = redaction_module.configure_redacting_handler(handler, redactor)

    assert first is handler
    assert second is handler
    assert handler.formatter is first_formatter
    assert sum(isinstance(item, RedactingFilter) for item in handler.filters) == 1


def test_process_redaction_runs_after_logger_and_handler_filters() -> None:
    secret = "filter-injected-secret"
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("scoped.filters")
    logger.handlers.clear()
    logger.filters.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)

    class LoggerInjection(logging.Filter):
        def filter(self, record: logging.LogRecord) -> logging.LogRecord:
            replacement = logging.makeLogRecord(record.__dict__)
            replacement.msg = f"logger={secret}"
            replacement.args = ()
            return replacement

    class HandlerInjection(logging.Filter):
        def filter(self, record: logging.LogRecord) -> logging.LogRecord:
            assert secret in record.getMessage()
            replacement = logging.makeLogRecord(record.__dict__)
            replacement.msg = f"handler={secret}"
            replacement.args = ()
            return replacement

    logger_filter = LoggerInjection()
    handler_filter = HandlerInjection()
    logger.addFilter(logger_filter)
    handler.addFilter(handler_filter)
    logger.addHandler(handler)
    try:
        with redaction_module.scoped_log_redaction(secret):
            logger.warning("safe input")
    finally:
        logger.removeHandler(handler)
        logger.removeFilter(logger_filter)
        handler.close()

    assert secret not in output.getvalue()
    assert REDACTED_MARKER in output.getvalue()


def test_process_redaction_scrubs_formatter_constants_and_restores_formatter() -> None:
    secret = "formatter-constant-secret"
    output = StringIO()
    handler = logging.StreamHandler(output)
    formatter = logging.Formatter(f"constant={secret} %(message)s")
    handler.setFormatter(formatter)
    logger = logging.getLogger("scoped.formatter-constant")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    try:
        with redaction_module.scoped_log_redaction(secret):
            logger.warning("safe input")
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert secret not in output.getvalue()
    assert REDACTED_MARKER in output.getvalue()
    assert handler.formatter is formatter


@pytest.mark.parametrize(
    "entrypoint",
    ["normal", "call_handlers", "direct_handler", "logger_subclass"],
)
def test_process_redaction_covers_standard_logging_dispatch_entrypoints(
    entrypoint: str,
) -> None:
    secret = f"dispatch-{entrypoint}-secret"
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger(f"scoped.dispatch.{entrypoint}")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    record = logging.LogRecord(
        logger.name,
        logging.WARNING,
        __file__,
        1,
        "secret=%s",
        (secret,),
        None,
    )

    class DirectLogger(logging.Logger):
        def handle(self, direct_record: logging.LogRecord) -> None:
            self.callHandlers(direct_record)

    try:
        with redaction_module.scoped_log_redaction(secret):
            if entrypoint == "normal":
                logger.warning("secret=%s", secret)
            elif entrypoint == "call_handlers":
                logger.callHandlers(record)
            elif entrypoint == "direct_handler":
                handler.handle(record)
            else:
                direct_logger = DirectLogger(f"{logger.name}.subclass")
                direct_logger.handlers.append(handler)
                direct_logger.propagate = False
                direct_logger.handle(record)
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert secret not in output.getvalue()
    assert REDACTED_MARKER in output.getvalue()


def test_process_redaction_survives_runtime_logger_handle_replacement() -> None:
    secret = "runtime-logger-replacement-secret"
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("scoped.runtime-logger-replacement")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    original_handle = logging.Logger.handle

    def replacement_handle(
        current_logger: logging.Logger, record: logging.LogRecord
    ) -> None:
        current_logger.callHandlers(record)

    try:
        with redaction_module.scoped_log_redaction(secret):
            logging.Logger.handle = replacement_handle
            logger.warning("secret=%s", secret)
    finally:
        logging.Logger.handle = original_handle
        logger.removeHandler(handler)
        handler.close()

    assert secret not in output.getvalue()
    assert REDACTED_MARKER in output.getvalue()


def test_process_redaction_preserves_handler_logrecord_return_and_state() -> None:
    secret = "replacement-record-secret"
    output = StringIO()
    handler = logging.StreamHandler(output)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)
    original = logging.makeLogRecord(
        {"name": "scoped.replacement", "levelno": logging.WARNING, "msg": "safe"}
    )
    replacement = logging.makeLogRecord(
        {
            "name": "scoped.replacement",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )

    class ReplaceRecord(logging.Filter):
        def filter(self, _record: logging.LogRecord) -> logging.LogRecord:
            return replacement

    replacing_filter = ReplaceRecord()
    handler.addFilter(replacing_filter)
    filters = tuple(handler.filters)

    with redaction_module.scoped_log_redaction(secret):
        result = handler.handle(original)

    assert result is replacement
    assert secret not in output.getvalue()
    assert handler.formatter is formatter
    assert tuple(handler.filters) == filters
    handler.close()


@pytest.mark.parametrize("filter_result", ["truthy", "replacement"])
def test_process_redaction_emits_exact_python312_selected_record_identity(
    filter_result: str,
) -> None:
    secret = f"identity-{filter_result}-secret"
    emitted: list[logging.LogRecord] = []
    original = logging.makeLogRecord(
        {
            "name": "scoped.identity",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )
    replacement = logging.makeLogRecord(
        {
            "name": "scoped.identity.replacement",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )

    class CaptureHandler(logging.Handler):
        def filter(self, _record: logging.LogRecord) -> bool | logging.LogRecord:
            return True if filter_result == "truthy" else replacement

        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record)

    handler = CaptureHandler()
    with redaction_module.scoped_log_redaction(secret):
        result = handler.handle(original)

    expected = original if filter_result == "truthy" else replacement
    assert emitted == [expected]
    assert emitted[0] is expected
    assert result is (True if filter_result == "truthy" else replacement)
    assert secret not in expected.getMessage()
    handler.close()


def test_process_redaction_preserves_false_filter_return_without_emission() -> None:
    secret = "filtered-handler-secret"
    output = StringIO()
    handler = logging.StreamHandler(output)
    formatter = logging.Formatter("%(message)s")
    handler.setFormatter(formatter)

    class RejectRecord(logging.Filter):
        def filter(self, _record: logging.LogRecord) -> bool:
            return False

    rejecting_filter = RejectRecord()
    handler.addFilter(rejecting_filter)
    record = logging.makeLogRecord(
        {"name": "scoped.filtered", "levelno": logging.WARNING, "msg": secret}
    )

    with redaction_module.scoped_log_redaction(secret):
        result = handler.handle(record)

    assert result is False
    assert output.getvalue() == ""
    assert handler.formatter is formatter
    assert handler.filters == [rejecting_filter]
    handler.close()


def test_process_redaction_never_overwrites_concurrent_formatter_update() -> None:
    secret = "concurrent-formatter-secret"
    entered_emit = Event()
    release_emit = Event()
    output = StringIO()

    class BlockingStreamHandler(logging.StreamHandler[Any]):
        def emit(self, record: logging.LogRecord) -> None:
            entered_emit.set()
            assert release_emit.wait(timeout=5)
            super().emit(record)

    handler = BlockingStreamHandler(output)
    old_formatter = logging.Formatter("old=%(message)s")
    new_formatter = logging.Formatter(f"new={secret} %(message)s")
    handler.setFormatter(old_formatter)
    record = logging.makeLogRecord(
        {
            "name": "scoped.concurrent-formatter",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )

    with redaction_module.scoped_log_redaction(secret):
        with ThreadPoolExecutor(max_workers=1) as executor:
            handled = executor.submit(handler.handle, record)
            assert entered_emit.wait(timeout=5)
            handler.setFormatter(new_formatter)
            release_emit.set()
            assert handled.result(timeout=5) is record

    assert handler.formatter is new_formatter
    assert secret not in output.getvalue()
    assert output.getvalue().startswith("new=")
    handler.close()


def test_process_format_hook_inactive_delegates_exact_formatter_result() -> None:
    sentinel = HostileStr("inactive-formatter-result")

    class IdentityFormatter(logging.Formatter):
        def format(self, _record: logging.LogRecord) -> str:
            return sentinel

    handler = logging.Handler()
    handler.setFormatter(IdentityFormatter())
    record = logging.makeLogRecord(
        {"name": "scoped.inactive-format", "levelno": logging.INFO, "msg": "safe"}
    )

    rendered = handler.format(record)

    assert rendered is sentinel
    handler.close()


def test_process_format_hook_preserves_formatter_exception_semantics() -> None:
    secret = "formatter-exception-secret"

    class FormattingFailure(RuntimeError):
        pass

    class FailingFormatter(logging.Formatter):
        def format(self, _record: logging.LogRecord) -> str:
            raise FormattingFailure("expected formatter failure")

    class FormattingHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            self.format(record)

    handler = FormattingHandler()
    handler.setFormatter(FailingFormatter())
    record = logging.makeLogRecord(
        {"name": "scoped.format-failure", "levelno": logging.INFO, "msg": secret}
    )

    with redaction_module.scoped_log_redaction(secret):
        with pytest.raises(FormattingFailure, match="expected formatter failure"):
            handler.handle(record)

    assert isinstance(handler.formatter, FailingFormatter)
    handler.close()


def test_process_redaction_restores_handler_state_and_lock_on_emit_exception() -> None:
    secret = "raising-handler-secret"
    formatter = logging.Formatter("%(message)s")
    events: list[str] = []
    formatter_seen: logging.Formatter | None = None

    class EmissionFailure(RuntimeError):
        pass

    class RaisingHandler(logging.Handler):
        def acquire(self) -> None:
            events.append("acquire")
            super().acquire()

        def release(self) -> None:
            events.append("release")
            super().release()

        def emit(self, _record: logging.LogRecord) -> None:
            nonlocal formatter_seen
            events.append("emit")
            formatter_seen = self.formatter
            raise EmissionFailure("expected")

    handler = RaisingHandler()
    handler.setFormatter(formatter)
    record = logging.makeLogRecord(
        {"name": "scoped.raise", "levelno": logging.WARNING, "msg": secret}
    )

    with redaction_module.scoped_log_redaction(secret):
        with pytest.raises(EmissionFailure, match="expected"):
            handler.handle(record)

    assert formatter_seen is formatter
    assert handler.formatter is formatter
    assert events[-3:] == ["acquire", "emit", "release"]
    handler.close()


def test_delayed_record_retains_longest_first_context_until_record_gc() -> None:
    short = "delayed-prefix-secret"
    long = "delayed-prefix-secret-with-suffix"
    visible_suffix = "-with-suffix"
    records: list[logging.LogRecord] = []

    class DelayedHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = DelayedHandler()
    handler.setFormatter(logging.Formatter(f"constant={long} %(message)s"))
    record = logging.makeLogRecord(
        {
            "name": "scoped.delayed",
            "levelno": logging.WARNING,
            "msg": long,
            "args": (),
        }
    )
    with redaction_module.scoped_log_redaction(short):
        with redaction_module.scoped_log_redaction(long):
            handler.handle(record)

    assert records == [record]
    context = record.__dict__[redaction_module._RECORD_REDACTOR_ATTRIBUTE]
    assert not isinstance(context, SecretRedactor)
    assert short not in repr(context)
    assert long not in repr(context)
    assert repr(context) == "LogRedactionContext(active=True)"
    rendered = handler.format(record)
    assert short not in rendered
    assert long not in rendered
    assert visible_suffix not in rendered

    context_reference = weakref.ref(context)
    records.clear()
    del context
    del record
    gc.collect()
    assert context_reference() is None
    handler.close()


def test_delayed_record_context_transfers_to_downstream_filter_replacement() -> None:
    secret = "delayed-replacement-secret"
    delayed: list[logging.LogRecord] = []

    class DelayedHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            delayed.append(record)

    source_handler = DelayedHandler()
    original = logging.makeLogRecord(
        {
            "name": "scoped.delayed-replacement",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )
    with redaction_module.scoped_log_redaction(secret):
        source_handler.handle(original)

    replacement = logging.makeLogRecord(
        {
            "name": "scoped.delayed-replacement.filtered",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )

    class ReplaceDelayedRecord(logging.Filter):
        def filter(self, _record: logging.LogRecord) -> logging.LogRecord:
            return replacement

    output = StringIO()
    output_handler = logging.StreamHandler(output)
    output_handler.setFormatter(logging.Formatter(f"constant={secret} %(message)s"))
    output_handler.addFilter(ReplaceDelayedRecord())
    result = output_handler.handle(delayed[0])

    assert result is replacement
    assert secret not in replacement.getMessage()
    assert secret not in output.getvalue()
    assert REDACTED_MARKER in output.getvalue()
    source_handler.close()
    output_handler.close()


def test_delayed_record_context_unions_with_active_overlapping_secret() -> None:
    snapshot_secret = "delayed-union-secret"
    active_secret = "delayed-union-secret-with-suffix"
    visible_suffix = "-with-suffix"
    delayed: list[logging.LogRecord] = []

    class DelayedHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            delayed.append(record)

    source_handler = DelayedHandler()
    record = logging.makeLogRecord(
        {
            "name": "scoped.delayed-union",
            "levelno": logging.WARNING,
            "msg": snapshot_secret,
            "args": (),
        }
    )
    with redaction_module.scoped_log_redaction(snapshot_secret):
        source_handler.handle(record)

    class InjectActiveSecret(logging.Filter):
        def filter(self, filtered: logging.LogRecord) -> bool:
            filtered.msg = active_secret
            filtered.args = ()
            return True

    output = StringIO()
    output_handler = logging.StreamHandler(output)
    output_handler.addFilter(InjectActiveSecret())
    output_handler.setFormatter(
        logging.Formatter(
            f"snapshot={snapshot_secret} active={active_secret} %(message)s"
        )
    )
    with redaction_module.scoped_log_redaction(active_secret):
        result = output_handler.handle(delayed[0])

    assert result is delayed[0]
    assert snapshot_secret not in output.getvalue()
    assert active_secret not in output.getvalue()
    assert visible_suffix not in output.getvalue()
    source_handler.close()
    output_handler.close()


def test_socket_handler_pickle_reduces_record_context_to_builtin_none() -> None:
    secret = "socket-pickle-secret"
    payloads: list[bytes] = []

    class CapturingSocketHandler(SocketHandler):
        def emit(self, record: logging.LogRecord) -> None:
            payloads.append(self.makePickle(record))

    handler = CapturingSocketHandler("localhost", 0)
    record = logging.makeLogRecord(
        {
            "name": "scoped.socket-pickle",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )
    with redaction_module.scoped_log_redaction(secret):
        handler.handle(record)

    assert len(payloads) == 1
    payload = payloads[0]
    assert secret.encode() not in payload
    assert b"security.redaction" not in payload
    assert b"LogRedactionContext" not in payload
    assert b"SecretRedactor" not in payload
    restored = pickle.loads(payload[4:])
    assert secret not in repr(restored)
    assert restored[redaction_module._RECORD_REDACTOR_ATTRIBUTE] is None
    handler.close()


def test_queue_handler_prepared_record_is_multiprocessing_pickle_safe() -> None:
    secret = "multiprocessing-queue-secret"
    records: Queue[logging.LogRecord] = Queue()
    handler = QueueHandler(records)
    record = logging.makeLogRecord(
        {
            "name": "scoped.multiprocessing-queue",
            "levelno": logging.WARNING,
            "msg": secret,
            "args": (),
        }
    )
    with redaction_module.scoped_log_redaction(secret):
        handler.handle(record)

    prepared = records.get_nowait()
    assert secret not in prepared.getMessage()
    payload = bytes(ForkingPickler.dumps(prepared))
    assert secret.encode() not in payload
    assert b"security.redaction" not in payload
    assert b"LogRedactionContext" not in payload
    assert b"SecretRedactor" not in payload
    restored = pickle.loads(payload)
    assert secret not in restored.getMessage()
    assert restored.__dict__[redaction_module._RECORD_REDACTOR_ATTRIBUTE] is None
    handler.close()


def test_queue_handler_record_keeps_redaction_context_after_scope_closes() -> None:
    secret = "queued-handler-secret"
    records: Queue[logging.LogRecord] = Queue()
    queue_handler = QueueHandler(records)
    output = StringIO()
    output_handler = logging.StreamHandler(output)
    output_handler.setFormatter(logging.Formatter(f"constant={secret} %(message)s"))
    listener = QueueListener(records, output_handler)
    logger = logging.getLogger("scoped.queue")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(queue_handler)
    try:
        with redaction_module.scoped_log_redaction(secret):
            logger.warning("payload=%s", secret)
        listener.start()
        listener.stop()
    finally:
        logger.removeHandler(queue_handler)
        queue_handler.close()
        output_handler.close()

    assert secret not in output.getvalue()
    assert REDACTED_MARKER in output.getvalue()


@pytest.mark.parametrize("invalid", [None, b"bytes", 123])
def test_register_rejects_non_string_values(invalid: object) -> None:
    redactor = SecretRedactor([])

    with pytest.raises(TypeError):
        redactor.register(invalid)  # type: ignore[arg-type]


def test_scoped_log_redaction_preserves_custom_factory_and_does_not_accumulate() -> (
    None
):
    original = logging.getLogRecordFactory()
    calls: list[str] = []
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("scoped.custom")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)

    def custom_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        calls.append(str(args[0]))
        record = original(*args, **kwargs)
        record.custom_factory_marker = "preserved"
        return record

    logging.setLogRecordFactory(custom_factory)
    wrappers: list[object] = []
    references: list[weakref.ReferenceType[Any]] = []
    try:
        for index in range(32):
            token = f"replacement-secret-{index:02d}"
            with redaction_module.scoped_log_redaction(token) as lease:
                references.append(weakref.ref(lease))
                wrappers.append(logging.Handler.handle)
                assert logging.getLogRecordFactory() is custom_factory
                logger.warning("token=%s", token)
            del lease
            assert logging.getLogRecordFactory() is custom_factory
        gc.collect()
    finally:
        logging.setLogRecordFactory(original)
        logger.removeHandler(handler)
        handler.close()

    assert calls == ["scoped.custom"] * 32
    assert len({id(wrapper) for wrapper in wrappers}) == 1
    assert all(reference() is None for reference in references)
    for index in range(32):
        assert f"replacement-secret-{index:02d}" not in output.getvalue()


def test_overlapping_scoped_log_redaction_redacts_union_across_threads() -> None:
    first_secret = "first-overlap-secret"
    second_secret = "second-overlap-secret"
    entered = Barrier(2)
    logged = Barrier(2)
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("scoped.concurrent")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)

    def render(secret: str) -> None:
        with redaction_module.scoped_log_redaction(secret):
            entered.wait(timeout=5)
            logger.warning("%s %s", first_secret, second_secret)
            logged.wait(timeout=5)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            tuple(executor.map(render, (first_secret, second_secret)))
    finally:
        logger.removeHandler(handler)
        handler.close()

    assert first_secret not in output.getvalue()
    assert second_secret not in output.getvalue()


def test_handle_boundary_survives_factory_replacement_and_direct_records() -> None:
    secret = "factory-independent-secret"
    original_factory = logging.getLogRecordFactory()
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("scoped.handle.factory-replacement")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)

    def replacement_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return original_factory(*args, **kwargs)

    try:
        with redaction_module.scoped_log_redaction(secret):
            logging.setLogRecordFactory(replacement_factory)
            logger.warning("normal=%s", secret)
            direct = logging.LogRecord(
                logger.name,
                logging.WARNING,
                __file__,
                1,
                "direct=%s",
                (secret,),
                None,
            )
            logger.handle(direct)
    finally:
        logging.setLogRecordFactory(original_factory)
        logger.removeHandler(handler)
        handler.close()

    assert secret not in output.getvalue()


def test_overlapping_prefix_secrets_use_one_longest_first_union() -> None:
    short = "prefix-secret"
    long = "prefix-secret-with-suffix"
    visible_suffix = "-with-suffix"
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("scoped.prefix")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)
    record = logging.LogRecord(
        logger.name,
        logging.WARNING,
        __file__,
        1,
        "%s",
        (long,),
        None,
    )

    try:
        with redaction_module.scoped_log_redaction(short):
            with redaction_module.scoped_log_redaction(long):
                captured_wrapper = logging.Handler.handle
                logger.handle(record)

        assert short not in output.getvalue()
        assert long not in output.getvalue()
        assert visible_suffix not in output.getvalue()

        after_scope = logging.LogRecord(
            "scoped.prefix.after",
            logging.WARNING,
            __file__,
            1,
            "ordinary",
            (),
            None,
        )
        captured_wrapper(handler, after_scope)
        assert output.getvalue().endswith("ordinary\n")
    finally:
        logger.removeHandler(handler)
        handler.close()


def test_log_secret_lease_refresh_keeps_one_stable_nonrecursive_handler_hook() -> None:
    output = StringIO()
    handler = logging.StreamHandler(output)
    stable_handle_hook = logging.Handler.handle
    stable_format_hook = logging.Handler.format
    lease = redaction_module.LogSecretLease()
    last_secret = ""
    try:
        for index in range(32):
            last_secret = f"lease-refresh-secret-{index:02d}"
            lease.replace(last_secret)
            record = logging.makeLogRecord(
                {
                    "name": "scoped.lease-refresh",
                    "levelno": logging.WARNING,
                    "msg": last_secret,
                    "args": (),
                }
            )
            handler.handle(record)
            assert logging.Handler.handle is stable_handle_hook
            assert logging.Handler.format is stable_format_hook
            assert last_secret not in output.getvalue()
            output.seek(0)
            output.truncate(0)
    finally:
        lease.close()

    handler.handle(
        logging.makeLogRecord(
            {
                "name": "scoped.lease-refresh.closed",
                "levelno": logging.WARNING,
                "msg": last_secret,
                "args": (),
            }
        )
    )
    assert last_secret in output.getvalue()
    handler.close()


def test_lifetime_secret_leases_union_replace_close_and_collect() -> None:
    first = "first-service-secret"
    second = "second-service-secret"
    scoped = "invocation-secret"
    output = StringIO()
    handler = logging.StreamHandler(output)
    logger = logging.getLogger("scoped.lifetime")
    logger.handlers.clear()
    logger.propagate = False
    logger.setLevel(logging.WARNING)
    logger.addHandler(handler)

    first_lease = redaction_module.LogSecretLease(first)
    second_lease = redaction_module.LogSecretLease(second)
    try:
        with redaction_module.scoped_log_redaction(scoped):
            logger.warning("all=%s %s %s", first, second, scoped)
        assert all(
            secret not in output.getvalue() for secret in (first, second, scoped)
        )

        output.seek(0)
        output.truncate(0)
        first_lease.replace("replacement-service-secret")
        logger.warning("old=%s new=%s", first, "replacement-service-secret")
        assert first in output.getvalue()
        assert "replacement-service-secret" not in output.getvalue()

        reference = weakref.ref(second_lease)
        del second_lease
        gc.collect()
        assert reference() is None
        output.seek(0)
        output.truncate(0)
        logger.warning("collected=%s", second)
        assert second in output.getvalue()
    finally:
        first_lease.close()
        logger.removeHandler(handler)
        handler.close()
