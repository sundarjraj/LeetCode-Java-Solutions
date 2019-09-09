import contextlib
import functools
import sys
from typing import Any, Callable, Tuple, Optional, List, Dict
from typing_extensions import Protocol, AsyncContextManager, runtime_checkable

from app.commons import tracing
from app.commons.stats import get_service_stats_client, get_request_logger
from app.commons.context.logger import root_logger as default_logger
import structlog
from doordash_python_stats import ddstats


# borrowed from "structlog._frames"
def _discover_caller(additional_ignores=None) -> Tuple[Any, str]:
    """
    Remove all app.commons.tracing calls and return the relevant app frame.

    :param additional_ignores: Additional names with which the first frame must
        not start.
    :type additional_ignores: `list` of `str` or `None`

    :rtype: tuple of (frame, name)
    """
    ignores = [
        # current module
        __name__,
        # tracing module
        "app.commons.tracing",
    ] + (additional_ignores or [])
    f = sys._getframe()
    name = f.f_globals.get("__name__") or "?"
    while any(tuple(name.startswith(i) for i in ignores)):
        if f.f_back is None:
            name = "?"
            break
        f = f.f_back
        name = f.f_globals.get("__name__") or "?"
    return f, name


@runtime_checkable
class Database(Protocol):
    database_name: str
    instance_name: str


class Logger(Protocol):
    log: structlog.BoundLoggerBase


class DatabaseTimer(tracing.MetricTimer):
    database: Database
    calling_module_name: str = ""
    calling_function_name: str = ""
    stack_frame: Any = None

    def __init__(self, send_metrics, database: Database, additional_ignores=None):
        super().__init__(send_metrics)
        self.database = database
        self.additional_ignores = additional_ignores

    def __enter__(self):
        super().__enter__()
        self.stack_frame, self.calling_module_name = _discover_caller(
            additional_ignores=self.additional_ignores
        )
        self.calling_function_name = self.stack_frame.f_code.co_name
        return self


def track_query(func):
    return DatabaseTimingTracker(message="query complete")(func)


def track_transaction(func):
    return TransactionTimingTracker(message="transaction complete")(func)


def log_query_timing(tracker: "DatabaseTimingTracker", timer: DatabaseTimer):
    if not isinstance(timer.database, Database):
        return
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)

    transaction_name = tracing.get_transaction_name()

    database = {
        "name": timer.database.database_name,
        "transaction": bool(transaction_name),
        "transaction_name": transaction_name,
        "instance": timer.database.instance_name,
    }
    caller = {
        "module": timer.calling_module_name,
        "service": tracing.get_application_name(),
        "processor": tracing.get_processor_name(),
        "repository": tracing.get_repository_name(),
    }
    log.info(
        tracker.message,
        query=timer.calling_function_name,
        latency_ms=round(timer.delta_ms, 3),
        database=database,
        caller=caller,
    )


def log_transaction_timing(tracker: "DatabaseTimingTracker", timer: DatabaseTimer):
    if not isinstance(timer.database, Database):
        return
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)

    database = {
        "name": timer.database.database_name,
        "instance": timer.database.instance_name,
    }
    caller = {
        "module": timer.calling_module_name,
        "service": tracing.get_application_name(),
        "processor": tracing.get_processor_name(),
        "repository": tracing.get_repository_name(),
    }
    log.info(
        tracker.message,
        transaction=timer.calling_function_name,
        latency_ms=round(timer.delta_ms, 3),
        database=database,
        caller=caller,
    )


def stat_query_timing(tracker: "DatabaseTimingTracker", timer: DatabaseTimer):
    if not isinstance(timer.database, Database):
        return

    stats: ddstats.DoorStatsProxyMultiServer = get_service_stats_client()
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)
    transaction_name = tracing.get_transaction_name()

    stat_name = (
        f"io.{timer.database.database_name}.query.{timer.calling_function_name}.latency"
    )
    tags = {
        # "query_type": "",
        "transaction": "yes" if transaction_name else "no",
        "transaction_name": transaction_name,
        "instance": timer.database.instance_name,
    }
    stats.timing(stat_name, timer.delta_ms, tags=tags)
    log.debug("statsd: %s", stat_name, latency_ms=timer.delta_ms, tags=tags)


def stat_transaction_timing(tracker: "DatabaseTimingTracker", timer: DatabaseTimer):
    if not isinstance(timer.database, Database):
        return

    stats: ddstats.DoorStatsProxyMultiServer = get_service_stats_client()
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)

    stat_name = f"io.{timer.database.database_name}.transaction.{timer.calling_function_name}.latency"
    tags = {"instance": timer.database.instance_name}
    stats.timing(stat_name, timer.delta_ms, tags=tags)
    log.debug("statsd: %s", stat_name, latency_ms=timer.delta_ms, tags=tags)


class DatabaseTimingTracker(tracing.TimingTracker[DatabaseTimer]):
    """
    Tracker for database queries and transactions
    """

    message: str
    log: structlog.stdlib.BoundLogger
    # database query processors
    processors = [log_query_timing, stat_query_timing]
    stats: ddstats.DoorStatsProxyMultiServer

    def __init__(self, *, message: str):
        super().__init__()
        self.message = message

    def create_tracker(
        self,
        obj=tracing.NotSpecified,
        additional_ignores: Optional[List[str]] = None,
        *,
        func: Callable,
        args: List[Any],
        kwargs: Dict[str, Any],
    ) -> DatabaseTimer:
        additional_ignores = additional_ignores or ["app.commons.database"]
        return DatabaseTimer(
            self.process_metrics, database=obj, additional_ignores=additional_ignores
        )

    def __call__(self, func_or_class):
        return self._decorate_class_method(func_or_class)


class TransactionTimingTracker(DatabaseTimingTracker):
    # database transaction processors
    processors = [log_query_timing, stat_transaction_timing]

    def __call__(self, func):
        @functools.wraps(func)
        def wrapper(obj: Database, *args, **kwargs):
            # chain together a context managers
            @contextlib.asynccontextmanager
            async def wrap_transaction(obj: Database, manager: AsyncContextManager):
                async with contextlib.AsyncExitStack() as stack:
                    # timing: start timing transaction
                    tracker: DatabaseTimer = stack.enter_context(
                        self.create_tracker(
                            obj=obj,
                            additional_ignores=["app.commons.database", "contextlib"],
                            func=func,
                            args=args,
                            kwargs=kwargs,
                        )
                    )
                    # tracing: set transaction name
                    stack.enter_context(
                        tracing.breadcrumb_as(
                            tracing.Breadcrumb(
                                transaction_name=tracker.calling_function_name
                            )
                        )
                    )
                    # start transaction
                    transaction = await stack.enter_async_context(manager)
                    yield transaction

            transaction_manager = func(obj, *args, **kwargs)
            return wrap_transaction(obj, transaction_manager)

        return wrapper


def log_request_timing(tracker: "ClientTimingTracker", timer: "ClientTimer"):
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)

    context = timer.breadcrumb.dict(
        include={"provider_name", "action", "resource", "status_code", "country"},
        skip_defaults=True,
    )
    log.info(
        "client request complete", latency_ms=round(timer.delta_ms, 3), context=context
    )


def stat_request_timing(tracker: "ClientTimingTracker", timer: "ClientTimer"):
    log: structlog.stdlib.BoundLogger = get_request_logger(default=default_logger)
    stats: ddstats.DoorStatsProxyMultiServer = get_service_stats_client()

    stat_name = "io.stripe-lib.latency"
    tags = timer.breadcrumb.dict(
        include={"provider_name", "action", "resource", "status_code", "country"},
        skip_defaults=True,
    )
    stats.timing(stat_name, timer.delta_ms, tags=tags)
    log.debug("statsd: %s", stat_name, latency_ms=timer.delta_ms, tags=tags)


class ClientTimer(tracing.MetricTimer):
    breadcrumb: tracing.Breadcrumb = tracing.Breadcrumb()

    def __init__(self, send_metrics):
        super().__init__(send_metrics)

    def __enter__(self):
        self.breadcrumb = tracing.get_current_breadcrumb()
        return super().__enter__()


class ClientTimingTracker(tracing.TimingTracker[ClientTimer]):
    processors = [log_request_timing, stat_request_timing]

    def create_tracker(
        self,
        obj=tracing.NotSpecified,
        *,
        func: Callable,
        args: List[Any],
        kwargs: Dict[str, Any],
    ):
        return ClientTimer(self.process_metrics)


def track_client_response(**kwargs):
    return ClientTimingTracker(**kwargs)
