class DadoError(Exception):
    """Expected user-facing DADO error."""


class ValidationError(DadoError):
    pass


class ConflictError(DadoError):
    pass
