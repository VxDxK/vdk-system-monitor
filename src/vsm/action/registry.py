from vsm.action.battery_level import BatteryLevel
from vsm.action.copy_file import CopyFile
from vsm.action.cpu_freq import CpuFreq
from vsm.action.cpu_freq_governor import CpuFreqGovernor
from vsm.action.cpu_time_in_state import CpuTimeInState
from vsm.action.ddr_freq import DdrFreq
from vsm.action.disk_io import DiskIo
from vsm.action.dmesg import Dmesg
from vsm.action.fans import Fans
from vsm.action.filesystem_usage import FilesystemUsage
from vsm.action.fio import Fio
from vsm.action.frame_count import FrameCount
from vsm.action.ftrace import Ftrace
from vsm.action.gpu_freq import GpuFreq
from vsm.action.iface_info import IfaceInfo
from vsm.action.iperf import Iperf
from vsm.action.launch import Launch
from vsm.action.load_average import LoadAverage
from vsm.action.manual import Manual
from vsm.action.meminfo import Meminfo
from vsm.action.net_dev import NetDev
from vsm.action.net_snmp import NetSnmp
from vsm.action.net_udp import NetUdp
from vsm.action.npu_freq import NpuFreq
from vsm.action.periodic_launcher import PeriodicLauncher
from vsm.action.pid_stat import PidStat
from vsm.action.ping import Ping
from vsm.action.poll_files import PollFiles
from vsm.action.proc_stat import ProcStat
from vsm.action.process_io import ProcessIo
from vsm.action.pull import Pull
from vsm.action.push import Push
from vsm.action.rup import Rup
from vsm.action.scan_processes import ScanProcesses
from vsm.action.set_file import SetFile
from vsm.action.shell import ShellAction
from vsm.action.simpleperf import Simpleperf
from vsm.action.sleep import Sleep
from vsm.action.smaps import Smaps
from vsm.action.socket_buffer_limit import SocketBufferLimit
from vsm.action.socket_stat import SocketStat
from vsm.action.ss import Ss
from vsm.action.strace import Strace
from vsm.action.swap import Swap
from vsm.action.tcpdump import Tcpdump
from vsm.action.temperature import Temperature
from vsm.action.top import Top
from vsm.action.traffic_generator import TrafficGenerator
from vsm.action.uptime import Uptime
from vsm.action.wifi_conn_info import WifiConnInfo

ACTION_TYPES: dict[str, type[object]] = {
    "launch": Launch,
    "periodic_launcher": PeriodicLauncher,
    "poll_files": PollFiles,
    "pull": Pull,
    "push": Push,
    "shell": ShellAction,
    "sleep": Sleep,
    "manual": Manual,
    "battery_level": BatteryLevel,
    "copy_file": CopyFile,
    "cpu_freq": CpuFreq,
    "cpu_freq_governor": CpuFreqGovernor,
    "cpu_time_in_state": CpuTimeInState,
    "ddr_freq": DdrFreq,
    "dmesg": Dmesg,
    "fio": Fio,
    "frame_count": FrameCount,
    "ftrace": Ftrace,
    "gpu_freq": GpuFreq,
    "iface_info": IfaceInfo,
    "iperf": Iperf,
    "meminfo": Meminfo,
    "net_dev": NetDev,
    "net_snmp": NetSnmp,
    "net_udp": NetUdp,
    "npu_freq": NpuFreq,
    "pid_stat": PidStat,
    "ping": Ping,
    "proc_stat": ProcStat,
    "rup": Rup,
    "scan_processes": ScanProcesses,
    "set_file": SetFile,
    "simpleperf": Simpleperf,
    "smaps": Smaps,
    "socket_buffer_limit": SocketBufferLimit,
    "socket_stat": SocketStat,
    "ss": Ss,
    "strace": Strace,
    "tcpdump": Tcpdump,
    "temperature": Temperature,
    "top": Top,
    "traffic_generator": TrafficGenerator,
    "wifi_conn_info": WifiConnInfo,
    "disk_io": DiskIo,
    "swap": Swap,
    "fans": Fans,
    "filesystem_usage": FilesystemUsage,
    "load_average": LoadAverage,
    "uptime": Uptime,
    "process_io": ProcessIo,
}
