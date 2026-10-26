from dataclasses import replace
from pathlib import PurePosixPath

from vsm.models import LogRef
from vsm.processing import Dataset, Params
from vsm.processor._parsing import Event, text
from vsm.processor._scalar import ScalarProcessor
from vsm.util.records import FileRecord


class TemperatureProcessor(ScalarProcessor):
    """Convert thermal readings from millidegrees to degrees Celsius."""

    name = "temperature"
    chart_title = "Temperature"
    group_label = "Hardware"
    column = "temperature_celsius"
    scale = 0.001

    unit = "°C"

    def parse_event(self, event: Event, params: Params) -> list[dict[str, object]]:
        if event["type"] == "file" and PurePosixPath(str(event["path"])).name == "type":
            return []
        return super().parse_event(event, params)

    def transform(
        self, source: LogRef[FileRecord], params: Params, es: list[Event]
    ) -> tuple[Dataset, ...]:
        names = {
            str(PurePosixPath(str(e["path"])).parent): text(e).strip()
            for e in es
            if e["type"] == "file" and PurePosixPath(str(e["path"])).name == "type"
        }
        results: list[Dataset] = []
        for data in super().transform(source, params, es):
            frame = data.frame.copy()
            if "path" in frame:
                frame["entity_label"] = [
                    f"{names[str(PurePosixPath(path).parent)]} ({PurePosixPath(path).parent.name})"
                    if str(PurePosixPath(path).parent) in names
                    else label
                    for path, label in zip(frame.path, frame.entity_label, strict=True)
                ]
            results.append(replace(data, frame=frame))
        return tuple(results)
