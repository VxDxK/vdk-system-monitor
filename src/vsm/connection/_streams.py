from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from vsm.models import CaptureOutput, FileOutput, Output, OutputLimitExceeded


class Sink:
    def __init__(self, output: Output | None):
        self.output = output or CaptureOutput()
        self.data = bytearray()
        self.file: BinaryIO | None = None
        if isinstance(self.output, FileOutput):
            self.output.path.parent.mkdir(parents=True, exist_ok=True)
            self.file = self.output.path.open("xb")

    def write(self, data: bytes) -> None:
        if isinstance(self.output, CaptureOutput):
            if len(self.data) + len(data) > self.output.limit_bytes:
                raise OutputLimitExceeded("subprocess capture limit exceeded")
            self.data.extend(data)
        elif self.file is not None:
            self.file.write(data)
            self.file.flush()

    def close(self) -> None:
        if self.file is not None:
            self.file.close()

    @property
    def path(self) -> Path | None:
        return self.output.path if isinstance(self.output, FileOutput) else None
