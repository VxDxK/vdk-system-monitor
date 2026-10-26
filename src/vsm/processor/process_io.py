from vsm.processor.pid_stat import PidStatProcessor


class ProcessIoProcessor(PidStatProcessor):
    """Compute process/thread byte and operation rates from accounting counters."""

    def __init__(self) -> None:
        super().__init__("process_io")
