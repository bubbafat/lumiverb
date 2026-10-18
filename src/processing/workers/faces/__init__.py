"""Face detection (InsightFace): the provider, and what a machine doing faces says when it can't."""


class FaceError(Exception):
    """A machine couldn't look for faces in an image (ADR-022).

    endpoint_fault: the machine was at fault (unreachable, refused the key,
    the model gone, out of memory), not the image: the work goes to another
    machine, and no clip is charged when none is left. Otherwise the image's
    (one it can't read)."""

    def __init__(self, message: str, *, endpoint_fault: bool) -> None:
        super().__init__(message)
        self.endpoint_fault = endpoint_fault
