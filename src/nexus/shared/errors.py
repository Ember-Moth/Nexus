class NexusError(Exception):
    """An error safe to identify by type, without exposing dependency payloads."""


class StorageUnavailable(NexusError):
    pass


class BootstrapRejected(NexusError):
    pass


class ServiceError(NexusError):
    def __init__(
        self, code: str, message: str, status: int, *, retry_after: int | None = None
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.retry_after = retry_after
