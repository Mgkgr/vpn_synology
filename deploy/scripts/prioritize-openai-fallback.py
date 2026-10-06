"""Give the enabled OpenAI fallback policy precedence over broad AI pinning."""
import re


def prioritize_openai_fallback(configuration):
    rule = 'AND,((GEOSITE,openai),(RULE-SET,managed-fallback)),VPS-FALLBACK'
    if re.search(r'(?m)^\s*- ' + re.escape(rule) + r'\r?$', configuration):
        return configuration
    anchors = list(re.finditer(r'(?m)^([ \t]*)- RULE-SET,managed-wg-imp,WG-IMP\r?$', configuration))
    if len(anchors) != 1 or not re.search(r'(?m)^\s*- RULE-SET,managed-fallback,VPS-FALLBACK\r?$', configuration):
        raise ValueError('Expected unique managed rule anchors; no changes made')
    anchor = anchors[0]
    newline = '\r\n' if '\r\n' in configuration else '\n'
    return configuration[:anchor.start()] + anchor.group(1) + '- ' + rule + newline + configuration[anchor.start():]
