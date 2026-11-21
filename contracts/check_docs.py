"""Active design links and HTML/JS structure only; never opens a browser."""
from pathlib import Path
from html.parser import HTMLParser
import json, re, subprocess, tempfile

ROOT=Path(__file__).resolve().parent.parent
class Page(HTMLParser):
    def __init__(self):
        super().__init__();self.ids=[];self.links=[];self.scripts=[];self.in_script=False
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if 'id' in a:self.ids.append(a['id'])
        if tag=='a' and 'href' in a:self.links.append(a['href'])
        if tag=='script':self.in_script=True;self.scripts.append('')
    def handle_endtag(self,tag):
        if tag=='script':self.in_script=False
    def handle_data(self,data):
        if self.in_script:self.scripts[-1]+=data

def main():
    docs=[ROOT/'README.md',ROOT/'SOURCES.md',ROOT/'contracts/README.md',*[p for p in (ROOT/'docs').rglob('*.md') if 'archive' not in p.parts]]
    broken=[];count=0
    for p in docs:
        text=p.read_text(encoding='utf-8')
        for link in re.findall(r'\[[^\]]*\]\(([^)]+)\)',text):
            if '://' in link or link.startswith('#'):continue
            count+=1
            if not (p.parent/link.split('#')[0]).resolve().exists():broken.append((str(p),link))
    assert not broken,broken
    scripts=0;screens=0
    for name in ['docs/app-ui/index.html','docs/architecture/v6.html','docs/architecture/v5.html']:
        p=ROOT/name;page=Page();page.feed(p.read_text(encoding='utf-8'))
        assert len(page.ids)==len(set(page.ids)),name+' duplicate ID'
        for link in page.links:
            if link.startswith('#'):assert link[1:] in page.ids,(name,link)
            elif '://' not in link:assert (p.parent/link.split('#')[0]).exists(),(name,link)
        for script in page.scripts:
            with tempfile.TemporaryDirectory(prefix='tickdesk-js-check-') as tmp:
                js=Path(tmp)/'inline.js';js.write_text(script,encoding='utf-8')
                result=subprocess.run(['node','--check',str(js)],capture_output=True,text=True)
                assert result.returncode==0,result.stderr
                scripts+=1
        if name.endswith('app-ui/index.html'):
            screens=sum(i.startswith('screen-') for i in page.ids)
            assert screens==13,screens
    print('PASS '+json.dumps({'activeMarkdown':len(docs),'localLinks':count,'htmlFiles':3,'inlineScripts':scripts,'appScreens':screens,'scope':'static only; no visual rendering'}))

if __name__=='__main__':main()
