from vsm.processor._file import NativeFileProcessor
from vsm.processor._text import TextProcessor
from vsm.processor.battery_level import BatteryLevelProcessor
from vsm.processor.cpu_freq import CpuFreqProcessor
from vsm.processor.cpu_time_in_state import CpuTimeInStateProcessor
from vsm.processor.ddr_freq import DdrFreqProcessor
from vsm.processor.disk_io import DiskIoProcessor
from vsm.processor.fans import FansProcessor
from vsm.processor.filesystem_usage import FilesystemUsageProcessor
from vsm.processor.fio import FioProcessor
from vsm.processor.frame_count import FrameCountProcessor
from vsm.processor.gpu_freq import GpuFreqProcessor
from vsm.processor.iperf import IperfProcessor
from vsm.processor.load_average import LoadAverageProcessor
from vsm.processor.meminfo import MeminfoProcessor
from vsm.processor.net_dev import NetProcessor
from vsm.processor.net_snmp import NetSnmpProcessor
from vsm.processor.net_udp import NetUdpProcessor
from vsm.processor.npu_freq import NpuFreqProcessor
from vsm.processor.pid_stat import PidStatProcessor
from vsm.processor.ping import PingProcessor
from vsm.processor.proc_stat import CpuProcessor
from vsm.processor.process_io import ProcessIoProcessor
from vsm.processor.rup import RupProcessor
from vsm.processor.smaps import SmapsProcessor
from vsm.processor.socket_stat import SocketStatProcessor
from vsm.processor.ss import SsProcessor
from vsm.processor.swap import SwapProcessor
from vsm.processor.temperature import TemperatureProcessor
from vsm.processor.time import TimeProcessor
from vsm.processor.top import TopProcessor
from vsm.processor.traffic_generator import TrafficGeneratorProcessor
from vsm.processor.uptime import UptimeProcessor
from vsm.processor.wifi_conn_info import WifiConnInfoProcessor

FILE_NAMES = (
    "battery_level",
    "cpu_freq",
    "ddr_freq",
    "frame_count",
    "gpu_freq",
    "meminfo",
    "net_dev",
    "net_snmp",
    "net_udp",
    "npu_freq",
    "proc_stat",
    "smaps",
    "temperature",
    "disk_io",
    "swap",
    "fans",
    "filesystem_usage",
    "load_average",
    "uptime",
)
FILE_PROCESSORS = {
    "battery_level": BatteryLevelProcessor,
    "cpu_freq": CpuFreqProcessor,
    "ddr_freq": DdrFreqProcessor,
    "frame_count": FrameCountProcessor,
    "gpu_freq": GpuFreqProcessor,
    "meminfo": MeminfoProcessor,
    "net_dev": NetProcessor,
    "net_snmp": NetSnmpProcessor,
    "net_udp": NetUdpProcessor,
    "npu_freq": NpuFreqProcessor,
    "proc_stat": CpuProcessor,
    "smaps": SmapsProcessor,
    "temperature": TemperatureProcessor,
    "disk_io": DiskIoProcessor,
    "swap": SwapProcessor,
    "fans": FansProcessor,
    "filesystem_usage": FilesystemUsageProcessor,
    "load_average": LoadAverageProcessor,
    "uptime": UptimeProcessor,
}
EXTERNAL_PROCESSORS = {
    "fio": FioProcessor,
    "iperf": IperfProcessor,
    "ping": PingProcessor,
    "ss": SsProcessor,
    "time": TimeProcessor,
    "top": TopProcessor,
    "wifi_conn_info": WifiConnInfoProcessor,
}


def file_processor(name: str) -> NativeFileProcessor:
    return FILE_PROCESSORS[name]()


def FileProcessor(name: str) -> NativeFileProcessor:
    return file_processor(name)


def ExternalProcessor(name: str) -> TextProcessor:
    return EXTERNAL_PROCESSORS[name]()


PROCESSORS: dict[str, object] = {
    "battery_level": BatteryLevelProcessor(),
    "cpu_freq": CpuFreqProcessor(),
    "ddr_freq": DdrFreqProcessor(),
    "frame_count": FrameCountProcessor(),
    "gpu_freq": GpuFreqProcessor(),
    "meminfo": MeminfoProcessor(),
    "net_dev": NetProcessor(),
    "net_snmp": NetSnmpProcessor(),
    "net_udp": NetUdpProcessor(),
    "npu_freq": NpuFreqProcessor(),
    "proc_stat": CpuProcessor(),
    "smaps": SmapsProcessor(),
    "temperature": TemperatureProcessor(),
    "disk_io": DiskIoProcessor(),
    "swap": SwapProcessor(),
    "fans": FansProcessor(),
    "filesystem_usage": FilesystemUsageProcessor(),
    "load_average": LoadAverageProcessor(),
    "uptime": UptimeProcessor(),
    "fio": FioProcessor(),
    "iperf": IperfProcessor(),
    "ping": PingProcessor(),
    "ss": SsProcessor(),
    "time": TimeProcessor(),
    "top": TopProcessor(),
    "wifi_conn_info": WifiConnInfoProcessor(),
    "pid_stat": PidStatProcessor(),
    "process_io": ProcessIoProcessor(),
    "socket_stat": SocketStatProcessor(),
    "traffic_generator": TrafficGeneratorProcessor(),
    "rup": RupProcessor(),
    "cpu_time_in_state": CpuTimeInStateProcessor(),
}
