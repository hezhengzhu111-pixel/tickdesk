"""Run the actual local/relay executables with an isolated, durable test App peer.

Build both Cargo projects first. No real credentials, running data or Android app
are used. The temporary processes are deliberately killed in restart scenarios.
"""
import base64
import contextlib
import datetime as dt
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sqlite3
import ssl
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect

import workbench_backup

ROOT = Path(__file__).resolve().parents[1]
HOST = '127.77.0.1'
URL = f'wss://{HOST}:8443/relay/v2/connect'


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def canonical(value):
    # Test traffic uses ASCII object keys and safe integers only. Do not use this
    # helper as a general production TD-C14N-1 parser.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def identifier():
    return str(uuid.uuid4())


def free_port():
    with socket.socket() as sock:
        sock.bind((HOST, 0))
        return sock.getsockname()[1]


def query(path, sql, args=()):
    with contextlib.closing(sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)) as db:
        return db.execute(sql, args).fetchall()


def wait_for(check, label, seconds=15):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.05)
    raise AssertionError(f'timeout: {label}')


class Harness:
    def __init__(self, path):
        self.path = path
        self.children = {}
        self.logs = []
        self.peers = []
        self.tokens = {role: secrets.token_urlsafe(32) for role in ('app', 'local')}
        self.local_port = free_port()
        self.metrics_port = free_port()
        self.database = path / 'data' / 'tickdesk.sqlite3'
        with socket.socket() as probe:
            probe.bind((HOST, 8443))  # Fail if occupied; never stop another service.
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'TickDesk integration only')])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(minutes=1)).not_valid_after(now + dt.timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(HOST))]), False)
                .add_extension(x509.BasicConstraints(ca=True, path_length=None), True).sign(key, hashes.SHA256()))
        (path / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        (path / 'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        pin = base64.b64encode(hashlib.sha256(spki).digest()).decode()
        self.trust = ssl.create_default_context(cafile=str(path / 'cert.pem'))
        self.roles()
        (path / 'local.token').write_text(self.tokens['local'], encoding='ascii')
        (path / 'relay.toml').write_text(f"listen='{HOST}:8443'\nroles_file='roles.toml'\nmetrics_listen='{HOST}:{self.metrics_port}'\n[tls]\ncert='cert.pem'\nkey='key.pem'\n", encoding='utf-8')
        (path / 'local.toml').write_text(f"listen='{HOST}:{self.local_port}'\ndata_dir='data'\n[relay]\nrelay_url='{URL}'\npair_id='integration'\ntoken_file='local.token'\npins=['sha256/{pin}']\n", encoding='utf-8')
        business = json.loads((ROOT / 'contracts/v2/business.schema.json').read_text(encoding='utf-8'))
        relay = json.loads((ROOT / 'contracts/v2/relay.schema.json').read_text(encoding='utf-8'))
        registry = Registry().with_resource(business['$id'], Resource.from_contents(business))
        self.validator = Draft202012Validator(relay, registry=registry, format_checker=FormatChecker())

    def roles(self, app=True):
        text = ''.join(f"[pairs.integration.{role}]\nsha256='{hashlib.sha256(token.encode()).hexdigest()}'\n" for role, token in self.tokens.items() if app or role != 'app')
        candidate = self.path / 'roles.next'
        candidate.write_text(text, encoding='ascii')
        os.replace(candidate, self.path / 'roles.toml')

    def start(self, name, executable=None):
        project = 'local-service' if name == 'local' else 'local-service/relay'
        suffix = '.exe' if os.name == 'nt' else ''
        executable = executable or ROOT / project / 'target/debug' / f'tickdesk-{name}{suffix}'
        log = open(self.path / f'{name}-{len(self.logs)}.log', 'wb')
        self.logs.append(log)
        self.children[name] = subprocess.Popen([str(executable), '--config', str(self.path / f'{name}.toml')], cwd=executable.parent, stdout=log, stderr=log, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        wait_for(lambda: self.health(name, 'live') == 200, f'{name} startup')

    def stop(self, name):
        process = self.children.pop(name, None)
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=10)

    def manage(self, *args):
        suffix = '.exe' if os.name == 'nt' else ''
        executable = ROOT / 'local-service/target/debug' / f'tickdesk-local{suffix}'
        subprocess.run([str(executable),'--config',str(self.path/'local.toml'),*map(str,args)],
                       check=True,capture_output=True,timeout=30,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))

    def health(self, name, route):
        url = f'https://{HOST}:8443' if name == 'relay' else f'http://{HOST}:{self.local_port}'
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(context=self.trust))
        try:
            with opener.open(f'{url}/health/{route}', timeout=1) as response:
                return response.status
        except urllib.error.HTTPError as error:
            error.close()
            return error.code
        except (OSError, urllib.error.URLError):
            return None

    def dial(self, token=None, protocol='tickdesk.v2'):
        return connect(URL, ssl=self.trust, proxy=None, compression=None, subprotocols=[protocol], additional_headers={'Authorization': f'Bearer {token or self.tokens["app"]}'}, open_timeout=5, close_timeout=1, ping_interval=15, ping_timeout=45, max_size=128*1024)

    def identity(self):
        return dict(query(self.database, "SELECT key,value FROM metadata WHERE key IN ('incarnation','server_epoch')"))

    def close(self):
        for peer in self.peers:
            peer.close()
        for name in list(self.children):
            self.stop(name)
        for log in self.logs:
            log.close()


class AppPeer:
    def __init__(self, harness, incarnation=None):
        self.h = harness
        self.incarnation = incarnation or identifier()
        self.ws = None
        self.frames = []
        self.hold_receipts = False
        self.db = sqlite3.connect(harness.path / f'app-{self.incarnation}.sqlite3')
        self.db.execute('CREATE TABLE IF NOT EXISTS inbox (sender TEXT, id TEXT, hash TEXT, body TEXT, PRIMARY KEY(sender,id))')
        self.h.peers.append(self)

    def connect(self):
        self.frames.clear()
        self.ws = self.h.dial()
        self.send({'v':2, 'type':'hello', 'incarnation':self.incarnation, 'serverEpoch':None})
        welcome = self.wait(lambda f: f['type'] == 'welcome')
        require(welcome['pairId'] == 'integration' and welcome['role'] == 'app', 'authenticated pair/role')
        self.wait(lambda f: f['type'] == 'presence' and f['peer']['online'])

    def close_socket(self):
        if self.ws:
            self.ws.close()
            self.ws = None

    def close(self):
        self.close_socket()
        if self.db:
            self.db.close()
            self.db = None

    def send(self, frame):
        self.h.validator.validate(frame)
        self.ws.send(canonical(frame).decode())

    def receipt(self, frame):
        self.send({key: frame[key] for key in ('v','deliveryId','payloadHash','fromIncarnation','toIncarnation')} | {'type':'receipt'})

    def receive(self, timeout):
        raw = self.ws.recv(timeout=timeout)
        require(isinstance(raw, str), 'relay sent binary data')
        frame = json.loads(raw)
        self.h.validator.validate(frame)
        if frame['type'] == 'deliver':
            require(frame['toIncarnation'] == self.incarnation, 'delivery recipient')
            require(frame['payloadHash'] == digest(frame['payload']), 'delivery payload hash')
            original = self.db.execute('SELECT hash FROM inbox WHERE sender=? AND id=?', (frame['fromIncarnation'], frame['deliveryId'])).fetchone()
            require(original is None or original[0] == frame['payloadHash'], 'delivery ID reused with another hash')
            self.db.execute('INSERT OR IGNORE INTO inbox VALUES (?,?,?,?)', (frame['fromIncarnation'],frame['deliveryId'],frame['payloadHash'],raw))
            self.db.commit()  # Test peer receipts only after SQLite persistence.
            if not self.hold_receipts:
                self.receipt(frame)
        self.frames.append(frame)
        return frame

    def wait(self, predicate, start=0, seconds=20):
        deadline = time.monotonic() + seconds
        for frame in self.frames[start:]:
            if predicate(frame):
                return frame
        while time.monotonic() < deadline:
            frame = self.receive(max(0.01, deadline - time.monotonic()))
            if predicate(frame):
                return frame
        raise AssertionError('expected frame not received')

    def frame(self, payload, category='business', delivery_id=None):
        return {'v':2,'type':'deliver','deliveryId':delivery_id or identifier(),'fromIncarnation':self.incarnation,'toIncarnation':self.h.identity()['incarnation'],'category':category,'payloadHash':digest(payload),'payload':payload}

    def command(self, name, fields=None, client_id=None, epoch=None):
        key = client_id or identifier()
        epoch = epoch or self.h.identity()['server_epoch']
        return {'v':2,'kind':'command','id':identifier(),'dedupeKey':f'cmd:{key}','ts':int(time.time()*1000),'serverEpoch':epoch,'data':{'cmd':name,'clientMsgId':key,'serverEpoch':epoch,**(fields or {})}}

    def transact(self, payload, delivery_id=None):
        start = len(self.frames)
        frame = self.frame(payload, delivery_id=delivery_id)
        self.send(frame)
        receipt = self.wait(lambda f: f['type']=='receipt' and f['deliveryId']==frame['deliveryId'], start)
        for key in ('payloadHash','fromIncarnation','toIncarnation'):
            require(receipt[key] == frame[key], f'receipt {key} must echo deliver')
        ack = self.wait(lambda f: f['type']=='deliver' and f['category']=='business' and f['payload']['kind']=='ack' and f['payload']['data']['clientMsgId']==payload['data']['clientMsgId'] and f['payload']['data']['requestHash']==digest(payload['data']), start)
        return ack

    def call(self, name, fields=None):
        ack = self.transact(self.command(name, fields))['payload']['data']
        require(ack['ok'], f'{name} rejected: {ack["error"]}')
        return ack['result']


def test_png():
    # Deterministic incompressible test pixels: > 4 file chunks, no user images.
    pixels = hashlib.shake_256(b'tickdesk integration fixture').digest(256*256*3)
    raw = b''.join(b'\0' + pixels[i:i+768] for i in range(0,len(pixels),768))
    def chunk(kind, data):
        return struct.pack('!I',len(data)) + kind + data + struct.pack('!I',zlib.crc32(kind+data))
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR',struct.pack('!2I5B',256,256,8,2,0,0,0)) + chunk(b'IDAT',zlib.compress(raw)) + chunk(b'IEND',b'')


def run(h):
    h.start('relay')
    require(h.health('relay','ready') == 200, 'relay readiness')
    for token, protocol, status in [(secrets.token_urlsafe(32),'tickdesk.v2',401),(h.tokens['app'],'wrong',426)]:
        try:
            with h.dial(token, protocol):
                raise AssertionError('invalid handshake accepted')
        except InvalidStatus as error:
            require(error.response.status_code == status, 'handshake rejection status')
    print('PASS: actual relay TLS, authentication and subprotocol rejection', flush=True)
    h.start('local')
    require(h.health('local','ready') == 503, 'incomplete local capabilities must remain unready')
    identity = h.identity()
    app = AppPeer(h)
    app.connect()
    status = app.call('get_status')
    catalog = json.loads((ROOT / 'contracts/v2/commands.json').read_text(encoding='utf-8'))
    require(set(status['protocol']['supportedCommands']) == {command['name'] for command in catalog['commands']},
            'implemented v2 command capability set')
    require(app.call('get_execution_progress') == {'version': 3, 'progress': None},
            'versioned execution capability round trip through the actual relay')
    holding = app.call('get_holding_details', {'symbol': '300502.SZ'})
    require(holding['version'] == 3 and holding['serverEpoch'] == identity['server_epoch'] and holding['holding'] is None,
            'versioned holding capability round trip through the actual relay')
    print('PASS: versioned quant read probes across the actual TLS relay', flush=True)
    app.close_socket()
    h.stop('local')
    h.manage('--backup-to',h.path/'pre-command-backup')
    h.start('local')
    app.connect()
    pause = app.command('pause')
    first = app.transact(pause)
    require(first['payload']['data']['ok'], 'pause succeeded')
    duplicate = app.transact(pause)
    require(duplicate['payload'] == first['payload'], 'same command must return original complete ack')
    conflict = json.loads(json.dumps(pause))
    conflict['data']['cmd'] = 'resume'
    rejected = app.transact(conflict)
    require(rejected['payload']['data']['error']['code']=='COMMAND_ID_REUSED', 'conflicting business key')
    require(app.call('get_status')['analysis']['controlVersion']==1, 'duplicate command caused a second effect')
    print('PASS: actual local command, durable receipt, original ack replay and idempotency conflict', flush=True)

    app.hold_receipts = True
    pending = app.transact(app.command('get_status'))
    app.close_socket()
    h.stop('relay')
    h.start('relay')
    app.connect()
    replay = app.wait(lambda f:f['type']=='deliver' and f['deliveryId']==pending['deliveryId'], seconds=40)
    require(replay==pending, 'relay restart changed immutable delivery')
    app.hold_receipts = False
    for frame in list(app.frames):
        if frame['type']=='deliver': app.receipt(frame)
    wait_for(lambda: query(h.database,"SELECT state FROM transport_outbox WHERE delivery_id=?",(pending['deliveryId'],))==[('delivered',)], 'receipt committed locally')
    require(h.identity()==identity, 'relay restart changed local identity')
    print('PASS: relay crash/restart, local reconnect, lost receipt and identical durable replay', flush=True)

    app.close_socket()
    h.stop('local')
    h.start('local')
    app.connect()
    require(h.identity()==identity, 'normal local restart changed identity')
    require(app.transact(pause)['payload']==first['payload'], 'restart lost first ack')
    require(app.call('get_status')['analysis']['controlVersion']==1, 'restart replay duplicated effect')
    status = app.call('get_command_status', {'originalEpoch':identity['server_epoch'],'targetClientMsgId':pause['data']['clientMsgId'],'targetRequestHash':digest(pause['data'])})
    require(status['state']=='found' and status['ackEventId']==first['payload']['id'], 'query must find original ack')
    print('PASS: local crash/restart preserves identity, first result and read-only command status', flush=True)

    data = test_png()
    upload = app.call('begin_upload',{'name':'integration.png','mime':'image/png','sizeBytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})['upload']
    chunks = [data[i:i+32768] for i in range(0,len(data),32768)]
    for index, chunk in enumerate(chunks):
        payload = {'transferId':upload['uploadId'],'fileId':upload['fileId'],'index':index,'totalChunks':len(chunks),'chunkHash':hashlib.sha256(chunk).hexdigest(),'bytes':base64.b64encode(chunk).decode()}
        frame = app.frame(payload,'file_chunk')
        start = len(app.frames)
        app.send(frame)
        app.wait(lambda f:f['type']=='receipt' and f['deliveryId']==frame['deliveryId'],start)
    original = app.call('finalize_upload',{'uploadId':upload['uploadId']})['file']
    require((h.path/'data/files'/original['fileId']).read_bytes()==data, 'archived bytes')
    app.hold_receipts = True
    start = len(app.frames)
    download = app.call('read_file',{'fileId':original['fileId']})
    busy_args = {'fileId':original['fileId'],'baseVersion':original['version'],'confirm':True}
    busy = app.transact(app.command('delete_file',busy_args))['payload']['data']
    require(not busy['ok'] and busy['error']['code']=='FILE_BUSY', 'download lease must prevent deletion')
    app.hold_receipts = False
    for frame in list(app.frames[start:]):
        if frame['type']=='deliver': app.receipt(frame)
    downloaded = {}
    deadline = time.monotonic()+20
    while len(downloaded)<download['totalChunks']:
        for frame in app.frames[start:]:
            if frame['type']=='deliver' and frame['category']=='file_chunk' and frame['payload']['transferId']==download['downloadId']:
                part=frame['payload']
                decoded=base64.b64decode(part['bytes'],validate=True)
                require(hashlib.sha256(decoded).hexdigest()==part['chunkHash'],'download chunk hash')
                downloaded[part['index']]=decoded
        if len(downloaded)<download['totalChunks']: app.receive(max(0.01,deadline-time.monotonic()))
    require(b''.join(downloaded[i] for i in range(len(downloaded)))==data,'full downloaded bytes')
    wait_for(lambda: query(h.database, 'SELECT state FROM file_downloads WHERE download_id=?',
                           (download['downloadId'],)) == [('complete',)],
             'final download receipt committed before stop')
    app.close_socket()
    h.stop('local')
    evidence = h.path / 'python-evidence'
    facts = evidence / 'tickdesk-ai/facts.sqlite3'
    facts.parent.mkdir(parents=True)
    (evidence / 'user_data').mkdir()
    (evidence / 'user_data/secrets.json').write_text('fixture-only', encoding='utf-8')
    (evidence / 'backtest_runs').mkdir()
    (evidence / 'backtest_runs/fixture.json').write_text('{"status":"isolated-fixture"}', encoding='utf-8')
    with contextlib.closing(sqlite3.connect(facts)) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE facts (body TEXT)")
        connection.execute("INSERT INTO facts VALUES ('synthetic source revision')")
        connection.commit()
        require(facts.with_name(facts.name + '-wal').stat().st_size > 0, 'evidence fixture committed in WAL')
        executable = ROOT / 'local-service/target/debug' / ('tickdesk-local.exe' if os.name == 'nt' else 'tickdesk-local')
        archive = h.path / 'combined-backup'
        workbench_backup.backup(executable, h.path / 'local.toml', evidence, archive)
    restored = h.path / 'combined-restore'
    workbench_backup.restore(executable, archive, restored)
    require(
        query(restored / 'python-data/tickdesk-ai/facts.sqlite3', 'SELECT body FROM facts')
        == [('synthetic source revision',)], 'committed Python evidence restored',
    )
    require(not (archive / 'python-data/user_data/secrets.json').exists(), 'supplier configuration excluded')
    require((restored / 'python-data/backtest_runs/fixture.json').read_text(encoding='utf-8')
            == '{"status":"isolated-fixture"}', 'backtest version evidence restored')
    require((restored / 'local-data/restore-quarantine' / original['fileId']).read_bytes() == data,
            'combined restore quarantines genuine uploaded bytes')
    require(query(restored / 'local-data/tickdesk.sqlite3', 'SELECT COUNT(*) FROM recovery') == [(1,)],
            'combined restore preserves recovery gate')
    require(h.identity() == identity, 'combined restore leaves source identity in place')
    archived_facts = archive / 'python-data/tickdesk-ai/facts.sqlite3'
    with archived_facts.open('ab') as edited:
        edited.write(b'tampered')
    try:
        workbench_backup.restore(executable, archive, h.path / 'tampered-restore')
    except ValueError:
        require(not (h.path / 'tampered-restore').exists(), 'tampered restore never published')
    else:
        raise AssertionError('tampered Python archive must be rejected')
    print('PASS: combined archive restores committed Python evidence, quarantines original, isolates epoch, and rejects tampering', flush=True)
    h.start('local')
    app.connect()
    deleted = app.call('delete_file',busy_args)['file']
    require(deleted['status']=='deleted', 'deletion tombstone')
    require(not (h.path/'data/files'/original['fileId']).exists(), 'manual deletion removed original')
    print(f'PASS: real {len(chunks)}-chunk upload/download, hash checks, lease and manual original deletion', flush=True)

    sync_start=len(app.frames)
    sync=app.call('sync')
    page=app.wait(lambda f:f['type']=='deliver' and f['payload']['id']==sync['pageEventId'],sync_start)['payload']['data']
    require(not page['hasMore'] and page['meta']['analysis']['paused'], 'authoritative snapshot after pause')
    for name, entry in page['manifest'].items():
        records=[page['meta']] if name=='meta' else page[name]
        require(entry['count']==len(records) and entry['sha256']==digest(records),f'snapshot manifest {name}')
    print('PASS: authoritative snapshot schema and collection manifest',flush=True)

    app.close_socket()
    h.stop('local')
    config=h.path/'local.toml'
    config.write_text(config.read_text(encoding='utf-8').replace("data_dir='data'","data_dir='restored-data'"),encoding='utf-8')
    h.manage('--restore-from',h.path/'pre-command-backup')
    h.database=h.path/'restored-data/tickdesk.sqlite3'
    h.start('local')
    app.connect()
    require(h.identity()['incarnation']!=identity['incarnation'] and h.identity()['server_epoch']!=identity['server_epoch'],'restore must rotate both identities')
    recovery=app.call('get_recovery')
    session=recovery['reconciliationId']
    manifest=[]
    for record_id,kind,body in [('old-command','command',pause),('old-ack','ack',first['payload'])]:
        proof=app.call('recovery_evidence',{'reconciliationId':session,'recordId':record_id,'sourceType':kind,
            'payloadHash':digest(body),'partIndex':0,'partCount':1,'bytesBase64':base64.b64encode(canonical(body)).decode(),
            'evidenceEpoch':identity['server_epoch'],'capturedAt':int(time.time()*1000),'observedSnapshotSeq':first['payload']['snapshotSeq']})
        require(proof['record']['status']=='verified','recovery evidence')
        manifest.append({'recordId':record_id,'sourceType':kind,'payloadHash':digest(body)})
    recovery=app.call('get_recovery')
    require(len(recovery['items'])==1,'single missing historical pause')
    item=recovery['items'][0]
    archived=app.call('recovery_reconcile',{'reconciliationId':session,'reviewedSnapshotSeq':recovery['reviewedSnapshotSeq'],
        'positions':[],'decisions':[{'itemId':item['itemId'],'evidenceRecordIds':item['evidenceRecordIds'],
        'disposition':'archive_operation','note':'Reviewed historical pause; retain restored state.'}]})
    require(archived['remainingCount']==0,'historical operation archived')
    seq=archived['reviewedSnapshotSeq']
    app.call('recovery_seal_evidence',{'reconciliationId':session,'reviewedSnapshotSeq':seq,
        'recordCount':len(manifest),'manifestHash':digest(sorted(manifest,key=lambda p:p['recordId']))})
    require(not app.call('recovery_confirm',{'reconciliationId':session,'reviewedSnapshotSeq':seq})['recoveryMode'],'recovery complete')
    current=app.call('get_status')
    require(not current['analysis']['paused'] and current['analysis']['controlVersion']==0,'archive must not replay old operation')
    require(query(h.database,"SELECT count(*) FROM objects WHERE collection='executions'")==[(0,)],'archive must not create trades')
    print('PASS: actual backup/restore, epoch isolation, evidence sealing and non-trade archival without replay',flush=True)
    old_app=app.incarnation
    app.close_socket()
    app=AppPeer(h)
    app.connect()
    wait_for(lambda:query(h.database,'SELECT incarnation FROM peer')==[(app.incarnation,)],'replacement App committed locally')
    require(query(h.database,'SELECT count(*) FROM retired_peers WHERE incarnation=?',(old_app,))==[(1,)],'old App identity retired')
    stale=app.transact(pause)['payload']['data']
    require(not stale['ok'] and stale['error']['code']=='SERVER_EPOCH_MISMATCH','old epoch cannot execute under replacement App')
    unknown=app.call('get_command_status',{'originalEpoch':identity['server_epoch'],'targetClientMsgId':pause['data']['clientMsgId'],'targetRequestHash':digest(pause['data'])})
    require(unknown['state']=='not_found','restored backup does not invent old execution history')
    require(not app.call('get_status')['analysis']['paused'],'status query/rejected replay must preserve restored state')
    print('PASS: replacement App identity fence, old epoch rejection and unknown status without replay',flush=True)
    h.roles(app=False)
    try:
        while True: app.receive(5)
    except Exception as error:
        from websockets.exceptions import ConnectionClosed
        require(isinstance(error,ConnectionClosed) and error.rcvd is not None and error.rcvd.code==4401,'revocation must close actual App connection with 4401')
    wait_for(lambda:h.health('relay','ready')==503,'readiness after role revocation')
    require(h.health('relay','live')==200,'liveness after revocation')
    print('PASS: atomic credential revocation and truthful readiness', flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix='tickdesk-live-integration-') as directory:
        h=Harness(Path(directory))
        try:
            run(h)
        except Exception:
            print('DIAGNOSTIC process exits:',{name:p.poll() for name,p in h.children.items()},flush=True)
            print('DIAGNOSTIC relay live/ready:',h.health('relay','live'),h.health('relay','ready'),flush=True)
            # Only generated test logs; defensively redact test credentials.
            for log in h.logs:
                log.flush()
                tail = Path(log.name).read_text(encoding='utf-8',errors='replace')[-3000:]
                for token in h.tokens.values(): tail = tail.replace(token,'[redacted]')
                print(f'DIAGNOSTIC {Path(log.name).name}: {tail}',flush=True)
            raise
        finally:
            h.close()
    print('PASS: two real services integrated; all owned processes stopped; temporary credentials/data removed')


if __name__=='__main__':
    main()
