from pathlib import Path
from html.parser import HTMLParser
import re,json,subprocess,os,tempfile
scratch=tempfile.TemporaryDirectory(prefix="tickdesk-ui-check-")
s=Path('docs/app-ui/index.html').read_text(encoding='utf-8')
class Audit(HTMLParser):
 def __init__(self): super().__init__();self.nodes=[];self.stack=[];self.issues=[]
 def handle_starttag(self,t,a):
  a=dict(a);self.nodes.append((t,a))
  if t not in {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr','path','circle','ellipse','rect','use'}:self.stack.append(t)
 def handle_startendtag(self,t,a): self.nodes.append((t,dict(a)))
 def handle_endtag(self,t):
  if t in self.stack:
   if self.stack[-1]!=t:self.issues.append('nesting '+t+' after '+self.stack[-1])
   while self.stack and self.stack.pop()!=t:pass
p=Audit();p.feed(s)
ids=[a['id'] for t,a in p.nodes if 'id'in a];dup={i for i in ids if ids.count(i)>1}
missing=[x for x in re.findall(r'getElementById\("([^"+]+)"\)',s) if x not in ids]
refs=[a.get('for') for t,a in p.nodes if 'for'in a and a['for'] not in ids]
screens=[a['id'] for t,a in p.nodes if a.get('id','').startswith('screen-')]
nav=[a[k] for t,a in p.nodes for k in ('data-go','data-tab') if k in a]
unlabelled=[a.get('id',str(a)) for t,a in p.nodes if t in ['input','select'] and not(a.get('aria-label')or any(q.get('for')==a.get('id') and a.get('id') for _,q in p.nodes)or a.get('type')=='checkbox')]
script=re.search(r'<script>([\s\S]*?)</script>',s)[1];tmp=Path(scratch.name)/'tickdesk-app-inline.js';tmp.write_text(script,encoding='utf-8');res=subprocess.run(['node','--check',str(tmp)],capture_output=True,text=True)
def lum(h):
 a=[int(h[i:i+2],16)/255 for i in (1,3,5)];a=[v/12.92 if v<=.04045 else ((v+.055)/1.055)**2.4 for v in a];return sum(v*w for v,w in zip(a,[.2126,.7152,.0722]))
def ratio(a,b):x,y=sorted([lum(a),lum(b)]);return round((y+.05)/(x+.05),2)
pairs={'text/panel':['#f3f5f9','#151a24'],'muted/panel':['#aab6c8','#151a24'],'faint/panel':['#99a7bc','#151a24'],'primary':['#0b172b','#96baff'],'red/panel':['#ff626e','#151a24'],'green/panel':['#37d89a','#151a24'],'amber/panel':['#f5b95f','#151a24'],'input outline':['#71829a','#111720'],'old white/red':['#ffffff','#ed5360']}
print(json.dumps({'screens':len(screens),'duplicate_ids':list(dup),'missing_js_ids':missing,'missing_labels':refs,'unlabelled_fields':unlabelled,'broken_navigation':[n for n in nav if 'screen-'+n not in screens],'nesting':p.issues,'syntax_exit':res.returncode,'syntax_error':res.stderr,'contrast':{k:ratio(*v)for k,v in pairs.items()}},ensure_ascii=False,indent=2))
Path(scratch.name,'tickdesk-app-audit.json').write_text(json.dumps({'ids':ids,'nodes':p.nodes},ensure_ascii=False),encoding='utf-8')

assert len(screens)==13 and not dup and not missing and not refs and not unlabelled and not p.issues
assert all("screen-"+n in screens for n in nav) and res.returncode==0
logic=subprocess.run(["node",str(Path(__file__).with_name("check_ui.cjs")),str(Path(scratch.name,"tickdesk-app-audit.json")),str(tmp)])
assert logic.returncode==0
