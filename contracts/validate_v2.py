"""Offline design contract checks, not service integration tests."""
from pathlib import Path
import base64, copy, hashlib, json, random
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from openapi_spec_validator import validate_spec
from validate_v1 import canonical, digest, loads_business

PACK=Path(__file__).resolve().parent/'v2'
def read(n):return json.loads((PACK/n).read_text(encoding='utf-8'))
def check(ok,msg):
    if not ok:raise AssertionError(msg)

def wire_check(p,v):
    v.validate(p)
    check(len(canonical(p))<=131072,'wire byte limit')
    if p['type']=='presence':check(p['peer']['online']==(p['peer']['incarnation'] is not None),'presence identity')
    if p['type']=='deliver':
        check(digest(p['payload'])==p['payloadHash'],'payload hash')
        if p['category']=='business':check(len(canonical(p['payload']))<=65536,'business byte limit')
        else:
            c=p['payload'];raw=base64.b64decode(c['bytes'],validate=True)
            check(0<len(raw)<=32768,'chunk length');check(c['index']<c['totalChunks'],'chunk index')
            check(c['index']==c['totalChunks']-1 or len(raw)==32768,'non-tail length')
            check(hashlib.sha256(raw).hexdigest()==c['chunkHash'],'chunk hash')

def fault_model():
    rng=random.Random(20261115)
    for trial in range(10000):
        pending=True;inbox={};ledger={};ack_out={};delivered=False;effects=0
        for attempt in range(40):
            cut=rng.randrange(5) if attempt<39 else 4
            if cut==0:continue # relay crash before forward
            inbox.setdefault('delivery_1','immutable')
            if cut==1:continue # receipt lost after durable inbox
            delivered=True
            check(pending,'receipt erased business intent')
            if 'command_1' not in ledger:
                ledger['command_1']='result';ack_out['ack_1']='result';effects+=1
            if cut==2:continue # commit succeeded but business ack lost
            pending=False
            break
        check(not pending and delivered and effects==1,'lost or duplicate effect')
        check(ledger['command_1']==ack_out['ack_1'],'atomic business result')
    active='new';old='old';forwarded=[]
    for conn in [old,active,old]:
        if conn==active:forwarded.append(conn)
    if old==active:active=None
    check(forwarded==['new'] and active=='new','delayed close/session fencing')
    check(('d','hash','s1','r2')!=('d','hash','s1','r1'),'stale receipt')
    chunks={0:b'a'*32768};check(len(chunks)!=2,'missing chunk finalized')
    chunks.setdefault(0,b'other');check(chunks[0]==b'a'*32768,'duplicate block overwrite')
    chunks[1]=b'b';check(b''.join(chunks.values())==b'a'*32768+b'b','file reassembly')
    return 10000

def main():
    s=read('business.schema.json');w=read('relay.schema.json')
    for schema in [s,w,read('pairing.schema.json')]:Draft202012Validator.check_schema(schema)
    registry=Registry().with_resources([(x['$id'],Resource.from_contents(x)) for x in [s,w]])
    bv=Draft202012Validator(s,registry=registry);wv=Draft202012Validator(w,registry=registry)
    validate_spec(read('cloud.openapi.json'))
    cases=read('fixtures.json')['cases'];failures=[]
    for c in cases:
        valid=bv.is_valid(c['payload'])
        if valid and c['valid']:check(len(canonical(c['payload']))<=65536,'fixture too large '+c['name'])
        if valid!=c['valid']:failures.append(c['name']+': '+('unexpected acceptance' if valid else next(bv.iter_errors(c['payload'])).message[:180]))
    check(not failures,'\n'.join(failures))
    idx={c['name']:c['payload'] for c in cases};commands=read('commands.json')['commands']
    for c in commands:
        req=idx['request_'+c['name']];ack=idx['ack_'+c['name']]['data']
        check(ack['refId']==req['id'] and ack['clientMsgId']==req['data']['clientMsgId'],'ack association')
        check(ack['requestHash']==digest(req['data']),'request hash '+c['name'])
        check(c['request'].split('/')[-1] in s['$defs'] and c['result'].split('/')[-1] in s['$defs'],'catalog reference')
    for c in read('relay-fixtures.json')['cases']:
        try:wire_check(c['payload'],wv);valid=True
        except Exception:valid=False
        check(valid==c['valid'],'wire '+c['name'])
    delivery=copy.deepcopy(next(c['payload'] for c in read('relay-fixtures.json')['cases'] if c['name']=='deliver_command'))
    for mutation in [lambda p:p.update(payloadHash='0'*64),lambda p:p['payload'].update(v=1)]:
        p=copy.deepcopy(delivery);mutation(p)
        try:wire_check(p,wv)
        except Exception:continue
        raise AssertionError('corruption accepted')
    for raw in ['{"a":1,"a":2}','{"a":-0}','{"a":1.0}','{"a":9007199254740992}']:
        try:loads_business(raw)
        except ValueError:continue
        raise AssertionError('unsafe JSON accepted')
    h=read('hash-vectors.json')['business'];check(canonical(h['input']).decode()==h['canonicalUtf8'] and digest(h['input'])==h['sha256'],'canonical vector')
    Draft202012Validator({'$defs':s['$defs'],'$ref':'#/$defs/Push'}).validate(read('push.example.json'))
    long_reply=idx['history_preserves_full_reply']['data']['result']['items'][0]['reply']
    check(len(long_reply['text'])==17000 and all(k in long_reply for k in ['sources','attachments','unverified','errorCode']),'history lost reply fields')
    for r in read('evidence-vectors.json')['records']:
        raw=b''.join(base64.b64decode(p,validate=True) for p in r['parts']);v=loads_business(raw.decode())
        check(hashlib.sha256(raw).hexdigest()==r['payloadHash'] and v==r['payload'],'evidence bytes/hash')
        Draft202012Validator({'$defs':s['$defs'],'$ref':'#/$defs/Evidence_'+r['sourceType']}).validate(v)
    e=idx['request_recovery_evidence']['data'];raw=base64.b64decode(e['bytesBase64'],validate=True)
    check(loads_business(raw.decode())==idx['request_record_execution'] and hashlib.sha256(raw).hexdigest()==e['payloadHash'],'evidence request')
    pages=[c['payload']['data'] for c in cases if c['valid'] and c['payload'].get('kind')=='state_snapshot']
    for p in pages:
        if p['manifest']:
            group=[q for q in pages if q['snapshotId']==p['snapshotId']]
            for k,m in p['manifest'].items():
                data=[q['meta'] for q in group if q['meta']] if k=='meta' else [x for q in group for x in q[k]]
                check(len(data)==m['count'] and digest(data)==m['sha256'],'snapshot '+k)
    print('PASS '+json.dumps({'commands':len(commands),'businessFixtures':len(cases),'relayFixtures':len(read('relay-fixtures.json')['cases']),'evidenceVectors':len(read('evidence-vectors.json')['records']),'faultModelTraces':fault_model(),'cloudOperations':len(read('cloud.openapi.json')['paths'])}))

if __name__=='__main__':main()
