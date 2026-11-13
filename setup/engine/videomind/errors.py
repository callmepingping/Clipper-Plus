class AppError(Exception):
    """A safe error which can cross the native protocol boundary."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable

    def to_dict(self):
        return {"code": self.code, "message": self.message, "retryable": self.retryable}
