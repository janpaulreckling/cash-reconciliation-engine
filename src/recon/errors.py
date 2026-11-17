class ReconError(Exception):
    """Base class for all engine errors."""


class TransientError(ReconError):
    """Worth retrying: network blip, 5xx, rate limit."""


class PermanentError(ReconError):
    """Retrying will not help: bad credentials, invalid request, schema drift."""
