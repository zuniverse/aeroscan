class ApiError(Exception):
    """Application error carrying its own HTTP status and code.

    One exception type rather than a hierarchy: the handler in main.py
    turns any instance into the documented {error_code, message,
    details} envelope, so adding a new error is adding a raise, not a
    class.
    """

    def __init__(
        self,
        status_code: int,
        error_code: str,
        message: str,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_code = error_code
        self.message = message
        self.details = details
