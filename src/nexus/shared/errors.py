class NexusError(Exception):
    """An error safe to identify by type, without exposing dependency payloads."""


class StorageUnavailable(NexusError):
    pass


class BootstrapRejected(NexusError):
    pass
