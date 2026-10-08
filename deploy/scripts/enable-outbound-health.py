"""Reviewed activation transaction; the NAS adapter supplies bounded side effects.

No implicit retry or rollback after an ambiguous reload. Diagnostic groups never
become client routing rules. Compatible with Python 3.8 on Synology.
"""
import re


class ActivationError(RuntimeError):
    pass


def enable_flag(source):
    try:
        text = source.decode('utf-8')
    except (UnicodeError, AttributeError):
        raise ActivationError('invalid_environment_encoding') from None
    newline = '\r\n' if '\r\n' in text else '\n'
    normalized = text.replace('\r\n', '\n')
    if '\r' in normalized or (newline == '\r\n' and '\n' in text.replace('\r\n', '')):
        raise ActivationError('invalid_environment_format')
    lines = normalized.splitlines(keepends=True)
    positions = [i for i, line in enumerate(lines)
                 if re.match(r'\s*(?:export\s+)?OUTBOUND_HEALTH_ENABLED\s*=', line)]
    if positions:
        if len(positions) != 1 or not re.fullmatch(
                r'OUTBOUND_HEALTH_ENABLED=(?:true|false)\n?', lines[positions[0]]):
            raise ActivationError('ambiguous_health_flag')
        lines[positions[0]] = 'OUTBOUND_HEALTH_ENABLED=true' + ('\n' if lines[positions[0]].endswith('\n') else '')
        normalized = ''.join(lines)
    else:
        normalized += ('' if not normalized or normalized.endswith('\n') else '\n') + 'OUTBOUND_HEALTH_ENABLED=true\n'
    return normalized.replace('\n', newline).encode('utf-8')


def activate(actions, transform):
    status = {'state': 'not_started', 'backup_id': None, 'error': None}
    actions.publish(status)
    mutation_started = False
    try:
        before = actions.read()
        after = {'config': transform(before['config']), 'env': enable_flag(before['env'])}
        receipt = actions.backup()
        if (receipt.get('result') != 'verified'
                or not re.fullmatch(r'[a-f0-9]{64}', receipt.get('snapshot_id', ''))):
            raise ActivationError('backup_not_verified')
        status['backup_id'] = receipt['snapshot_id']
        if actions.read() != before:
            raise ActivationError('concurrent_change')
        actions.validate(after['config'])
        if actions.read() != before:
            raise ActivationError('concurrent_change')
        status['state'] = 'validated'
        actions.publish(status)
        # An interrupted write/reload must never be reported as a rollback.
        mutation_started = True
        actions.write('config', before['config'], after['config'])
        status['state'] = 'applied'
        actions.publish(status)
        actions.reload()
        actions.write('env', before['env'], after['env'])
        actions.restart_dashboard()
        if actions.read() != after or actions.verify() is not True:
            raise ActivationError('postflight_not_verified')
        status['state'] = 'verified'
        actions.publish(status)
        return status
    except Exception as error:
        reason = str(error) if isinstance(error, ActivationError) and re.fullmatch(r'[a-z_]{1,80}', str(error)) else 'activation_failed'
        status.update(state='unknown' if mutation_started else 'not_started', error=reason)
        actions.publish(status)
        raise ActivationError(reason) from None
