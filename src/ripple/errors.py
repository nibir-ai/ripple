class RippleError(Exception):
    """An operational failure with a stable code and CLI exit status."""

    def __init__(self, code: str, message: str, exit_code: int = 3):
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code
