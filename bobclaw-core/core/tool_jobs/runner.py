"""Installed deterministic report adapter; never executes model text or project code."""
import json
from pathlib import Path
import sys


def main():
    value = json.loads(sys.stdin.read(262145))
    if set(value) != {'role', 'content'} or value['role'] not in {'researcher','reviewer','proposer'}:
        raise ValueError('invalid report')
    content = value['content']
    if not isinstance(content, str) or len(content.encode()) > 240000:
        raise ValueError('report too large')
    if value['role'] == 'proposer':
        # The destination is fixed installed code, never a model-supplied path.
        path = Path('BOB-PROPOSAL.md')
        with path.open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
    print(json.dumps({'adapter': 'proposal-report/1', 'role': value['role'],
                      'content': content, 'executed_project_code': False}))


if __name__ == '__main__':
    main()
