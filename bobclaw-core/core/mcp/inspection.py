"""The same ten reads over the authenticated gateway; never a chat/model call."""
import inspect
from core.tools.inspection import INSPECTION_TOOLS


def register(mcp, proxy, face_allowed):
    if not any(face_allowed(face) for face in ('assistant','planner')):
        return

    def bind(tool):
        async def invoke(**kwargs):
            return await proxy('POST','/bob-inspection/'+tool.name,json_body=kwargs,timeout=100)
        invoke.__name__ = tool.name
        invoke.__doc__ = tool.description
        invoke.__signature__ = inspect.signature(tool.coroutine)
        invoke.__annotations__ = tool.coroutine.__annotations__
        return invoke

    for tool in INSPECTION_TOOLS:
        mcp.tool(name=tool.name)(bind(tool))
