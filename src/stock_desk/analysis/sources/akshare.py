from __future__ import annotations

from collections.abc import Callable
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Protocol, Self

from stock_desk.analysis.snapshot import ResearchSection, ResearchSectionKind
from stock_desk.analysis.sources.base import (
    clean_provider_error,
    Clock,
    research_section_from_table,
)
from stock_desk.market.providers.base import (
    ProviderClientError,
    ProviderInvalidResponse,
    ProviderNoData,
    ProviderTimeout,
    ProviderUnsupported,
    ProviderUnavailable,
)
from stock_desk.market.providers.sdk import (
    call_sdk,
    is_sdk_timeout,
    required_sdk_callable,
)
from stock_desk.market.types import CanonicalSymbol, ProviderId


class AkShareResearchClient(Protocol):
    def stock_financial_analysis_indicator_em(self, **kwargs: object) -> object: ...

    def stock_individual_notice_report(self, **kwargs: object) -> object: ...

    def stock_news_em(self, **kwargs: object) -> object: ...


class _WorkerProcess(Protocol):
    def communicate(
        self,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]: ...

    def kill(self) -> None: ...

    def read_result(self, maximum_bytes: int) -> bytes: ...

    def close_result(self) -> None: ...


WorkerLauncher = Callable[[str, dict[str, object]], _WorkerProcess]
AKSHARE_HARD_TIMEOUT_SECONDS = 20.0
_WORKER_OUTPUT_LIMIT_BYTES = 262_144
_WORKER_OPERATIONS = frozenset(
    {
        "stock_financial_analysis_indicator_em",
        "stock_individual_notice_report",
        "stock_news_em",
    }
)


class _SubprocessWorker:
    def __init__(
        self,
        *,
        process: subprocess.Popen[bytes],
        result_path: Path,
    ) -> None:
        self._process = process
        self._result_path = result_path

    def communicate(
        self,
        input: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[bytes, bytes]:
        if input is not None:
            raise ValueError("worker stdin is disabled")
        self._process.wait(timeout=timeout)
        return b"", b""

    def kill(self) -> None:
        self._process.kill()

    def read_result(self, maximum_bytes: int) -> bytes:
        with self._result_path.open("rb") as result:
            return result.read(maximum_bytes)

    def close_result(self) -> None:
        try:
            self._result_path.unlink()
        except FileNotFoundError:
            pass


def _launch_worker(
    operation: str,
    kwargs: dict[str, object],
) -> _WorkerProcess:
    try:
        encoded = json.dumps(
            kwargs,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError):
        raise ProviderInvalidResponse() from None
    if operation not in _WORKER_OPERATIONS or len(encoded.encode("utf-8")) > 4_096:
        raise ProviderInvalidResponse()
    temporary = tempfile.NamedTemporaryFile(
        prefix="stock-desk-akshare-",
        suffix=".json",
        delete=False,
    )
    temporary.close()
    result_path = Path(temporary.name)
    try:
        process = subprocess.Popen(
            (
                sys.executable,
                "-m",
                "stock_desk.analysis.sources._akshare_worker",
                operation,
                encoded,
                str(result_path),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except BaseException:
        try:
            result_path.unlink(missing_ok=True)
        except BaseException:
            pass
        raise
    return _SubprocessWorker(process=process, result_path=result_path)


def _kill_reap_and_close(
    process: _WorkerProcess,
) -> tuple[bool, KeyboardInterrupt | SystemExit | None]:
    cleaned = True
    interrupt: KeyboardInterrupt | SystemExit | None = None
    try:
        process.kill()
    except (KeyboardInterrupt, SystemExit) as error:
        cleaned = False
        interrupt = error
    except Exception:
        cleaned = False
    try:
        process.communicate(timeout=5.0)
    except (KeyboardInterrupt, SystemExit) as error:
        cleaned = False
        if interrupt is None:
            interrupt = error
    except Exception:
        cleaned = False
    try:
        process.close_result()
    except (KeyboardInterrupt, SystemExit) as error:
        cleaned = False
        if interrupt is None:
            interrupt = error
    except Exception:
        cleaned = False
    return cleaned, interrupt


class AkShareIsolatedSdkFacade:
    """Run timeout-less AKShare SDK calls in a killable worker process."""

    def __init__(
        self,
        *,
        launcher: WorkerLauncher = _launch_worker,
        timeout_seconds: float = AKSHARE_HARD_TIMEOUT_SECONDS,
    ) -> None:
        if not 0 < timeout_seconds <= 120:
            raise ValueError("AKShare worker timeout is invalid")
        self._launcher = launcher
        self._timeout_seconds = timeout_seconds

    def _call(self, operation: str, **kwargs: object) -> object:
        safe_error: ProviderClientError | None = None
        interrupt: KeyboardInterrupt | SystemExit | None = None
        process: _WorkerProcess | None = None
        must_cleanup = False
        try:
            process = self._launcher(operation, dict(kwargs))
            process.communicate(timeout=self._timeout_seconds)
        except subprocess.TimeoutExpired:
            must_cleanup = True
            safe_error = ProviderTimeout()
        except (KeyboardInterrupt, SystemExit) as error:
            must_cleanup = process is not None
            interrupt = error
        except ProviderClientError as error:
            must_cleanup = process is not None
            safe_error = clean_provider_error(error)
        except Exception:
            must_cleanup = process is not None
            safe_error = ProviderInvalidResponse()
        if must_cleanup and process is not None:
            cleaned, cleanup_interrupt = _kill_reap_and_close(process)
            if interrupt is None and cleanup_interrupt is not None:
                interrupt = cleanup_interrupt
            if not cleaned and interrupt is None:
                safe_error = ProviderUnavailable()
        if interrupt is not None:
            raise interrupt
        if safe_error is not None:
            raise safe_error
        if process is None:
            raise ProviderUnavailable()
        result_error: ProviderClientError | None = None
        payload_bytes = b""
        try:
            payload_bytes = process.read_result(_WORKER_OUTPUT_LIMIT_BYTES + 1)
        except Exception:
            result_error = ProviderInvalidResponse()
        try:
            process.close_result()
        except Exception:
            result_error = ProviderUnavailable()
        if result_error is not None:
            raise result_error
        if len(payload_bytes) > _WORKER_OUTPUT_LIMIT_BYTES:
            raise ProviderInvalidResponse()
        try:
            payload = json.loads(payload_bytes)
        except (UnicodeDecodeError, ValueError, TypeError):
            payload = None
        if not isinstance(payload, dict):
            raise ProviderInvalidResponse()
        try:
            canonical_payload = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        except (TypeError, ValueError):
            canonical_payload = None
        if canonical_payload is None or payload_bytes != canonical_payload:
            raise ProviderInvalidResponse()
        if set(payload) == {"status"} and payload["status"] == "no_data":
            raise ProviderNoData()
        if set(payload) == {"status"} and payload["status"] == "timeout":
            raise ProviderTimeout()
        if set(payload) == {"status"} and payload["status"] == "provider_unavailable":
            raise ProviderUnavailable()
        if set(payload) == {"status"} and payload["status"] == "invalid_response":
            raise ProviderInvalidResponse()
        if set(payload) != {"status", "rows"} or payload["status"] != "ok":
            raise ProviderInvalidResponse()
        if not isinstance(payload["rows"], list):
            raise ProviderInvalidResponse()
        return payload["rows"]

    def stock_financial_analysis_indicator_em(self, **kwargs: object) -> object:
        return self._call("stock_financial_analysis_indicator_em", **kwargs)

    def stock_individual_notice_report(self, **kwargs: object) -> object:
        return self._call("stock_individual_notice_report", **kwargs)

    def stock_news_em(self, **kwargs: object) -> object:
        return self._call("stock_news_em", **kwargs)


class AkShareResearchSdkFacade:
    """Minimal facade over the three symbol-scoped AKShare research APIs."""

    def __init__(self, *, module: object) -> None:
        self._module = module

    def _call(self, operation: str, **kwargs: object) -> object:
        safe_error: ProviderClientError | None = None
        try:
            return call_sdk(
                required_sdk_callable(self._module, operation),
                **kwargs,
            )
        except ProviderClientError as error:
            safe_error = clean_provider_error(error)
        except Exception:
            safe_error = ProviderInvalidResponse()
        raise safe_error

    def stock_financial_analysis_indicator_em(self, **kwargs: object) -> object:
        return self._call(
            "stock_financial_analysis_indicator_em",
            **kwargs,
        )

    def stock_individual_notice_report(self, **kwargs: object) -> object:
        return self._call(
            "stock_individual_notice_report",
            **kwargs,
        )

    def stock_news_em(self, **kwargs: object) -> object:
        return self._call("stock_news_em", **kwargs)


class AkShareResearchSource:
    name = ProviderId.AKSHARE

    def __init__(self, *, client: AkShareResearchClient, clock: Clock) -> None:
        self._client = client
        self._clock = clock

    @classmethod
    def from_sdk(cls, *, clock: Clock) -> Self:
        return cls(client=AkShareIsolatedSdkFacade(), clock=clock)

    def fetch(
        self,
        symbol: CanonicalSymbol,
        kind: ResearchSectionKind,
    ) -> ResearchSection:
        code = symbol[:6]
        safe_error: ProviderClientError | None = None
        try:
            if kind is ResearchSectionKind.FUNDAMENTALS:
                table = self._client.stock_financial_analysis_indicator_em(
                    symbol=symbol,
                    indicator="按报告期",
                )
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    identity_fields=("SECUCODE",),
                    expected_identity=symbol,
                    cutoff_fields=("REPORT_DATE",),
                    default_source_url=(
                        "https://emweb.securities.eastmoney.com/pc_hsf10/"
                        f"pages/index.html?type=web&code={symbol[-2:]}{code}#/cwfx"
                    ),
                )
            if kind is ResearchSectionKind.ANNOUNCEMENTS:
                table = self._client.stock_individual_notice_report(
                    security=code,
                    symbol="全部",
                )
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    identity_fields=("代码",),
                    expected_identity=code,
                    cutoff_fields=("公告日期",),
                    published_fields=("公告日期",),
                    url_fields=("网址", "公告链接"),
                    default_source_url=f"https://data.eastmoney.com/notices/stock/{code}.html",
                )
            if kind is ResearchSectionKind.NEWS:
                table = self._client.stock_news_em(symbol=code)
                return research_section_from_table(
                    source=self.name,
                    kind=kind,
                    symbol=symbol,
                    table=table,
                    fetched_at=self._clock(),
                    identity_fields=("关键词",),
                    expected_identity=code,
                    cutoff_fields=("发布时间",),
                    published_fields=("发布时间",),
                    url_fields=("新闻链接",),
                    default_source_url=f"https://so.eastmoney.com/news/s?keyword={code}",
                )
            raise ProviderUnsupported()
        except ProviderClientError as error:
            safe_error = clean_provider_error(error)
        except Exception as error:
            safe_error = (
                ProviderTimeout()
                if is_sdk_timeout(error)
                else ProviderInvalidResponse()
            )
        raise safe_error


__all__ = [
    "AKSHARE_HARD_TIMEOUT_SECONDS",
    "AkShareIsolatedSdkFacade",
    "AkShareResearchClient",
    "AkShareResearchSdkFacade",
    "AkShareResearchSource",
]
