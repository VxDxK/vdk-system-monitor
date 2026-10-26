"""Built-in connection discovery; importing does not establish connections."""

from vsm.connection.host import Host
from vsm.connection.ssh import SSH

CONNECTION_TYPES: dict[str, type[object]] = {"host": Host, "ssh": SSH}
