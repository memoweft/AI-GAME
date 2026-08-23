from __future__ import annotations

from typing import Any

from .application_runtime import RuntimeNotFound


class ApplicationRuntimeCatalog:
    """Route frozen application profiles without making one adapter universal.

    Each runtime owns a separate durable database and coordinator.  The
    catalog only selects by the already-frozen profile ID; it never infers an
    account, starts a third-party owner for local work, or changes a binding.
    """

    def __init__(self, runtimes: dict[str, Any], *, scheduler_profile_id: str) -> None:
        if not runtimes:
            raise ValueError("application runtime catalog requires at least one runtime")
        if scheduler_profile_id not in runtimes:
            raise ValueError("scheduler profile must be registered")
        self._runtimes = dict(runtimes)
        self._scheduler_profile_id = scheduler_profile_id

    def start(
        self,
        profile_id: str,
        client_request_id: str,
        target_id: str | None = None,
        initial_input: str | None = None,
    ) -> Any:
        return self._for_profile(profile_id).start(
            profile_id,
            client_request_id,
            target_id=target_id,
            initial_input=initial_input,
        )

    def command(self, instance_id: str, command: Any, client_request_id: str) -> Any:
        return self._for_instance(instance_id).command(
            instance_id, command, client_request_id
        )

    def report_owner_event(self, instance_id: str, event: Any) -> Any:
        runtime = self._for_instance(instance_id)
        reporter = getattr(runtime, "report_owner_event", None)
        if not callable(reporter):
            raise RuntimeNotFound(instance_id)
        return reporter(instance_id, event)

    def inspect(self, instance_id: str) -> Any:
        return self._for_instance(instance_id).inspect(instance_id)

    def list(self, limit: int = 100) -> list[Any]:
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        items = [
            item
            for runtime in self._runtimes.values()
            for item in runtime.list(limit)
        ]
        items.sort(
            key=lambda item: (
                str(getattr(item, "created_at", "")),
                str(getattr(item, "instance_id", "")),
            ),
            reverse=True,
        )
        return items[:limit]

    def startup(self) -> None:
        for runtime in self._runtimes.values():
            startup = getattr(runtime, "startup", None)
            if callable(startup):
                startup()

    def shutdown(self) -> None:
        first_error: Exception | None = None
        for runtime in reversed(tuple(self._runtimes.values())):
            shutdown = getattr(runtime, "shutdown", None)
            if not callable(shutdown):
                continue
            try:
                shutdown()
            except Exception as error:
                if first_error is None:
                    first_error = error
        if first_error is not None:
            raise first_error

    def scheduler_status(self) -> Any:
        status = getattr(self._for_profile(self._scheduler_profile_id), "scheduler_status", None)
        if not callable(status):
            raise RuntimeNotFound(self._scheduler_profile_id)
        return status()

    def _for_profile(self, profile_id: str) -> Any:
        try:
            return self._runtimes[profile_id]
        except KeyError:
            raise ValueError("profile_id is not registered in this runtime catalog") from None

    def _for_instance(self, instance_id: str) -> Any:
        for runtime in self._runtimes.values():
            try:
                runtime.inspect(instance_id)
            except RuntimeNotFound:
                continue
            return runtime
        raise RuntimeNotFound(instance_id)
