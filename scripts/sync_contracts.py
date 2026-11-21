"""Export pinned v2 contract snapshots; --check never writes files."""
from pathlib import Path
import argparse
import hashlib
import json

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ('local-service', 'local-service/relay', 'android-app')

def sync(root: Path, check: bool = False) -> None:
    source = root / 'contracts' / 'v2'
    files = sorted(source.glob('*.json'))
    if not files:
        raise ValueError('No canonical v2 contracts found')
    manifest = {'contractVersion': '2.0.0', 'protocolMajor': 2,
                'files': {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}}
    expected = {p.name: p.read_bytes() for p in files}
    expected['manifest.json'] = (json.dumps(manifest, indent=2)+'\n').encode()
    for project in PROJECTS:
        destination = root / project / 'contracts' / 'v2'
        if not check:
            destination.mkdir(parents=True, exist_ok=True)
        actual_names = {p.name for p in destination.glob('*.json')}
        extras = actual_names - expected.keys()
        if extras:
            raise ValueError(f'{project}: unexpected contract files {sorted(extras)}; review manually')
        for name, content in expected.items():
            target = destination / name
            if check:
                if not target.exists() or target.read_bytes() != content:
                    raise ValueError(f'{project}: stale or missing {name}; run scripts/sync_contracts.py')
            else:
                target.write_bytes(content)

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args()
    sync(ROOT, args.check)
    print('PASS: three v2 contract snapshots match' if args.check else 'Exported v2 to three projects')
