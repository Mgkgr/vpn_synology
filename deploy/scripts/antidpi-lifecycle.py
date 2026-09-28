"""Pure recovery decisions for the isolated probe, not an installed root worker."""
import re


def recovery_plan(owner, socks, images, run_id, host_namespace, last_repair, now):
    if not isinstance(run_id, str) or not re.fullmatch(r'[a-f0-9]{32}', run_id):
        raise ValueError('reviewed probe identity required')
    for role, state in (('engine', owner), ('socks', socks)):
        if not isinstance(state, dict):
            raise ValueError('container inspection is unavailable')
        labels = state.get('labels', {})
        if (not re.fullmatch(r'[a-f0-9]{64}', state.get('id', ''))
                or state.get('image') != images.get(role)
                or not re.fullmatch(r'sha256:[a-f0-9]{64}', images.get(role, ''))
                or labels.get('vpn.dashboard.scope') != 'antidpi-offline-probe'
                or labels.get('vpn.dashboard.run') != run_id
                or labels.get('vpn.dashboard.role') != role
                or type(state.get('running')) is not bool
                or type(state.get('healthy')) is not bool):
            raise ValueError('untrusted staging container identity')
        if state['running'] and (type(state.get('namespace')) is not int
                                 or state['namespace'] <= 0 or state['namespace'] == host_namespace):
            raise ValueError('isolated namespace evidence is missing')
    if (owner.get('network') != 'none'
            or not re.fullmatch(r'container:[a-f0-9]{64}', socks.get('network', ''))
            or owner['id'] == socks['id']):
        raise ValueError('unexpected staging network mode')
    if not owner['running'] or not owner['healthy']:
        return (['stop_socks'] if socks['running'] else []) + ['wait_engine']
    if (socks['running'] and socks['healthy'] and owner['namespace'] == socks['namespace']
            and socks['network'] == 'container:' + owner['id']):
        return []
    plan = ['stop_socks'] if socks['running'] else []
    if last_repair is not None and now - last_repair < 300:
        return plan + ['cooldown']
    # Restart alone can preserve a dead owner reference. Always recreate the joiner.
    return plan + ['recreate_socks']
