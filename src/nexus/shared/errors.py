class NexusError(Exception):
    """An error safe to identify by type, without exposing dependency payloads."""


class StorageUnavailable(NexusError):
    pass


class BootstrapRejected(NexusError):
    pass


class ServiceError(NexusError):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
