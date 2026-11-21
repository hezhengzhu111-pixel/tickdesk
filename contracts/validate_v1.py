"""Validate the checked-in contract pack, offline; never call live services."""
from pathlib import Path
import base64
import copy
import hashlib
import hmac
import json

from jsonschema import Draft202012Validator, FormatChecker
from openapi_spec_validator import validate_spec

ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / 'contracts' / 'v1'


def read(name):
    return json.loads((PACK / name).read_text(encoding='utf-8'))


def canonical(value):
    """TD-C14N-1: integer-only JSON numbers, Unicode scalars, no normalization."""
    def check(item):
        if isinstance(item, float):
            raise ValueError('decimal values must use strings')
        if isinstance(item, int) and not isinstance(item, bool):
            if abs(item) > 9007199254740991:
                raise ValueError('JSON integer outside interoperable range')
        if isinstance(item, str):
            item.encode('utf-8', errors='strict')
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif isinstance(item, dict):
            for key, child in item.items():
                if not isinstance(key, str) or not key.isascii():
                    raise ValueError('business property names must be ASCII')
                check(child)
    check(value)
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def loads_business(text):
    def reject_number(value):
        raise ValueError('non-integer JSON number')
    def integer(value):
        if value == '-0' or abs(int(value)) > 9007199254740991:
            raise ValueError('invalid interoperable integer')
        return int(value)
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate property')
            result[key] = value
        return result
    value = json.loads(text, parse_int=integer, parse_float=reject_number,
                       parse_constant=reject_number, object_pairs_hook=pairs)
    canonical(value)
    return value


def encode(envelope):
    raw = canonical(envelope)
    if len(raw) > 65536:
        raise ValueError('MESSAGE_TOO_LARGE')
    body = raw.decode('utf-8')
    if len(body) <= 2000:
        return [(envelope['id'], body)]
    parts = [raw[i:i + 768] for i in range(0, len(raw), 768)]
    transfer = envelope['id']  # v1: one immutable transfer per logical event
    output = []
    for index, part in enumerate(parts):
        key = f"{envelope['id']}:{index}"
        chunk = {'v': 1, 'kind': 'chunk', 'id': key.replace(':', '_'),
                 'serverEpoch': envelope['serverEpoch'], 'transferId': transfer,
                 'eventId': envelope['id'], 'index': index, 'total': len(parts),
                 'sha256': hashlib.sha256(raw).hexdigest(),
                 'data': base64.b64encode(part).decode('ascii')}
        text = canonical(chunk).decode('utf-8')
        if len(text) > 2000 or len(key) > 64:
            raise ValueError('wire limit exceeded')
        output.append((key, text))
    return output


def reassemble(messages):
    """Pure fixture helper. Production must persist/authenticate before calling."""
    pieces = {}
    metadata = None
    for _, text in messages:
        chunk = loads_business(text)
        if chunk['kind'] != 'chunk':
            if len(messages) != 1:
                raise ValueError('mixed direct and chunk transfer')
            return chunk
        stamp = tuple(chunk[k] for k in ('serverEpoch', 'transferId', 'eventId', 'total', 'sha256'))
        if metadata is None:
            metadata = stamp
        if stamp != metadata or not 1 <= chunk['total'] <= 86 or not 0 <= chunk['index'] < chunk['total']:
            raise ValueError('CHUNK_INVALID')
        data = base64.b64decode(chunk['data'], validate=True)
        if len(data) > 768 or (chunk['index'] in pieces and pieces[chunk['index']] != data):
            raise ValueError('CHUNK_INVALID')
        pieces[chunk['index']] = data
    if metadata is None or len(pieces) != metadata[3]:
        raise ValueError('CHUNK_INCOMPLETE')
    raw = b''.join(pieces[i] for i in range(metadata[3]))
    if len(raw) > 65536 or hashlib.sha256(raw).hexdigest() != metadata[4]:
        raise ValueError('CHUNK_INVALID')
    result = loads_business(raw.decode('utf-8'))
    if result['id'] != metadata[2] or result['serverEpoch'] != metadata[0]:
        raise ValueError('CHUNK_INVALID')
    return result


def main():
    schema = read('business.schema.json')
    Draft202012Validator.check_schema(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    validate_spec(read('cloud.openapi.json'))
    cloud = read('cloud.openapi.json')
    for case in read('cloud-fixtures.json')['cases']:
        cloud_validator = Draft202012Validator({'components':cloud['components'], '$ref':'#/components/schemas/' + case['schema']})
        assert cloud_validator.is_valid(case['payload']), case['name']
    cases = read('fixtures.json')['cases']
    failures = []
    by_name = {case['name']: case['payload'] for case in cases}
    for case in cases:
        errors = list(validator.iter_errors(case['payload']))
        if bool(errors) == case['valid']:
            failures.append(case['name'] + ': ' + (errors[0].message[:180] if errors else 'unexpected acceptance'))
    catalog = read('commands.json')['commands']
    for command in catalog:
        name = command['name']
        assert 'request_' + name in by_name and 'ack_' + name in by_name
        assert command['request'].split('/')[-1] in schema['$defs']
        assert command['result'].split('/')[-1] in schema['$defs']
        request, ack = by_name['request_' + name], by_name['ack_' + name]
        assert ack['data']['refId'] == request['id']
        assert ack['data']['clientMsgId'] == request['data']['clientMsgId']
        assert ack['data']['requestHash'] == digest(request['data'])
        assert request['id'] != request['data']['clientMsgId']
    for source in read('cloud-source-lock.json')['files']:
        path = ROOT / source['path']
        assert hashlib.sha256(path.read_bytes()).hexdigest() == source['sha256'], source['path'] + ' source changed: review cloud contract'
    for record in read('evidence-vectors.json')['records']:
        raw = b''.join(base64.b64decode(x, validate=True) for x in record['parts'])
        assert len(raw) <= 65536 and len(record['parts']) <= 16
        assert all(len(base64.b64decode(x)) <= 4096 for x in record['parts'])
        assert hashlib.sha256(raw).hexdigest() == record['payloadHash']
        decoded = loads_business(raw.decode('utf-8'))
        evidence_validator = Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/Evidence_' + record['sourceType']})
        assert evidence_validator.is_valid(decoded), record['name']
        assert decoded == record['payload']
    evidence_request = by_name['request_recovery_evidence']['data']
    evidence_bytes = base64.b64decode(evidence_request['bytesBase64'], validate=True)
    assert hashlib.sha256(evidence_bytes).hexdigest() == evidence_request['payloadHash']
    decoded = loads_business(evidence_bytes.decode('utf-8'))
    assert decoded == by_name['request_record_execution']
    assert evidence_request['evidenceEpoch'] == decoded['serverEpoch']
    scenarios = read('sync-scenarios.json')['cases']
    forward = scenarios[0]
    history_cursor = forward['initialHistoryCursor']
    assert history_cursor == forward['expectedAfterMessageId']
    assert int(max(forward['persistedWsIds'], key=int)) > int(history_cursor)
    applied = sorted(forward['historyPageDescending'], key=int)
    assert applied == forward['expectedApplyOrder']
    assert applied[-1] == forward['expectedHistoryCursor']
    assert scenarios[1]['expectedHistoryCursor'] == scenarios[1]['initialHistoryCursor']
    vectors = read('hash-vectors.json')
    v = vectors['business']
    assert canonical(v['input']).decode() == v['canonicalUtf8'] and digest(v['input']) == v['sha256']
    hv = vectors['hmac']
    body_hash = base64.urlsafe_b64encode(hashlib.sha256(hv['bodyUtf8'].encode()).digest()).decode().rstrip('=')
    expected = f"method=POST&path=/api/auth/internal/token&bodyHash={body_hash}&ts={hv['timestamp']}&nonce={hv['nonce']}"
    assert expected == hv['canonical']
    actual = base64.urlsafe_b64encode(hmac.new(hv['testSecret'].encode(), expected.encode(), hashlib.sha256).digest()).decode().rstrip('=')
    assert actual == hv['signature']
    pages = [by_name['snapshot_page_0']['data'], by_name['snapshot_page_1']['data']]
    for name, info in pages[0]['manifest'].items():
        values = [pages[0]['meta']] if name == 'meta' else [item for page in pages for item in page[name]]
        assert len(values) == info['count'] and digest(values) == info['sha256'], name
    max_wire = 0
    for case in cases:
        if not case['valid']:
            continue
        env = case['payload']
        assert env['serverEpoch'] == env['data']['serverEpoch'] if env['kind'] == 'command' else True
        encoded = encode(env)
        max_wire = max(max_wire, max(len(body) for _, body in encoded))
        for _, body in encoded:
            if not validator.is_valid(json.loads(body)):
                failures.append(case['name'] + ': invalid encoded body')
        shuffled = list(reversed(encoded)) + encoded[:1] if len(encoded) > 1 else encoded
        assert reassemble(shuffled) == env
    push_validator = Draft202012Validator({'$defs':schema['$defs'], '$ref':'#/$defs/Push'})
    push_validator.validate(read('push.example.json'))
    # Real transport boundaries: one over-limit codepoint, full 64KiB,
    # 86 parts, duplicates, missing parts, corrupt hash and conflicting bytes.
    boundary = copy.deepcopy(by_name['reply'])
    boundary['id'] = 'e' * 61
    boundary['data']['text'] = ''
    empty = len(canonical(boundary))
    boundary['data']['text'] = 'x' * (65536 - empty)
    encoded = encode(boundary)
    assert len(encoded) == 86 and reassemble(encoded) == boundary
    assert all(len(key) <= 64 and len(body) <= 2000 for key, body in encoded)
    def rejected(action):
        try:
            action()
        except (ValueError, UnicodeError):
            return
        raise AssertionError('invalid input was accepted')
    over = copy.deepcopy(boundary); over['data']['text'] += 'x'
    rejected(lambda: encode(over))
    rejected(lambda: reassemble(encoded[:-1]))
    corrupt = json.loads(encoded[0][1]); corrupt['data'] = base64.b64encode(b'wrong').decode()
    rejected(lambda: reassemble([(encoded[0][0], json.dumps(corrupt)), *encoded[1:]]))
    rejected(lambda: reassemble([*encoded, (encoded[0][0], json.dumps(corrupt))]))
    rejected(lambda: canonical({'amount':10.2}))
    rejected(lambda: canonical({'id':9007199254740992}))
    rejected(lambda: loads_business('{"qty":1,"qty":2}'))
    rejected(lambda: loads_business('{"qty":-0}'))
    rejected(lambda: loads_business('{"qty":1e0}'))
    # Unknown protocol major must fail producer schema validation.
    newer = copy.deepcopy(by_name['request_act']); newer['v'] = 2
    assert not validator.is_valid(newer)
    if failures:
        raise AssertionError('\n'.join(failures))
    report = {'commands':len(catalog),'fixtures':len(cases),'positive':sum(x['valid'] for x in cases),
              'negative':sum(not x['valid'] for x in cases),'cloudOperations':sum(len(x) for x in read('cloud.openapi.json')['paths'].values()),
              'cloudDtoFixtures':len(read('cloud-fixtures.json')['cases']),'evidenceVectors':len(read('evidence-vectors.json')['records']),
              'lockedSourceFiles':len(read('cloud-source-lock.json')['files']),'maxFixtureWireCharacters':max_wire,
              'maxBoundaryParts':len(encoded),'result':'PASS (contract checks only; no live services)'}
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    main()
