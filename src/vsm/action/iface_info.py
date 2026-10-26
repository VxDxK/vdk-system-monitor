from __future__ import annotations

import json
from dataclasses import dataclass

from vsm.action.action import ActionContext
from vsm.models import Exec, InterfaceAddress


@dataclass(frozen=True, kw_only=True)
class IfaceInfo:
    """Discover target InterfaceAddress values through ip JSON output."""

    async def run(self, ctx: ActionContext) -> tuple[InterfaceAddress, ...]:
        result = await ctx.connection.run(
            Exec(argv=(str(await ctx.connection.executable("ip")), "-j", "address"))
        )
        return tuple(
            InterfaceAddress(interface=i["ifname"], address=a["local"], family=a["family"])
            for i in json.loads(result.stdout)
            for a in i.get("addr_info", [])
        )
