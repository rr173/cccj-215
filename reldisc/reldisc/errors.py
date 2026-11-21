"""Error taxonomy.

A *data error* (corrupt input, unknown column, duplicate row_id, bad value
type, ...) must never be silently reinterpreted as a failed inclusion
relationship: it aborts the command with a dedicated exit code instead.
"""


class ReldiscError(Exception):
    exit_code = 1
    label = "error"


class UsageError(ReldiscError):
    """Bad command line arguments, illegal mapping, unprepared workdir..."""
    exit_code = 2
    label = "usage error"


class DataError(ReldiscError):
    """The input data itself is corrupt or violates the declared schema."""
    exit_code = 3
    label = "data error"


class InternalError(ReldiscError):
    """Two independent evaluation paths disagreed; this is a bug."""
    exit_code = 1
    label = "internal error"
