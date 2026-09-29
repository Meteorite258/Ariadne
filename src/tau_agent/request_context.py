"""Optional request projection; routing and tool authority stay with the harness."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from tau_agent.messages import AgentMessage


@dataclass(frozen=True, slots=True)
class RequestContext:
    system: str
    messages: tuple[AgentMessage, ...]


RequestContextHook = Callable[[RequestContext], Awaitable[RequestContext]]
