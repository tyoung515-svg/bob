import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from core.mcp.server import build_server,MCPConfig
@pytest.mark.asyncio
async def test_central_memory_mcp_roundtrip():
 hits=[]
 async def accept(request):
  hits.append((request.path,request.headers['Authorization'],await request.json()));return web.json_response({'authority':'server'})
 app=web.Application();app.router.add_post('/agent-memory/search',accept)
 server=TestServer(app);await server.start_server()
 try:
  tools={t.name:t for t in build_server(MCPConfig('fixture',str(server.make_url('/')).rstrip('/'),['assistant']))._tool_manager.list_tools()}
  assert {'bob_memory_status','bob_memory_search','bob_memory_read','bob_memory_save'}<=tools.keys()
  await tools['bob_memory_search'].run({'query':'marker'})
  assert hits==[('/agent-memory/search','Bearer fixture',{'query':'marker','limit':10,'offset':0})]
 finally:await server.close()
