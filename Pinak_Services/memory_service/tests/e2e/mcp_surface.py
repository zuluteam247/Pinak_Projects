import datetime
import uuid
import asyncio,json,os,datetime,jwt
from pathlib import Path
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
server=str(Path(__file__).resolve().parents[2] / 'client' / 'pinak_memory_mcp.py')
base={**os.environ,'PINAK_API_URL':os.environ.get('PINAK_API_URL', 'http://127.0.0.1:18765/api/v1'),'PINAK_JWT_SECRET':os.environ['PINAK_JWT_SECRET'],'PINAK_CLIENT_ID':'mcp-fixture','PINAK_CLIENT_NAME':'mcp-fixture','PINAK_PROJECT_ID':'sandbox-project','PINAK_TENANT_ID':'sandbox-tenant','FASTMCP_DISABLE_VERSION_CHECK':'1'}
assert base['PINAK_API_URL'].startswith('http://127.0.0.1:') or base['PINAK_API_URL'].startswith('http://localhost:'), 'Refuse remote target'
results=[]
def mint(tenant='sandbox-tenant',project='sandbox-project',admin=False,read=True,write=True):
 now=datetime.datetime.now(datetime.timezone.utc)
 claims={'sub':'fixture-admin' if admin else 'fixture-agent','tenant':tenant,'project_id':project,'client_id':'mcp-fixture','client_name':'mcp-fixture','roles':['admin'] if admin else ['agent'],'scopes':(['memory.read'] if read else [])+(['memory.write'] if write else [])+(['memory.admin'] if admin else []),'iat':now,'exp':now+datetime.timedelta(minutes=10)}
 return jwt.encode({**claims, "iss": "pinak-memory", "aud": "pinak-memory-api", "jti": str(uuid.uuid4())},base['PINAK_JWT_SECRET'],algorithm='HS256')
async def session(label,token,ops):
 env={**base,'PINAK_JWT_TOKEN':token}
 async with Client(StdioTransport(os.environ.get('PINAK_MCP_PYTHON', 'python3'),[server],env=env,cwd=str(Path(__file__).resolve().parents[2]),keep_alive=False,log_file=Path(os.environ.get('PINAK_MCP_SERVER_LOG', '/tmp/pinak-mcp-server.log')))) as client:
  if label=='agent':
   names=[t.name for t in await client.list_tools()]
   print('TOOLS',len(names),names,flush=True)
  for name,args in ops:
   try:
    reply=await client.call_tool(name,args,timeout=20)
    data=reply.structured_content if reply.structured_content is not None else reply.data
    rec={'client':label,'tool':name,'input':args,'error':reply.is_error,'output':data}
   except Exception as exc: rec={'client':label,'tool':name,'input':args,'error':True,'output':str(exc)}
   print(json.dumps(rec,ensure_ascii=False,default=str),flush=True); results.append(rec)
   yield rec
async def main():
 creates=[('create_memory',{'layer':'semantic','payload':{'content':'mcp semantic topaz','tags':['test']}}),('create_memory',{'layer':'episodic','payload':{'content':'mcp episodic topaz','goal':'test','outcome':'done'}}),('create_memory',{'layer':'procedural','payload':{'skill_name':'mcp procedural topaz','steps':['check']}}),('create_memory',{'layer':'rag','payload':{'query':'mcp rag topaz','content':'mcp rag document topaz','external_source':'fixture:topaz'}}),('add_working',{'content':'mcp working topaz'}),('add_session',{'session_id':'topaz-session','content':'mcp session topaz','role':'user'}),('add_event',{'event_type':'fixture','payload':{'marker':'mcp event topaz'}})]
 ids={}
 async for rec in session('agent',mint(),creates):
  if rec['tool']=='create_memory' and isinstance(rec['output'],dict): ids[rec['input']['layer']]=rec['output'].get('id')
 read_ops=[('search_context',{'query':'topaz'}),('search_rag',{'query':'topaz'}),('recall',{'query':'topaz'}),('list_working',{}),('list_session',{'session_id':'topaz-session'}),('list_events',{}),('list_clients',{}),('list_issues',{}),('client_summary',{}),('list_agents',{}),('list_access',{}),('list_schemas',{}),('propose_memory',{'layer':'episodic','payload':{'content':'mcp quarantined topaz'}}),('register_client',{'client_id':'fixture-extra','client_name':'fixture-extra'}),('reflect_and_condense',{}),('verify_integrity',{}),('edit_memory',{'layer':'rag','memory_id':ids['rag'],'updates':{'content':'unapproved'}}),('delete_memory',{'layer':'rag','memory_id':ids['rag']})]
 read_ops += [('read_memory',{'layer':layer,'memory_id':mid}) for layer,mid in ids.items()]
 async for rec in session('agent',mint(),read_ops):
  if rec['tool']=='propose_memory' and isinstance(rec['output'],dict): ids['quarantine']=rec['output'].get('id')
 # scoped admin operations, exclusively disposable fixture records.
 admin_ops=[('list_quarantine',{}),('review_quarantine',{'item_id':ids['quarantine'],'decision':'approve'}),('review_quarantine',{'item_id':ids['quarantine'],'decision':'approve'}),('edit_memory',{'layer':'rag','memory_id':ids['rag'],'updates':{'content':'mcp edited topaz'}}),('read_memory',{'layer':'rag','memory_id':ids['rag']}),('edit_memory',{'layer':'rag','memory_id':ids['rag'],'updates':{'tenant':'hostile'}}),('delete_memory',{'layer':'rag','memory_id':ids['rag']}),('read_memory',{'layer':'rag','memory_id':ids['rag']}),('verify_integrity',{}),('list_issues',{})]
 issue_id=None
 async for rec in session('admin',mint(admin=True),admin_ops):
  if rec['tool']=='list_issues':
   issues=rec['output'].get('result',[]) if isinstance(rec['output'],dict) else rec['output']
   if issues and isinstance(issues[0],dict): issue_id=issues[0].get('id')
 if issue_id:
  async for rec in session('admin',mint(admin=True),[('resolve_issue',{'issue_id':issue_id,'resolution':'fixture reviewed'})]): pass
 async for rec in session('other-tenant',mint(tenant='other-tenant',admin=True),[('read_memory',{'layer':'semantic','memory_id':ids['semantic']}),('list_quarantine',{}),('review_quarantine',{'item_id':ids['quarantine'],'decision':'reject'}),('edit_memory',{'layer':'semantic','memory_id':ids['semantic'],'updates':{'content':'hostile'}}),('delete_memory',{'layer':'semantic','memory_id':ids['semantic']})]): pass
 async for rec in session('read-only',mint(read=True,write=False),[('create_memory',{'layer':'semantic','payload':{'content':'forbidden'}}),('propose_memory',{'layer':'semantic','payload':{'content':'forbidden'}})]): pass
 Path(os.environ.get('PINAK_MCP_RESULTS', '/tmp/pinak-mcp-surface-results.json')).write_text(json.dumps(results,indent=2,default=str,ensure_ascii=False))
asyncio.run(main())
