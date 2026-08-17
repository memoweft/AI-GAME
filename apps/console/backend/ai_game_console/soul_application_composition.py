from __future__ import annotations

import hashlib
import sqlite3
import threading
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .application_runtime import ApplicationRuntime, ApplicationRuntimeError, RuntimeClosed
from .application_runtime.store import _SQLiteApplicationStore
from .applications.soul import (
    PROFILE_ID,
    SCHEDULER_CONTROLLER_REF,
    LoopbackSoulVisionClient,
    ReplyLearningStore,
    SoulApplicationError,
    SoulOwnerClient,
    build_soul_application_ports,
)
from .cloud_config import CloudChatConfiguration
from .config import Settings
from .gui_owl_client import GuiOwlClientError, _loopback_chat_completions_endpoint


APPLICATION_DATABASE_FILENAME = "application-runtime.db"
REPLY_LEARNING_DATABASE_FILENAME = "soul-reply-learning.db"
SCHEDULER_LIFECYCLE_DATABASE_FILENAME = "soul-scheduler-lifecycle.db"
DATE_AS_YUN_PERSONA_SHA256 = (
    "24eac3791fc2537c2362d43b2ca58d5ddeb995b159d60599cb358694ca09d85a"
)
DATE_AS_YUN_PERSONA_VERSION = (
    "date-as-yun-runtime-v1:sha256:" + DATE_AS_YUN_PERSONA_SHA256
)
_DATE_AS_YUN_RESOURCE = "date-as-yun-runtime-v1.md"


@dataclass(frozen=True, slots=True)
class DateAsYunPersona:
    prompt: str
    version: str
    source_path: Path


class SoulApplicationUnavailable(ApplicationRuntimeError):
    """Stable public failure while production dependencies are not ready."""

    code = "soul_application_dependencies_unavailable"

    def __init__(self, reason: str = "unavailable") -> None:
        # ``reason`` is one of this module's inert constants. The API never
        # exposes exception text or underlying provider/owner errors.
        super().__init__(reason)
        self.reason = reason


class SQLiteApplicationArchive:
    """History plus the core-owned idle-stop settlement seam."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self._store = _SQLiteApplicationStore(self.database_path)

    def inspect(self, instance_id: str):
        return self._store.inspect(instance_id)

    def list(self, limit: int = 100):
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        return self._store.list(limit)

    def lifecycle_instances(self, profile_id: str):
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        try:
            instance_ids = [
                str(row[0])
                for row in connection.execute(
                    "SELECT instance_id FROM application_instances "
                    "WHERE profile_id=?",
                    (profile_id,),
                ).fetchall()
            ]
        finally:
            connection.close()
        return [self._store.inspect(instance_id) for instance_id in instance_ids]

    def settle_stop_if_idle(self, instance_id: str) -> bool:
        """Delegate the core CAS; never clear a worker or unfinished intent."""

        return self._store.settle_stop_if_idle(instance_id)


@dataclass(frozen=True, slots=True)
class SoulSchedulerLifecycleRecord:
    requested_state: str
    desired_state: str | None
    source_instance_id: str | None
    transition_ref: str
    generation: int
    updated_at: str


class SQLiteSoulSchedulerLifecycleStore:
    """One content-free durable desired-state receipt for the global scheduler."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        with self._connection() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS soul_scheduler_lifecycle(
                  singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                  requested_state TEXT NOT NULL,
                  desired_state TEXT,
                  source_instance_id TEXT,
                  transition_ref TEXT NOT NULL,
                  generation INTEGER NOT NULL,
                  updated_at TEXT NOT NULL
                )
                """
            )

    def get(self) -> SoulSchedulerLifecycleRecord | None:
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT requested_state,desired_state,source_instance_id,"
                "transition_ref,generation,updated_at "
                "FROM soul_scheduler_lifecycle WHERE singleton=1"
            ).fetchone()
        return self._record(row) if row is not None else None

    def record(
        self,
        *,
        requested_state: str,
        desired_state: str | None,
        source_instance_id: str | None,
        transition_ref: str,
    ) -> SoulSchedulerLifecycleRecord:
        if requested_state not in {"running", "paused", "stopped"}:
            raise ValueError("invalid requested scheduler state")
        if desired_state not in {"running", "paused", "stopped", None}:
            raise ValueError("invalid desired scheduler state")
        if len(transition_ref) != 64 or any(
            character not in "0123456789abcdef" for character in transition_ref
        ):
            raise ValueError("invalid scheduler transition ref")
        if source_instance_id is not None and len(source_instance_id) > 192:
            raise ValueError("invalid scheduler source instance")
        with self._lock, self._connection() as db:
            row = db.execute(
                "SELECT requested_state,desired_state,source_instance_id,"
                "transition_ref,generation,updated_at "
                "FROM soul_scheduler_lifecycle WHERE singleton=1"
            ).fetchone()
            current = self._record(row) if row is not None else None
            if current is not None and current.transition_ref == transition_ref:
                return current
            generation = 1 if current is None else current.generation + 1
            updated_at = _utc_now()
            db.execute(
                """
                INSERT INTO soul_scheduler_lifecycle(
                  singleton,requested_state,desired_state,source_instance_id,
                  transition_ref,generation,updated_at)
                VALUES(1,?,?,?,?,?,?)
                ON CONFLICT(singleton) DO UPDATE SET
                  requested_state=excluded.requested_state,
                  desired_state=excluded.desired_state,
                  source_instance_id=excluded.source_instance_id,
                  transition_ref=excluded.transition_ref,
                  generation=excluded.generation,
                  updated_at=excluded.updated_at
                """,
                (
                    requested_state,
                    desired_state,
                    source_instance_id,
                    transition_ref,
                    generation,
                    updated_at,
                ),
            )
        return SoulSchedulerLifecycleRecord(
            requested_state,
            desired_state,
            source_instance_id,
            transition_ref,
            generation,
            updated_at,
        )

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _record(row: sqlite3.Row) -> SoulSchedulerLifecycleRecord:
        return SoulSchedulerLifecycleRecord(
            str(row["requested_state"]),
            str(row["desired_state"]) if row["desired_state"] is not None else None,
            (
                str(row["source_instance_id"])
                if row["source_instance_id"] is not None
                else None
            ),
            str(row["transition_ref"]),
            int(row["generation"]),
            str(row["updated_at"]),
        )


class SoulApplicationRuntimeGateway:
    """Lazy execution interface that leaves the history archive always readable."""

    def __init__(
        self,
        *,
        archive: SQLiteApplicationArchive,
        dependency_probe: Callable[[], None],
        runtime_factory: Callable[[], Any] | None,
        scheduler_reader: Callable[[], Mapping[str, Any]] | None = None,
        scheduler_controller: Callable[[str], Mapping[str, Any]] | None = None,
        scheduler_lifecycle_store: SQLiteSoulSchedulerLifecycleStore | None = None,
        activation_retry_delays: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0),
        lifecycle_poll_seconds: float = 5.0,
        lifecycle_retry_delays: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0),
        activation_shutdown_timeout_seconds: float = 10.0,
        runtime_shutdown_timeout_seconds: float = 75.0,
    ) -> None:
        if (
            not activation_retry_delays
            or any(delay <= 0 for delay in activation_retry_delays)
            or not lifecycle_retry_delays
            or any(delay <= 0 for delay in lifecycle_retry_delays)
            or lifecycle_poll_seconds <= 0
            or activation_shutdown_timeout_seconds <= 0
            or runtime_shutdown_timeout_seconds <= 0
        ):
            raise ValueError("activation and shutdown timeouts must be positive")
        self._archive = archive
        self._dependency_probe = dependency_probe
        self._runtime_factory = runtime_factory
        self._scheduler_reader = scheduler_reader
        self._scheduler_controller = scheduler_controller
        self._scheduler_lifecycle_store = scheduler_lifecycle_store
        self._activation_retry_delays = tuple(
            float(delay) for delay in activation_retry_delays
        )
        self._activation_shutdown_timeout_seconds = float(
            activation_shutdown_timeout_seconds
        )
        self._lifecycle_poll_seconds = float(lifecycle_poll_seconds)
        self._lifecycle_retry_delays = tuple(
            float(delay) for delay in lifecycle_retry_delays
        )
        self._runtime_shutdown_timeout_seconds = float(
            runtime_shutdown_timeout_seconds
        )
        self._runtime: Any | None = None
        self._closed = False
        self._lock = threading.RLock()
        self._runtime_init_lock = threading.Lock()
        self._operation_lock = threading.RLock()
        self._scheduler_lock = threading.RLock()
        self._activation_stop = threading.Event()
        self._activation_thread: threading.Thread | None = None
        self._lifecycle_stop = threading.Event()
        self._lifecycle_thread: threading.Thread | None = None

    def start(
        self,
        profile_id: str,
        client_request_id: str,
        target_id: str | None = None,
        initial_input: str | None = None,
    ):
        if profile_id != PROFILE_ID:
            raise ValueError("profile_id must match runtime profile")
        runtime = self._runtime_for_new_work()
        try:
            with self._operation_lock:
                with self._lock:
                    if self._closed:
                        raise RuntimeClosed("application runtime is closed")
                state = runtime.start(
                    profile_id,
                    client_request_id,
                    target_id=target_id,
                    initial_input=initial_input,
                )
                record = self._refresh_scheduler_desired(fallback=state)
                self._reconcile_scheduler_record(record)
        finally:
            self._start_lifecycle_reconciler()
        return state

    def command(self, instance_id: str, command, client_request_id: str):
        runtime = self._runtime_for_new_work()
        try:
            with self._operation_lock:
                with self._lock:
                    if self._closed:
                        raise RuntimeClosed("application runtime is closed")
                state = runtime.command(instance_id, command, client_request_id)
                record = (
                    None
                    if getattr(command, "tag", None) == "Input"
                    else self._refresh_scheduler_desired(fallback=state)
                )
                self._reconcile_scheduler_record(record)
        finally:
            self._start_lifecycle_reconciler()
        return state

    def startup(self) -> None:
        """Activate recovery when dependencies are ready; never create an instance."""

        try:
            self._runtime_for_new_work()
        except SoulApplicationUnavailable as error:
            if error.reason in _RETRYABLE_ACTIVATION_REASONS:
                self._start_activation_retry()
        # Matcher acquisition is an owner-side lifecycle independent from the
        # local vision/cloud reply worker. Durable scheduler evidence must keep
        # converging even while reply dependencies cannot construct a runtime.
        self._activate_lifecycle_reconciler()

    def inspect(self, instance_id: str):
        return self._archive.inspect(instance_id)

    def list(self, limit: int = 100):
        return self._archive.list(limit)

    def scheduler_status(self) -> Mapping[str, Any]:
        if self._scheduler_reader is None:
            raise SoulApplicationUnavailable("scheduler_status_unavailable")
        try:
            with self._scheduler_lock:
                payload = self._scheduler_reader()
            return _scheduler_status_projection(payload)
        except SoulApplicationUnavailable:
            raise
        except Exception:
            raise SoulApplicationUnavailable(
                "scheduler_status_unavailable"
            ) from None

    def shutdown(self) -> None:
        with self._lock:
            self._closed = True
            self._activation_stop.set()
            self._lifecycle_stop.set()
            runtime = self._runtime
            activation_thread = self._activation_thread
            lifecycle_thread = self._lifecycle_thread

        activation_error: TimeoutError | None = None
        if (
            activation_thread is not None
            and activation_thread is not threading.current_thread()
        ):
            activation_thread.join(self._activation_shutdown_timeout_seconds)
            if activation_thread.is_alive():
                activation_error = TimeoutError(
                    "soul application activation shutdown timed out"
                )

        lifecycle_error: TimeoutError | None = None
        if (
            lifecycle_thread is not None
            and lifecycle_thread is not threading.current_thread()
        ):
            lifecycle_thread.join(self._activation_shutdown_timeout_seconds)
            if lifecycle_thread.is_alive():
                lifecycle_error = TimeoutError(
                    "soul scheduler lifecycle shutdown timed out"
                )

        runtime_error: TimeoutError | None = None
        if runtime is not None:
            try:
                runtime.shutdown(timeout=self._runtime_shutdown_timeout_seconds)
            except TimeoutError as error:
                # Keep the reference: a later shutdown call must be able to
                # join the same core runtime after its in-flight call returns.
                runtime_error = error
            else:
                with self._lock:
                    if self._runtime is runtime:
                        self._runtime = None

        if runtime_error is not None:
            raise runtime_error
        if activation_error is not None:
            raise activation_error
        if lifecycle_error is not None:
            raise lifecycle_error

    def _runtime_for_new_work(self):
        with self._lock:
            if self._closed:
                raise RuntimeClosed("application runtime is closed")
            if self._runtime is not None:
                return self._runtime

        # Dependency calls and runtime construction can block on owner/model
        # transports.  They must not hold the state lock that establishes the
        # shutdown fence.  The init lock keeps construction single-flight.
        with self._runtime_init_lock:
            with self._lock:
                if self._closed:
                    raise RuntimeClosed("application runtime is closed")
                if self._runtime is not None:
                    return self._runtime
                runtime_factory = self._runtime_factory
            self._dependency_probe()
            with self._lock:
                if self._closed:
                    raise RuntimeClosed("application runtime is closed")
            if runtime_factory is None:
                raise SoulApplicationUnavailable("composition_unavailable")
            try:
                candidate = runtime_factory()
            except SoulApplicationUnavailable:
                raise
            except Exception:
                raise SoulApplicationUnavailable(
                    "runtime_initialization_failed"
                ) from None
            with self._lock:
                if not self._closed:
                    self._runtime = candidate
                    return candidate
            # Shutdown won while the factory was outside the state lock.  Do
            # not publish or use the late runtime, and make a bounded effort to
            # close any coordinator it created.
            try:
                candidate.shutdown(timeout=self._runtime_shutdown_timeout_seconds)
            finally:
                raise RuntimeClosed("application runtime is closed")

    def _start_activation_retry(self) -> None:
        with self._lock:
            if self._closed or self._runtime is not None:
                return
            if (
                self._activation_thread is not None
                and self._activation_thread.is_alive()
            ):
                return
            self._activation_stop.clear()
            thread = threading.Thread(
                target=self._activation_retry_loop,
                name="soul-application-activation",
                daemon=True,
            )
            self._activation_thread = thread
            thread.start()

    def _activation_retry_loop(self) -> None:
        try:
            attempt = 0
            while not self._activation_stop.is_set():
                delay = self._activation_retry_delays[
                    min(attempt, len(self._activation_retry_delays) - 1)
                ]
                if self._activation_stop.wait(delay):
                    return
                try:
                    self._runtime_for_new_work()
                except SoulApplicationUnavailable as error:
                    if error.reason not in _RETRYABLE_ACTIVATION_REASONS:
                        return
                    attempt += 1
                    continue
                except RuntimeClosed:
                    return
                self._activate_lifecycle_reconciler()
                return
        finally:
            with self._lock:
                if self._activation_thread is threading.current_thread():
                    self._activation_thread = None

    def _activate_lifecycle_reconciler(self) -> None:
        try:
            with self._operation_lock:
                record = self._refresh_scheduler_desired()
                self._reconcile_scheduler_record(record)
        except SoulApplicationUnavailable:
            pass
        finally:
            self._start_lifecycle_reconciler()

    def _start_lifecycle_reconciler(self) -> None:
        if self._scheduler_lifecycle_store is None:
            return
        with self._lock:
            if self._closed:
                return
            if (
                self._lifecycle_thread is not None
                and self._lifecycle_thread.is_alive()
            ):
                return
            self._lifecycle_stop.clear()
            thread = threading.Thread(
                target=self._lifecycle_reconcile_loop,
                name="soul-scheduler-lifecycle",
                daemon=True,
            )
            self._lifecycle_thread = thread
            thread.start()

    def _lifecycle_reconcile_loop(self) -> None:
        attempt = 0
        delay = self._lifecycle_poll_seconds
        try:
            while not self._lifecycle_stop.wait(delay):
                try:
                    with self._operation_lock:
                        record = self._refresh_scheduler_desired()
                        self._reconcile_scheduler_record(record)
                except SoulApplicationUnavailable:
                    delay = self._lifecycle_retry_delays[
                        min(attempt, len(self._lifecycle_retry_delays) - 1)
                    ]
                    attempt += 1
                else:
                    attempt = 0
                    delay = self._lifecycle_poll_seconds
        finally:
            with self._lock:
                if self._lifecycle_thread is threading.current_thread():
                    self._lifecycle_thread = None

    def _refresh_scheduler_desired(
        self,
        *,
        fallback: Any | None = None,
    ) -> SoulSchedulerLifecycleRecord | None:
        store = self._scheduler_lifecycle_store
        if store is None:
            return None
        try:
            current = store.get()
            lifecycle_instances = getattr(
                self._archive, "lifecycle_instances", None
            )
            raw_instances = (
                lifecycle_instances(PROFILE_ID)
                if callable(lifecycle_instances)
                else self._archive.list(limit=500)
            )
            instances = [
                item
                for item in raw_instances
                if _state_value(item, "profile_id") == PROFILE_ID
            ]
        except Exception:
            raise SoulApplicationUnavailable(
                "scheduler_lifecycle_store_unavailable"
            ) from None
        # A cold process may have accepted Stop while no physical intent was
        # open, then restart with reply-model dependencies unavailable.  The
        # core CAS is dependency-free and settles only a genuinely idle stop;
        # worker-owned or unfinished owner work remains untouched.
        settle_stop_if_idle = getattr(self._archive, "settle_stop_if_idle", None)
        with self._lock:
            runtime_is_unavailable = self._runtime is None
        if runtime_is_unavailable and callable(settle_stop_if_idle):
            try:
                refreshed: list[Any] = []
                for instance in instances:
                    instance_id = _instance_id(instance)
                    if (
                        _state_value(instance, "status") == "stopping"
                        and instance_id
                        and settle_stop_if_idle(instance_id)
                    ):
                        instance = self._archive.inspect(instance_id)
                    refreshed.append(instance)
                instances = refreshed
            except Exception:
                raise SoulApplicationUnavailable(
                    "scheduler_lifecycle_store_unavailable"
                ) from None

        decision = _aggregate_lifecycle_decision(instances, current)
        if decision is None and fallback is not None:
            fallback_status = _state_value(fallback, "status")
            if fallback_status in {
                "queued",
                "running",
                "waiting",
                "paused",
                "stopping",
                "stopped",
            }:
                decision = _fallback_lifecycle_decision(fallback, current)
        if decision is None:
            return current
        try:
            return store.record(
                requested_state=decision.requested_state,
                desired_state=decision.desired_state,
                source_instance_id=decision.source_instance_id,
                transition_ref=decision.transition_ref,
            )
        except Exception:
            raise SoulApplicationUnavailable(
                "scheduler_lifecycle_store_unavailable"
            ) from None

    def _reconcile_scheduler_record(
        self,
        record: SoulSchedulerLifecycleRecord | None,
    ) -> None:
        if record is None or record.desired_state is None:
            return
        if (
            self._scheduler_reader is None
            or self._scheduler_controller is None
            or self._scheduler_lifecycle_store is None
        ):
            raise SoulApplicationUnavailable("scheduler_control_unavailable")
        try:
            with self._scheduler_lock:
                if self._closed or self._lifecycle_stop.is_set():
                    return
                current = self._scheduler_lifecycle_store.get()
                if current is None or current.generation != record.generation:
                    return
                owner_state = self._scheduler_reader()
                current = self._scheduler_lifecycle_store.get()
                if current is None or current.generation != record.generation:
                    return
                if self._closed or self._lifecycle_stop.is_set():
                    return
                if _scheduler_matches(owner_state, record.desired_state):
                    return
                response = self._scheduler_controller(record.desired_state)
                _validate_scheduler_control(response, record.desired_state)
        except SoulApplicationUnavailable:
            raise
        except Exception:
            # A timeout may have committed remotely. Never compensate or send
            # a different desired state. Durable generation + GET converges on
            # the next monitor/replay pass.
            raise SoulApplicationUnavailable(
                "scheduler_control_unavailable"
            ) from None


@dataclass(frozen=True, slots=True)
class SoulApplicationComposition:
    runtime: SoulApplicationRuntimeGateway
    archive: SQLiteApplicationArchive


@dataclass(frozen=True, slots=True)
class _LifecycleEvidence:
    instance: Any
    action: str
    created_at: str
    sequence: int


@dataclass(frozen=True, slots=True)
class _LifecycleDecision:
    requested_state: str
    desired_state: str | None
    source_instance_id: str | None
    transition_ref: str


def load_date_as_yun_persona() -> DateAsYunPersona:
    """Load the reviewed, project-versioned runtime profile with hash pinning."""

    source_path = Path(__file__).resolve().parent / "resources" / _DATE_AS_YUN_RESOURCE
    try:
        raw = source_path.read_bytes()
    except OSError:
        raise RuntimeError("date_as_yun_persona_resource_unavailable") from None
    digest = hashlib.sha256(raw).hexdigest()
    if digest != DATE_AS_YUN_PERSONA_SHA256:
        raise RuntimeError("date_as_yun_persona_resource_mismatch")
    try:
        prompt = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError("date_as_yun_persona_resource_invalid") from None
    if not prompt.strip():
        raise RuntimeError("date_as_yun_persona_resource_invalid")
    return DateAsYunPersona(prompt, DATE_AS_YUN_PERSONA_VERSION, source_path)


def compose_soul_application_runtime(
    settings: Settings,
    cloud_configuration: CloudChatConfiguration | Any,
    *,
    owner_client: SoulOwnerClient | Any | None = None,
    vision: Any | None = None,
    runtime_factory: Callable[..., Any] = ApplicationRuntime,
    activation_retry_delays: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0),
    lifecycle_poll_seconds: float = 5.0,
    lifecycle_retry_delays: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0),
) -> SoulApplicationComposition:
    """Compose production Soul ports without contacting owner, model, or device."""

    application_database = settings.data_dir / APPLICATION_DATABASE_FILENAME
    learning_database = settings.data_dir / REPLY_LEARNING_DATABASE_FILENAME
    lifecycle_database = (
        settings.data_dir / SCHEDULER_LIFECYCLE_DATABASE_FILENAME
    )
    archive = SQLiteApplicationArchive(application_database)
    learning = ReplyLearningStore(learning_database)
    lifecycle_store = SQLiteSoulSchedulerLifecycleStore(lifecycle_database)

    setup_error: str | None = None
    try:
        persona = load_date_as_yun_persona()
    except RuntimeError:
        persona = None
        setup_error = "persona_unavailable"

    resolved_owner = owner_client
    if resolved_owner is None:
        try:
            resolved_owner = SoulOwnerClient(
                settings.soul_console_url,
                timeout_seconds=settings.soul_request_timeout_seconds,
                observation_timeout_seconds=settings.soul_observation_timeout_seconds,
            )
        except (SoulApplicationError, ValueError):
            setup_error = setup_error or "owner_configuration_invalid"

    resolved_vision = vision
    if resolved_vision is None:
        try:
            endpoint = _loopback_chat_completions_endpoint(
                settings.local_chat_endpoint or ""
            )
            model = (settings.local_chat_model or "").strip()
            if not model:
                raise ValueError("model_not_configured")
            resolved_vision = LoopbackSoulVisionClient(
                endpoint=endpoint,
                model=model,
                api_key=settings.local_chat_api_key,
                timeout_seconds=settings.chat_request_timeout_seconds,
            )
        except (GuiOwlClientError, ValueError):
            setup_error = setup_error or "local_vision_not_configured"

    resolver = cloud_configuration.resolve_provider
    ports = None
    if persona is not None and resolved_owner is not None and resolved_vision is not None:
        ports = build_soul_application_ports(
            owner_client=resolved_owner,
            vision=resolved_vision,
            cloud_provider_resolver=resolver,
            learning=learning,
            persona_prompt=persona.prompt,
            persona_version=persona.version,
        )

    def dependency_probe() -> None:
        if setup_error is not None or ports is None or resolved_owner is None:
            raise SoulApplicationUnavailable(setup_error or "composition_unavailable")
        try:
            provider = resolver()
        except Exception:
            raise SoulApplicationUnavailable("cloud_configuration_unavailable") from None
        if provider is None:
            raise SoulApplicationUnavailable("cloud_provider_not_configured")
        try:
            capabilities = resolved_owner.capabilities()
        except Exception:
            raise SoulApplicationUnavailable("owner_unavailable") from None
        if not _owner_capabilities_ready(capabilities):
            raise SoulApplicationUnavailable("owner_capabilities_incompatible")

    def build_runtime():
        if ports is None:
            raise SoulApplicationUnavailable("composition_unavailable")
        return runtime_factory(
            application_database,
            profile=PROFILE_ID,
            memory_scope=PROFILE_ID,
            observation_port=ports.observation_port,
            policy=ports.policy,
            execution_owner=ports.execution_owner,
            verifier=ports.verifier,
            memory_gate=ports.memory_gate,
            persistence_projection=ports.persistence_projection,
        )

    gateway = SoulApplicationRuntimeGateway(
        archive=archive,
        dependency_probe=dependency_probe,
        runtime_factory=build_runtime if ports is not None else None,
        scheduler_reader=(
            resolved_owner.scheduler if resolved_owner is not None else None
        ),
        scheduler_controller=(
            resolved_owner.set_scheduler_state
            if resolved_owner is not None
            else None
        ),
        scheduler_lifecycle_store=lifecycle_store,
        activation_retry_delays=activation_retry_delays,
        lifecycle_poll_seconds=lifecycle_poll_seconds,
        lifecycle_retry_delays=lifecycle_retry_delays,
        activation_shutdown_timeout_seconds=(
            settings.soul_request_timeout_seconds + 5.0
        ),
        runtime_shutdown_timeout_seconds=max(
            5.0,
            (2.0 * settings.chat_request_timeout_seconds)
            + (4.0 * settings.soul_request_timeout_seconds)
            + 5.0,
        ),
    )
    return SoulApplicationComposition(gateway, archive)


def _validate_scheduler_control(
    payload: Mapping[str, Any] | Any,
    desired_state: str,
) -> None:
    if not isinstance(payload, Mapping):
        raise SoulApplicationError("soul_scheduler_invalid_response")
    effective_state = payload.get("effective_state")
    controller_ref = payload.get("controller_ref")
    valid_effective = {
        "running": {"running"},
        "paused": {"paused", "stopped"},
        "stopped": {"stopping", "stopped"},
    }[desired_state]
    if (
        payload.get("contract_version") != "v1"
        or payload.get("desired_state") != desired_state
        or effective_state not in valid_effective
        or (
            desired_state in {"running", "paused"}
            and controller_ref != SCHEDULER_CONTROLLER_REF
        )
    ):
        raise SoulApplicationError("soul_scheduler_state_mismatch")


def _scheduler_matches(
    payload: Mapping[str, Any] | Any,
    desired_state: str,
) -> bool:
    # Parsing the public projection first validates every bounded owner field.
    _scheduler_status_projection(payload)
    controller_matches = (
        payload.get("controller_ref") == SCHEDULER_CONTROLLER_REF
    )
    if desired_state == "running":
        return (
            payload.get("desired_state") == "running"
            and payload.get("effective_state") == "running"
            and controller_matches
            and payload.get("reply_owner") == "application_runtime"
            and payload.get("scheduler_mode") == "match"
        )
    if desired_state == "paused":
        return (
            payload.get("desired_state") == "paused"
            and payload.get("effective_state") in {"paused", "stopped"}
            and controller_matches
        )
    return (
        payload.get("desired_state") == "stopped"
        and payload.get("effective_state") in {"stopping", "stopped"}
    )


def _scheduler_status_projection(
    payload: Mapping[str, Any] | Any,
) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise SoulApplicationError("soul_scheduler_invalid_response")
    desired = payload.get("desired_state")
    effective = payload.get("effective_state")
    reply_owner = payload.get("reply_owner")
    scheduler_mode = payload.get("scheduler_mode")
    controller_matches = (
        payload.get("controller_ref") == SCHEDULER_CONTROLLER_REF
    )
    if (
        payload.get("contract_version") != "v1"
        or desired not in {"running", "paused", "stopped"}
        or effective not in {"running", "paused", "stopping", "stopped"}
        or reply_owner not in {"application_runtime", "none"}
        or scheduler_mode not in {"match", None}
    ):
        raise SoulApplicationError("soul_scheduler_invalid_response")

    if desired == "stopped" and effective == "stopped":
        state = "stopped"
        code = "scheduler_stopped"
    elif desired == "stopped" and effective == "stopping":
        state = "degraded"
        code = "scheduler_stopping"
    elif desired == "paused" and effective == "stopped" and controller_matches:
        state = "paused"
        code = "scheduler_paused_cold"
    elif not controller_matches:
        state = "degraded"
        code = "scheduler_controller_mismatch"
    elif reply_owner != "application_runtime":
        state = "degraded"
        code = "scheduler_reply_owner_mismatch"
    elif scheduler_mode != "match":
        state = "degraded"
        code = "scheduler_mode_mismatch"
    elif desired == "running" and effective == "running":
        state = "running"
        code = "scheduler_running"
    elif desired == "paused" and effective == "paused":
        state = "paused"
        code = "scheduler_paused"
    else:
        state = "degraded"
        code = "scheduler_state_mismatch"

    return {
        "profile_id": PROFILE_ID,
        "state": state,
        "desired_state": desired,
        "effective_state": effective,
        "controller_matches": controller_matches,
        "code": code,
        "observed_at": _utc_now(),
    }


def _aggregate_lifecycle_decision(
    instances: list[Any],
    current: SoulSchedulerLifecycleRecord | None,
) -> _LifecycleDecision | None:
    """Reduce every instance into the one shared matcher target.

    Current safe-running work always wins over pause.  A stopping instance
    holds the target it needed immediately before Stop until the core proves
    the stop idle/settled.  With no nonterminal instance, the newest explicit
    lifecycle event remains authoritative even if its reply instance later
    failed or completed; terminal failure must never resurrect an older Stop.
    """

    evidences = _lifecycle_evidences(instances)
    by_instance: dict[str, list[_LifecycleEvidence]] = {}
    for evidence in evidences:
        by_instance.setdefault(_instance_id(evidence.instance), []).append(evidence)

    nonterminal = [
        instance
        for instance in instances
        if _state_value(instance, "status")
        in {"queued", "running", "waiting", "paused", "stopping"}
    ]
    requested_state: str
    desired_state: str | None
    source: _LifecycleEvidence | None = None
    if nonterminal:
        targets: list[tuple[str | None, _LifecycleEvidence | None, str]] = []
        for instance in nonterminal:
            instance_id = _instance_id(instance)
            status = str(_state_value(instance, "status") or "")
            instance_evidence = by_instance.get(instance_id, [])
            if status in {"queued", "running", "waiting"}:
                targets.append(("running", _latest(instance_evidence), status))
            elif status == "paused":
                targets.append(("paused", _latest(instance_evidence), status))
            else:
                prior_target, prior_evidence = _target_before_stop(
                    instance_evidence
                )
                if prior_target is None and current is not None:
                    prior_target = current.desired_state
                targets.append((prior_target, prior_evidence, status))

        chosen_target = "running" if any(
            target == "running" for target, _evidence, _status in targets
        ) else (
            "paused"
            if any(target == "paused" for target, _evidence, _status in targets)
            else None
        )
        matching = [
            evidence
            for target, evidence, _status in targets
            if target == chosen_target and evidence is not None
        ]
        source = _latest(matching)
        all_stopping = all(
            status == "stopping" for _target, _evidence, status in targets
        )
        requested_state = "stopped" if all_stopping else (chosen_target or "stopped")
        desired_state = chosen_target
        if desired_state is None and current is not None:
            desired_state = current.desired_state
    else:
        source = next(
            (
                evidence
                for evidence in reversed(evidences)
                if not (
                    evidence.action == "Stop"
                    and _state_value(evidence.instance, "status") != "stopped"
                )
            ),
            None,
        )
        if source is None:
            return None
        if source.action in {"Start", "Resume"}:
            requested_state = desired_state = "running"
        elif source.action == "Pause":
            requested_state = desired_state = "paused"
        elif (
            source.action == "Stop"
            and _state_value(source.instance, "status") == "stopped"
        ):
            requested_state = desired_state = "stopped"
        else:
            return (
                _decision_from_current(current, instances)
                if current is not None
                else None
            )

    source_instance_id = _instance_id(source.instance) if source else (
        current.source_instance_id if current is not None else None
    )
    return _LifecycleDecision(
        requested_state=requested_state,
        desired_state=desired_state,
        source_instance_id=source_instance_id or None,
        transition_ref=_lifecycle_transition_ref(
            instances,
            requested_state,
            desired_state,
        ),
    )


def _lifecycle_evidences(instances: list[Any]) -> list[_LifecycleEvidence]:
    candidates: list[tuple[tuple[str, str, int, str], _LifecycleEvidence]] = []
    for instance in instances:
        instance_id = _instance_id(instance)
        instance_created_at = str(
            _state_value(instance, "created_at") or ""
        )
        for event in _state_value(instance, "events") or ():
            event_type = _state_value(event, "event_type")
            action: str | None = None
            if event_type == "started":
                action = "Start"
            elif event_type == "command_accepted":
                data = _state_value(event, "data")
                tag = data.get("tag") if isinstance(data, Mapping) else None
                if tag in {"Pause", "Resume", "Stop"}:
                    action = str(tag)
            if action is None:
                continue
            created_at = str(_state_value(event, "created_at") or "")
            raw_sequence = _state_value(event, "sequence")
            sequence = int(raw_sequence) if isinstance(raw_sequence, int) else 0
            evidence = _LifecycleEvidence(
                instance,
                action,
                created_at,
                sequence,
            )
            candidates.append(
                (
                    (created_at, instance_created_at, sequence, instance_id),
                    evidence,
                )
            )
    return [item[1] for item in sorted(candidates, key=lambda item: item[0])]


def _fallback_lifecycle_decision(
    state: Any,
    current: SoulSchedulerLifecycleRecord | None,
) -> _LifecycleDecision:
    status = str(_state_value(state, "status") or "")
    if status in {"queued", "running", "waiting"}:
        requested_state = desired_state = "running"
    elif status == "paused":
        requested_state = desired_state = "paused"
    elif status == "stopped":
        requested_state = desired_state = "stopped"
    else:
        requested_state = "stopped"
        desired_state = current.desired_state if current is not None else None
    source_instance_id = _instance_id(state) or None
    transition_ref = hashlib.sha256(
        (
            f"fallback:{source_instance_id or 'unbound'}:{status}:"
            f"{_state_value(state, 'updated_at') or ''}:"
            f"{_state_value(state, 'revision') or 0}:"
            f"{requested_state}:{desired_state or 'unknown'}"
        ).encode("utf-8")
    ).hexdigest()
    return _LifecycleDecision(
        requested_state,
        desired_state,
        source_instance_id,
        transition_ref,
    )


def _decision_from_current(
    current: SoulSchedulerLifecycleRecord,
    instances: list[Any],
) -> _LifecycleDecision:
    return _LifecycleDecision(
        current.requested_state,
        current.desired_state,
        current.source_instance_id,
        _lifecycle_transition_ref(
            instances,
            current.requested_state,
            current.desired_state,
        ),
    )


def _target_before_stop(
    evidences: list[_LifecycleEvidence],
) -> tuple[str | None, _LifecycleEvidence | None]:
    target: str | None = None
    source: _LifecycleEvidence | None = None
    for evidence in evidences:
        if evidence.action in {"Start", "Resume"}:
            target = "running"
            source = evidence
        elif evidence.action == "Pause":
            target = "paused"
            source = evidence
    return target, source


def _latest(evidences: list[_LifecycleEvidence]) -> _LifecycleEvidence | None:
    return (
        max(
            evidences,
            key=lambda evidence: (
                evidence.created_at,
                str(_state_value(evidence.instance, "created_at") or ""),
                evidence.sequence,
                _instance_id(evidence.instance),
            ),
        )
        if evidences
        else None
    )


def _lifecycle_transition_ref(
    instances: list[Any],
    requested_state: str,
    desired_state: str | None,
) -> str:
    material = [f"decision:{requested_state}:{desired_state or 'unknown'}"]
    for instance in sorted(instances, key=_instance_id):
        instance_id = _instance_id(instance)
        material.append(
            f"instance:{instance_id}:{_state_value(instance, 'status') or ''}"
        )
        for evidence in _lifecycle_evidences([instance]):
            material.append(
                f"event:{instance_id}:{evidence.action}:"
                f"{evidence.created_at}:{evidence.sequence}"
            )
    return hashlib.sha256("\n".join(material).encode("utf-8")).hexdigest()


def _instance_id(instance: Any) -> str:
    return str(
        _state_value(instance, "instance_id")
        or _state_value(instance, "id")
        or ""
    )


def _state_value(state: Any, name: str) -> Any:
    if isinstance(state, Mapping):
        return state.get(name)
    return getattr(state, name, None)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _owner_capabilities_ready(payload: Mapping[str, Any] | Any) -> bool:
    if not isinstance(payload, Mapping):
        return False
    capabilities = payload.get("capabilities")
    if (
        payload.get("contract_version") != "v1"
        or payload.get("service") != "soul_execution_owner"
        or not isinstance(capabilities, Mapping)
    ):
        return False
    return all(
        capabilities.get(name) is True
        for name in (
            "observations",
            "intent_reserve",
            "intent_dispatch",
            "intent_inspect",
            "owner_captured_observations",
            "loopback_only",
        )
    )


_RETRYABLE_ACTIVATION_REASONS = frozenset(
    {
        "cloud_configuration_unavailable",
        "cloud_provider_not_configured",
        "owner_unavailable",
        "owner_capabilities_incompatible",
        "runtime_initialization_failed",
    }
)
