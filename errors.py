"""Application-specific exceptions and CLI exit codes."""

from __future__ import annotations


class ApplicationError(Exception):
    """Base class for an expected, user-facing application error."""

    exit_code = 1


class InvalidArgumentsError(ApplicationError):
    """Raised when a requested action or connector is unknown."""

    exit_code = 2


class InputDocumentError(ApplicationError):
    """Raised when an input document cannot be found or extracted."""

    exit_code = 3


class SkillFileError(ApplicationError):
    """Raised when a skill specification cannot be loaded."""

    exit_code = 4


class ConnectorConfigurationError(ApplicationError):
    """Raised when a connector is missing required configuration."""

    exit_code = 5


class ProviderError(ApplicationError):
    """Raised when an LLM provider rejects or cannot complete a request."""

    exit_code = 6


class OutputWriteError(ApplicationError):
    """Raised when the requested output file cannot be written."""

    exit_code = 7
