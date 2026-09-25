"""Disposable HTTP negative/edge test. Run ONLY against a fresh local fixture DB."""
import datetime,json,os,concurrent.futures
from pathlib import Path
import httpx,jwt
BASE=os.environ.get('PINAK_API_URL','http://127.0.0.1:18765/api/v1')
SECRET=os.environ['PINAK_JWT_SECRET']
assert BASE.startswith('http://127.0.0.1:') or BASE.startswith('http://localhost:'), 'Refuse remote target'


def token(tenant='security-a',project='p',scopes=('memory.read','memory.write'),roles=('agent',),client='fixture-security',expired=False):
 now=datetime.datetime.now(datetime.timezone.utc)
 claims={'sub':'fixture','tenant':tenant,'project_id':project,'client_id':client,'roles':list(roles),'scopes':list(scopes),'exp':now+datetime.timedelta(minutes=-1 if expired else 10)}
 return jwt.encode(claims,SECRET,algorithm='HS256')


def request(method,path,auth=None,body=None,params=None,headers=None):
 h={'Authorization':'Bearer '+auth} if auth else {}
 h.update(headers or {})
 with httpx.Client(timeout=30) as client:
  response=client.request(method,BASE+path,headers=h,json=body,params=params)
 try: output=response.json()
 except ValueError: output=response.text
 return {'case':path,'method':method,'status':response.status_code,'output':output}


def check(label,res,expected):
 assert res['status'] in expected,(label,res)
 return {'test':label,'status':res['status'],'expected':expected,'output':res['output'] if res['status']>=400 else 'ok'}


def main():
 out=[]; auth=token(); admin=token(scopes=('memory.read','memory.write','memory.admin'),roles=('admin',))
 out.append(check('missing bearer',request('GET','/memory/events'),[401]))
 out.append(check('bad JWT',request('GET','/memory/events',auth='not.a.jwt'),[401]))
 out.append(check('expired JWT',request('GET','/memory/events',auth=token(expired=True)),[401]))
 out.append(check('missing tenant',request('GET','/memory/events',auth=jwt.encode({'sub':'fixture','project_id':'p','scopes':['memory.read'],'exp':datetime.datetime.now(datetime.timezone.utc)+datetime.timedelta(minutes=5)},SECRET,algorithm='HS256')),[403]))
 out.append(check('read scope required',request('GET','/memory/events',auth=token(scopes=('memory.write',))),[403]))
 out.append(check('admin role required',request('POST','/memory/maintenance/verify',auth=token(scopes=('memory.admin',))),[403]))
 out.append(check('unsigned client header',request('GET','/memory/events',auth=auth,headers={'X-Pinak-Client-Id':'trusted-other'}),[403]))
 created=request('POST','/memory/rag/add',auth=auth,body={'query':'security urnstone','external_source':'fixture:security','content':'urnstone café ☾'})
 out.append(check('unicode write',created,[201])); rid=created['output']['id']
 out.append(check('unicode read',request('GET','/memory/rag/search',auth=auth,params={'query':'café'}),[200]))
 out.append(check('other tenant read',request('GET',f'/memory/rag/{rid}',auth=token(tenant='security-b')),[404]))
 out.append(check('other project read',request('GET',f'/memory/rag/{rid}',auth=token(project='other')),[404]))
 out.append(check('other tenant admin update',request('PUT',f'/memory/rag/{rid}',auth=token(tenant='security-b',scopes=('memory.admin',),roles=('admin',)),body={'content':'hijack'}),[404]))
 out.append(check('other tenant admin delete',request('DELETE',f'/memory/rag/{rid}',auth=token(tenant='security-b',scopes=('memory.admin',),roles=('admin',))),[404]))
 out.append(check('SQL column injection',request('PUT',f'/memory/rag/{rid}',auth=admin,body={"content = 'hijack' --":'bad'}),[400]))
 out.append(check('system field update refused',request('PUT',f'/memory/rag/{rid}',auth=admin,body={'tenant':'security-b'}),[404]))
 out.append(check('SQL-like RAG query literal',request('GET','/memory/rag/search',auth=auth,params={'query':"%' OR 1=1 --"}),[200]))
 assert request('GET','/memory/rag/search',auth=auth,params={'query':"%' OR 1=1 --"})['output']==[]
 out.append(check('oversize body rejected',request('POST','/memory/rag/add',auth=auth,body={'query':'large','external_source':'fixture:large','content':'x'*1100000}),[413]))
 q=request('POST','/memory/quarantine/propose/episodic',auth=auth,body={'content':'review me urnstone'})
 out.append(check('proposal',q,[202])); qid=q['output']['id']
 out.append(check('cross-tenant quarantine review',request('POST',f'/memory/quarantine/approve/{qid}',auth=token(tenant='security-b',scopes=('memory.admin',),roles=('admin',))),[404]))
 assert request('GET','/memory/quarantine/list',auth=admin)['output'][0]['id']==qid
 out.append(check('reject pending fixture',request('POST',f'/memory/quarantine/reject/{qid}',auth=admin),[200]))
 def write(i): return request('POST','/memory/event',auth=auth,body={'event_type':'parallel','payload':{'i':i}})['status']
 with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool: statuses=list(pool.map(write,range(12)))
 assert statuses==[201]*12,statuses
 out.append({'test':'12 concurrent event writes','status':statuses})
 audit=request('POST','/memory/maintenance/verify',auth=admin)
 assert audit['output']['audit']['valid']
 out.append(check('audit consistency',audit,[200]))
 path=Path(os.environ.get('PINAK_SECURITY_RESULTS','/tmp/pinak-security-results.json'))
 path.write_text(json.dumps(out,indent=2,ensure_ascii=False))
 print(json.dumps([{'test':x['test'],'status':x['status']} for x in out],ensure_ascii=False))

if __name__=='__main__': main()
