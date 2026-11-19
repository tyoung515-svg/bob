"""Factual nonempty Builder fallback, without another model/provider call."""
import json


def assistant_text(content):
    if isinstance(content,str):
        return content if content.strip() else ''
    if isinstance(content,list):
        return ''.join(block.get('text','') for block in content
                       if isinstance(block,dict) and block.get('type') in {'text','output_text'}
                       and isinstance(block.get('text'),str)).strip()
    return ''


def summary(receipts):
    if not receipts:
        return 'The Builder finished without writing a report. No tool results were recorded in this turn.'
    lines = []
    for name, content in receipts[-8:]:
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            lines.append(name + ': ' + str(content)[:180])
            continue
        if not isinstance(value, dict):
            lines.append(name + ': ' + str(value)[:180])
            continue
        error = value.get('error')
        if isinstance(error, dict) and error.get('code') == 'approval_expired':
            lines.append(name + ': approval expired; this action was not executed by this turn.')
        elif value.get('status') == 'proposed':
            lines.append(name + ': proposed; awaiting Apply.\n' + str(value.get('block') or ''))
        elif value.get('error'):
            lines.append(name + ': ' + str(value['error']))
        elif value.get('end_turn'):
            lines.append(name + ': ' + str(value.get('status','pending')) + '; ' + str(value.get('detail') or ''))
        elif 'exit_code' in value:
            lines.append(name + ': exit code ' + str(value['exit_code']) +
                         ('; output truncated' if value.get('truncated') else ''))
        else:
            lines.append(name + ': ' + str(value.get('status','result recorded')))
    return 'The Builder finished without writing a report.\n' + '\n'.join(lines)
