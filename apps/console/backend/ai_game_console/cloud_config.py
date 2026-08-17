from __future__ import annotations

import ctypes
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from time import perf_counter
from typing import Protocol
from urllib.parse import urlparse

from .chat import ChatProvider, ChatProviderError, ProviderMessage
from .config import Settings
from .domain import RuntimeCapability
from .openai_chat import OpenAIChatProvider
from .repository import ConcurrentUpdate, SQLiteRepository


class CloudConfigError(RuntimeError):
    """Stable, secret-free configuration failure exposed by the local API."""

    def __init__(self, *, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code

    def as_payload(self) -> dict[str, dict[str, str]]:
        return {"error": {"code": self.code, "message": self.message}}


class SecretProtector(Protocol):
    def protect(self, value: str) -> bytes: ...

    def unprotect(self, value: bytes) -> str: ...


class DpapiSecretProtector:
    """Protect secrets for the current Windows user with DPAPI.

    There is deliberately no plaintext fallback. The Windows console is the
    production owner of cloud credentials; unsupported hosts must inject a
    test/development protector explicitly.
    """

    _DESCRIPTION = "AI-GAME cloud chat API key"
    _CRYPTPROTECT_UI_FORBIDDEN = 0x01

    def protect(self, value: str) -> bytes:
        if os.name != "nt":
            raise CloudConfigError(
                code="cloud_secret_storage_unavailable",
                message="当前系统不支持本机受保护的密钥存储。",
                status_code=503,
            )
        return self._crypt(value.encode("utf-8"), protect=True)

    def unprotect(self, value: bytes) -> str:
        if os.name != "nt":
            raise CloudConfigError(
                code="cloud_secret_storage_unavailable",
                message="当前系统不支持本机受保护的密钥存储。",
                status_code=503,
            )
        try:
            return self._crypt(bytes(value), protect=False).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CloudConfigError(
                code="cloud_secret_unavailable",
                message="无法读取已保存的云端密钥，请重新保存配置。",
                status_code=409,
            ) from exc

    @classmethod
    def _crypt(cls, value: bytes, *, protect: bool) -> bytes:
        from ctypes import wintypes

        class DataBlob(ctypes.Structure):
            _fields_ = [
                ("cbData", wintypes.DWORD),
                ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
            ]

        input_buffer = ctypes.create_string_buffer(value)
        input_blob = DataBlob(
            len(value),
            ctypes.cast(input_buffer, ctypes.POINTER(ctypes.c_ubyte)),
        )
        output_blob = DataBlob()
        crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
        kernel32.LocalFree.restype = wintypes.HLOCAL

        try:
            if protect:
                operation = crypt32.CryptProtectData
                succeeded = operation(
                    ctypes.byref(input_blob),
                    cls._DESCRIPTION,
                    None,
                    None,
                    None,
                    cls._CRYPTPROTECT_UI_FORBIDDEN,
                    ctypes.byref(output_blob),
                )
            else:
                operation = crypt32.CryptUnprotectData
                succeeded = operation(
                    ctypes.byref(input_blob),
                    None,
                    None,
                    None,
                    None,
                    cls._CRYPTPROTECT_UI_FORBIDDEN,
                    ctypes.byref(output_blob),
                )
            if not succeeded:
                raise CloudConfigError(
                    code=(
                        "cloud_secret_storage_failed"
                        if protect
                        else "cloud_secret_unavailable"
                    ),
                    message=(
                        "无法保存云端密钥。"
                        if protect
                        else "无法读取已保存的云端密钥，请重新保存配置。"
                    ),
                    status_code=503 if protect else 409,
                )
            return ctypes.string_at(output_blob.pbData, output_blob.cbData)
        finally:
            if output_blob.pbData:
                kernel32.LocalFree(
                    ctypes.cast(output_blob.pbData, wintypes.HLOCAL)
                )


@dataclass(frozen=True, slots=True)
class CloudChatSnapshot:
    endpoint: str | None
    model: str | None
    api_key: str | None = field(default=None, repr=False)
    revision: int = 0
    updated_at: str | None = None
    credential_source: str = "none"

    @property
    def configured(self) -> bool:
        return bool(self.endpoint and self.model and self.api_key)


@dataclass(frozen=True, slots=True)
class CloudConnectionTestResult:
    ok: bool
    status: str
    detail: str
    latency_ms: int | None = None


ProviderFactory = Callable[[CloudChatSnapshot], ChatProvider]


class CloudChatConfiguration:
    """Single runtime owner for cloud config, provider generations and status."""

    def __init__(
        self,
        repository: SQLiteRepository,
        settings: Settings,
        *,
        protector: SecretProtector | None = None,
        provider_factory: ProviderFactory | None = None,
    ) -> None:
        self.repository = repository
        self.settings = settings
        self.protector = protector or DpapiSecretProtector()
        self.provider_factory = provider_factory or self._default_provider_factory
        self._lock = threading.RLock()
        self._started = False
        self._snapshot = CloudChatSnapshot(None, None)
        self._provider: ChatProvider | None = None
        self._status = "not_configured"
        self._detail = "云端端点、模型或 API key 尚未完整配置。"

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            persisted = self.repository.get_cloud_chat_config()
            if persisted is None:
                snapshot = CloudChatSnapshot(
                    endpoint=self.settings.cloud_chat_endpoint,
                    model=self.settings.cloud_chat_model,
                    api_key=self.settings.cloud_chat_api_key,
                    revision=0,
                    updated_at=None,
                    credential_source=(
                        "startup"
                        if self.settings.cloud_chat_api_key
                        else "none"
                    ),
                )
                self._install_snapshot(snapshot)
            elif persisted["endpoint"] is None:
                self._install_snapshot(
                    CloudChatSnapshot(
                        None,
                        None,
                        revision=persisted["revision"],
                        updated_at=persisted["updated_at"],
                    )
                )
            else:
                try:
                    api_key = self.protector.unprotect(
                        persisted["api_key_protected"]
                    )
                    snapshot = CloudChatSnapshot(
                        endpoint=persisted["endpoint"],
                        model=persisted["model"],
                        api_key=api_key,
                        revision=persisted["revision"],
                        updated_at=persisted["updated_at"],
                        credential_source="console",
                    )
                    self._install_snapshot(snapshot)
                except CloudConfigError:
                    self._snapshot = CloudChatSnapshot(
                        endpoint=persisted["endpoint"],
                        model=persisted["model"],
                        revision=persisted["revision"],
                        updated_at=persisted["updated_at"],
                        credential_source="console",
                    )
                    self._provider = None
                    self._status = "error"
                    self._detail = "无法读取已保存的 API key，请重新保存配置。"
            self._started = True

    def public_view(self) -> dict[str, object]:
        with self._lock:
            snapshot = self._snapshot
            return {
                "endpoint": snapshot.endpoint,
                "model": snapshot.model,
                "has_api_key": snapshot.api_key is not None,
                "configured": snapshot.configured and self._provider is not None,
                "credential_source": snapshot.credential_source,
                "status": self._status,
                "detail": self._detail,
                "revision": snapshot.revision,
                "updated_at": snapshot.updated_at,
            }

    def configure(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str | None,
        expected_revision: int,
    ) -> dict[str, object]:
        normalized_endpoint = _normalize_endpoint(endpoint)
        normalized_model = model.strip()
        if not normalized_model:
            raise CloudConfigError(
                code="invalid_cloud_chat_config",
                message="模型名称不能为空。",
            )
        with self._lock:
            current = self._snapshot
            next_key = api_key.strip() if api_key is not None else current.api_key
            if not next_key:
                raise CloudConfigError(
                    code="cloud_api_key_required",
                    message="首次配置时需要填写 API key。",
                )
            candidate = CloudChatSnapshot(
                endpoint=normalized_endpoint,
                model=normalized_model,
                api_key=next_key,
                revision=current.revision + 1,
                credential_source="console",
            )
            protected = self.protector.protect(next_key)
            provider = self.provider_factory(candidate)
            try:
                persisted = self.repository.write_cloud_chat_config(
                    endpoint=normalized_endpoint,
                    model=normalized_model,
                    api_key_protected=protected,
                    expected_revision=expected_revision,
                )
            except ConcurrentUpdate as exc:
                raise CloudConfigError(
                    code="cloud_config_changed",
                    message="云端配置已在其他页面更新，请刷新后重试。",
                    status_code=409,
                ) from exc
            self._snapshot = CloudChatSnapshot(
                endpoint=normalized_endpoint,
                model=normalized_model,
                api_key=next_key,
                revision=persisted["revision"],
                updated_at=persisted["updated_at"],
                credential_source="console",
            )
            self._provider = provider
            self._status = "unknown"
            self._detail = "配置已保存；首次发送或连接测试时验证。"
            return self.public_view()

    def clear(self, *, expected_revision: int) -> dict[str, object]:
        with self._lock:
            try:
                persisted = self.repository.clear_cloud_chat_config(
                    expected_revision=expected_revision
                )
            except ConcurrentUpdate as exc:
                raise CloudConfigError(
                    code="cloud_config_changed",
                    message="云端配置已在其他页面更新，请刷新后重试。",
                    status_code=409,
                ) from exc
            self._snapshot = CloudChatSnapshot(
                None,
                None,
                revision=persisted["revision"],
                updated_at=persisted["updated_at"],
            )
            self._provider = None
            self._status = "not_configured"
            self._detail = "云端端点、模型或 API key 尚未完整配置。"
            return self.public_view()

    def resolve_provider(self) -> ChatProvider | None:
        with self._lock:
            return self._provider

    def runtime_capability(self) -> RuntimeCapability:
        with self._lock:
            configured = self._snapshot.configured and self._provider is not None
            return RuntimeCapability(
                id="planner",
                name="云端规划器",
                status=self._status if configured else (
                    "error" if self._status == "error" else "not_configured"
                ),
                configured=configured,
                detail=self._detail,
                blocker=(
                    None
                    if configured and self._status in {"unknown", "ready"}
                    else {
                        "code": (
                            "cloud_planner_error"
                            if self._status == "error"
                            else "cloud_planner_not_configured"
                        ),
                        "message": (
                            "云端规划器连接失败。"
                            if self._status == "error"
                            else "云端规划器尚未配置。"
                        ),
                    }
                ),
            )

    def test_connection(self) -> CloudConnectionTestResult:
        with self._lock:
            provider = self._provider
        if provider is None:
            raise CloudConfigError(
                code="cloud_model_not_configured",
                message="请先保存完整的云端模型配置。",
                status_code=409,
            )
        started_at = perf_counter()
        try:
            provider.complete(
                (
                    ProviderMessage(
                        role="system",
                        content=(
                            "只返回 JSON：{\"assistant_text\":\"ok\","
                            "\"execution_goal\":null}"
                        ),
                    ),
                    ProviderMessage(role="user", content="连接测试"),
                ),
                json_response=True,
                is_cancelled=lambda: False,
            )
        except ChatProviderError as exc:
            with self._lock:
                if provider is self._provider:
                    self._status = "error"
                    self._detail = exc.public_message
            return CloudConnectionTestResult(False, "error", exc.public_message)
        latency_ms = max(0, round((perf_counter() - started_at) * 1000))
        with self._lock:
            if provider is self._provider:
                self._status = "ready"
                self._detail = "云端模型连接与规划响应格式验证通过。"
        return CloudConnectionTestResult(
            True,
            "ready",
            "云端模型连接与规划响应格式验证通过。",
            latency_ms,
        )

    def _install_snapshot(self, snapshot: CloudChatSnapshot) -> None:
        self._snapshot = snapshot
        if snapshot.configured:
            try:
                self._provider = self.provider_factory(snapshot)
            except Exception:
                self._provider = None
                self._status = "error"
                self._detail = "云端模型配置无法加载，请重新保存。"
                return
            self._status = "unknown"
            self._detail = "配置已加载；首次发送时验证连接。"
        else:
            self._provider = None
            self._status = "not_configured"
            self._detail = "云端端点、模型或 API key 尚未完整配置。"

    def _default_provider_factory(self, snapshot: CloudChatSnapshot) -> ChatProvider:
        if not snapshot.endpoint or not snapshot.model or not snapshot.api_key:
            raise CloudConfigError(
                code="cloud_model_not_configured",
                message="云端模型配置尚未完成。",
            )
        return OpenAIChatProvider(
            endpoint=snapshot.endpoint,
            model=snapshot.model,
            provider_name="cloud_openai_compatible",
            api_key=snapshot.api_key,
            timeout_seconds=self.settings.chat_request_timeout_seconds,
        )


def _normalize_endpoint(value: str) -> str:
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise CloudConfigError(
            code="invalid_cloud_chat_config",
            message="云端服务地址必须是有效的 HTTP 或 HTTPS 地址。",
        )
    return normalized
