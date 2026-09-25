import datetime, json
import httpx,jwt
from pathlib import Path
base=os.environ.get('PINAK_API_URL', 'http://127.0.0.1:18765/api/v1'); secret=os.environ['PINAK_JWT_SECRET']
assert base.startswith('http://127.0.0.1:') or base.startswith('http://localhost:'), 'Refuse remote target'
now=datetime.datetime.now(datetime.timezone.utc)
token=jwt.encode({'sub':'sandbox-agent','tenant':'sandbox-tenant','project_id':'sandbox-project','roles':['agent'],'scopes':['memory.read','memory.write'],'client_id':'sandbox-agent','iat':now,'exp':now+datetime.timedelta(minutes=30)},secret,algorithm='HS256')
headers={'Authorization':'Bearer '+token}; results=[]
def call(layer,method,path,payload=None,params=None):
 with httpx.Client(base_url=base,headers=headers,timeout=20) as client: response=client.request(method,path,json=payload,params=params)
 try: body=response.json()
 except: body=response.text
 record={'layer':layer,'method':method,'path':path,'input':payload or params,'status':response.status_code,'output':body}
 results.append(record)
 print(json.dumps(record,ensure_ascii=False))
 return body

layers=[('semantic','/memory/add',{'content':'fixture semantic moonlamp','tags':['fixture']}),('episodic','/memory/episodic/add',{'content':'fixture episodic moonlamp','goal':'test','outcome':'done'}),('procedural','/memory/procedural/add',{'skill_name':'fixture procedural moonlamp','steps':['one'],'trigger':'fixture'}),('rag','/memory/rag/add',{'query':'fixture rag moonlamp','external_source':'fixture:document','content':'fixture rag content moonlamp'}),('working','/memory/working/add',{'content':'fixture working moonlamp'}),('sessions','/memory/session/add',{'session_id':'fixture-moonlamp-session','content':'fixture sessions moonlamp','role':'user'}),('events','/memory/event',{'event_type':'fixture','payload':{'marker':'fixture events moonlamp'}})]
for layer,path,payload in layers:
 written=call(layer,'POST',path,payload)
 route={'semantic':('/memory/retrieve_context',{'query':'moonlamp'}),'episodic':('/memory/retrieve_context',{'query':'moonlamp'}),'procedural':('/memory/retrieve_context',{'query':'moonlamp'}),'rag':('/memory/rag/search',{'query':'moonlamp'}),'working':('/memory/working/list',{}),'sessions':('/memory/session/list',{'session_id':'fixture-moonlamp-session'}),'events':('/memory/events',{})}[layer]
 read=call(layer,'GET',route[0],params=route[1])
 assert isinstance(written,dict) and written.get('id') in json.dumps(read),f'{layer} readback failed'
call('rag-hybrid','GET','/memory/retrieve_context',params={'query':'moonlamp'})
Path(os.environ.get('PINAK_HTTP_RESULTS', '/tmp/pinak-seven-layer-results.json')).write_text(json.dumps(results,indent=2,ensure_ascii=False))
