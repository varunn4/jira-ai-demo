"""Custom exceptions used by the Jira AI analysis application.

These exception classes make expected failure cases explicit, such as a
missing prompt template or an invalid LLM configuration.
"""

class PromptNotFoundError(FileNotFoundError):
    """Raised when a requested prompt template cannot be found."""


class LLMConfigurationError(RuntimeError):
    """Raised when the configured LLM provider cannot be used."""
