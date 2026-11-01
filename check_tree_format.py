#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_tree_format.py — nbt-tree-format 的辅助校验脚本。

扫描给定 Markdown 文件里所有 ```md 树块，按本技能的输出规则做机器校验：
缩进网格、竖线贯穿、分支行（`↳`）、成员平铺（`（仅 …）`，含同一层多个判别键时
按条件里的键认领多态行、跨键合取按键分别认领）、键集开放（`（开放键集，键为 …）`）、
「条件「名称」」引用（须在树块外用「条件「名称」：…」定义）、复合标签子行、
行尾反斜杠、节点形状等。
脚本只做「形态」检查，不判断类型/说明是否符合来源，那部分仍需人工自检。

用法：
    python check_tree_format.py 报告.md
    python check_tree_format.py 报告.md 其它.md -o check-report.txt
    python check_tree_format.py docs/ --json check.json
    python check_tree_format.py 报告.md -o check.txt --ignore-non-tree --quiet

退出码：0 = 无问题；1 = 发现违规；2 = 参数或读写错误。
"""

import argparse
import io
import json
import os
import re
import sys

# 类型符号（与 SKILL.md 的「类型符号」一节一致）
TYPES = {'{}', '[]', '[B]', '[I]', '[L]', 'B', 'S', 'I', 'L', 'F', 'D', 'T/F', '""'}

BRANCH = '\u21b3'   # ↳
TEE = '\u251c'      # ├
ELBOW = '\u2514'    # └
BAR = '\u2502'      # │
DASH = '\u2500'     # ─

FENCE_RE = re.compile(r'^(`{3,})([^`]*)$')
# 节点解析见 split_node（类型符号 + 可选名称 + 可选条件段 + 冒号 + 说明）

RELAX_CHILDREN = ('\u7a7a', '\u9012\u5f52', '\u5206\u652f\u89c1')  # 空 / 递归 / 分支见

# 成员平铺（对象级 / 键级多态）：多态行写「（n 选一，判别键 X，成员平铺本级）」
VARIANT_PAREN_RE = re.compile('\uff08[^\uff09]*\u6210\u5458\u5e73\u94fa[^\uff09]*\uff09')
VARIANT_MARKER_RE = re.compile(
    '\uff08([^\uff09]*?)\u9009\u4e00\uff0c\u5224\u522b\u952e\\s*`([^`]+)`([^\uff09]*)\uff09')
COND_KV_NEG_RE = re.compile('(!)\\s*`([^`]+)`\\s*=\\s*`([^`]+)`')    # 取反的键值
COND_KV_RE = re.compile('`([^`]+)`\\s*=\\s*`([^`]+)`')
COND_REF_RE = re.compile('\u6761\u4ef6\u300c([^\u300d]+)\u300d')   # 条件「名称」
COND_DEF_RE = re.compile('^\\s*(?:[-*]\\s*)?\u6761\u4ef6\u300c([^\u300d]+)\u300d\\s*: ')
COND_SECTION_RE = re.compile('^####\\s*\u6761\u4ef6\u300c([^\u300d]+)\u300d\\s*$')  # #### 条件「名称」小节
TOKEN_RE = re.compile('`([^`]+)`')
EMPTY_DECL_RE = re.compile('`([^`]+)`\\s*\u65e0\u9644\u52a0\u6210\u5458')  # `x` 无附加成员
# 键集开放（键名由数据决定、无法枚举）：`{}` 行标「（开放键集，键为 …）」，不必给子行
OPEN_KEYSET_RE = re.compile('\uff08\u5f00\u653e\u952e\u96c6\uff0c\u952e\u4e3a[^\uff09]+\uff09')


class Report:
    def __init__(self):
        self.problems = []
        self.files = 0
        self.blocks = 0
        self.io_error = False

    def add(self, path, line, rule, message):
        self.problems.append({'file': path, 'line': line, 'rule': rule, 'message': message})

    def sorted(self):
        return sorted(self.problems, key=lambda p: (p['file'], p['line'], p['rule']))


def read_text(path, encoding):
    with io.open(path, 'r', encoding=encoding, errors='replace') as f:
        return f.read().split('\n')


def extract_md_blocks(lines):
    """返回 ([(首行行号, 行列表), ...], 未闭合的围栏栈)。

    只有「恰好 3 个反引号 + 语言 md」的围栏才会被当作树块；
    ```` 包裹层、```markdown 等其它围栏只用于正确配对，不收集内容。
    """
    blocks = []
    stack = []  # (反引号数量, 语言, collector or None)
    for i, ln in enumerate(lines, 1):
        m = FENCE_RE.match(ln)
        if m:
            count = len(m.group(1))
            lang = m.group(2).strip()
            if stack and count >= stack[-1][0]:
                _n, _lang, collector, _ln = stack.pop()
                if collector is not None:
                    blocks.append(collector)
                continue
            collector = (i + 1, []) if (count == 3 and lang == 'md') else None
            stack.append((count, lang, collector, i))
            continue
        if stack:
            for _n, _lang, collector, _ln in reversed(stack):
                if collector is not None:
                    collector[1].append(ln)
                    break
    return blocks, stack



def split_node(content):
    """解析节点内容：type(s) [**name**] [condition]：description。

    返回 dict(types, name, condition, desc)；无法识别时返回 None。
    条件段是名称之后、冒号之前的部分，可为 None。
    """
    if ': ' not in content:
        return None
    idx = content.find(': ')
    if idx < 0:
        return None
    left, desc = content[:idx], content[idx + 2:]
    rest = left.strip()
    types = []
    while rest.startswith('`'):
        m = re.match(r'`([^`]+)`', rest)
        if not m or m.group(1) not in TYPES:
            break
        types.append(m.group(1))
        rest = rest[m.end():].lstrip()
    if not types:
        return None
    name = None
    if rest.startswith('**'):
        m = re.match(r'\*\*(.+?)\*\*', rest)
        if not m:
            return None
        name = m.group(1)
        rest = rest[m.end():].lstrip()
    condition = rest.strip() or None
    return {'types': types, 'name': name, 'condition': condition, 'desc': desc.strip()}


COND_DESC_RE = re.compile('「([^」]+)」')


def parse_condition(cond):
    """解析条件段；返回 (pairs, refs, error)。

    pairs 为 (key, value, negated)；refs 为命名条件名称。
    语法：项为 `key`=`value`、比较项、条件「名称」，或「描述片段」；
    用 || && ! 与括号组合（C 语义）。描述片段不参与判别键认领。
    """
    if not cond:
        return [], [], None
    if cond.count('「') != cond.count('」'):
        return [], [], '条件里的「」没有配对'
    refs = [m.group(1) for m in COND_REF_RE.finditer(cond)]
    rest = COND_REF_RE.sub(' ', cond)
    rest = COND_DESC_RE.sub(' ', rest)
    pairs = []
    for m in COND_KV_NEG_RE.finditer(rest):
        pairs.append((m.group(2), m.group(3), True))
    rest = COND_KV_NEG_RE.sub(' ', rest)
    for m in COND_KV_RE.finditer(rest):
        pairs.append((m.group(1), m.group(2), False))
    return pairs, refs, None


BRANCH_TYPE_RE = re.compile(r'^((?:`[^`]+`\s*)+?)`([^`]+)`=(.*)$')


def parse_branch_content(content):
    """解析分支行内容「type(s) `key`=value：description」，返回 (info, error)。"""
    if ': ' not in content:
        return None, 'branch row must contain ": "'
    idx = content.find(': ')
    if idx < 0:
        return None, 'branch row must contain ": "'
    left, desc = content[:idx], content[idx + 2:]
    m = BRANCH_TYPE_RE.match(left.strip())
    if not m:
        return None, 'branch row must be "type(s) `key`=value"'
    tokens = re.findall(r'`([^`]+)`', m.group(1))
    if not tokens:
        return None, 'branch row needs type symbol(s)'
    bad = [t for t in tokens if t not in TYPES]
    if bad:
        return None, 'unknown type symbol(s) in branch row: %s' % ', '.join(bad)
    if not desc.strip():
        return None, 'branch row has empty description'
    return {'types': tokens, 'key': m.group(2), 'value': m.group(3).strip()}, None


def parse_block(path, start, block, report):
    """解析一个树块并登记问题；返回解析后的行信息列表。

    列位模型：子列 = 父列 + 4；父是分支行（`↳`）时子列 = 父列 + 3，
    因此分支子树会离开 4 的网格，但分支结束后立刻回到网格。
    """
    entries = []          # dict(depth, col, branch, sym, content, body, has_more, parent)
    stack = []            # 祖先链（含根）
    for k, raw in enumerate(block):
        ln = start + k
        if not raw.strip():
            report.add(path, ln, 'blank', 'blank line inside tree block')
            continue
        if not raw.endswith('\\'):
            report.add(path, ln, 'backslash', 'missing trailing backslash')
        body = raw[:-1]
        if body != body.rstrip():
            report.add(path, ln, 'backslash', 'trailing whitespace before backslash')

        if k == 0:
            root = split_node(body)
            if root and body == body.lstrip():
                entry = {'depth': -1, 'col': 0, 'branch': False, 'sym': None, 'content': body,
                         'body': body, 'has_more': False,
                         'name': root['name'], 'types': root['types'],
                         'condition': root['condition'], 'parent': None}
                entries.append(entry)
                stack = [entry]
                continue
            if body.startswith(' ') and len(body) > 1 and body[1] in (TEE, ELBOW, BRANCH):
                pass  # 片段示例：首行就是第 1 层
            else:
                report.add(path, ln, 'root', 'root line must be `symbol`[ **name**]：description at column 0')
                entry = {'depth': -1, 'col': 0, 'branch': False, 'sym': None, 'content': body,
                         'body': body, 'has_more': False,
                         'name': None, 'parent': None}
                entries.append(entry)
                stack = [entry]
                continue

        m = re.match(r'^([ \u2502]*)([\u251c\u2514\u21b3])(.?)', body)
        if not m:
            report.add(path, ln, 'tree-symbol', 'line has no tree symbol (├─/└─/↳)')
            entries.append({'depth': -1, 'col': 0, 'branch': False, 'sym': None, 'content': body,
                            'body': body, 'has_more': False,
                            'name': None, 'parent': None})
            continue
        sym_idx = len(m.group(1))
        sym = m.group(2)
        after = m.group(3)
        is_branch = sym == BRANCH
        while stack and stack[-1]['col'] >= sym_idx:
            stack.pop()
        parent = stack[-1] if stack else None
        if parent is None or parent['depth'] < 0:
            depth = 1
            want_col = 1
        else:
            depth = parent['depth'] + 1
            want_col = parent['col'] + (3 if parent['branch'] else 4)
        if sym_idx != want_col:
            report.add(path, ln, 'grid',
                       'symbol at column %d, expected %d (parent at column %d%s)'
                       % (sym_idx, want_col, parent['col'] if parent else 0,
                          '，父是分支行' if parent and parent['branch'] else ''))
        if is_branch:
            if after != ' ':
                report.add(path, ln, 'branch', 'branch row must be "↳ " (symbol then one space)')
            content = body[sym_idx + 2:]
        else:
            if after != DASH:
                report.add(path, ln, 'dash', 'missing "─" after %r' % sym)
            elif body[sym_idx + 2:sym_idx + 3] != ' ':
                report.add(path, ln, 'node', 'expected one space after "%s─"' % sym)
            content = body[sym_idx + 3:]
        if not content:
            report.add(path, ln, 'node', 'empty description')
        name = None
        types = None
        condition = None
        if is_branch:
            _parsed, err = parse_branch_content(content)
            if err:
                report.add(path, ln, 'branch', err)
            if '**' in content.split(': ')[0]:
                report.add(path, ln, 'branch', 'branch row must not have a bold name')
        else:
            node = split_node(content)
            if not node:
                report.add(path, ln, 'node', 'node must be `type` [**name**] [condition]：description')
            else:
                name = node['name']
                types = node['types']
                condition = node['condition']
                for token in types:
                    if token not in TYPES:
                        report.add(path, ln, 'type', 'unknown type symbol `%s`' % token)
                if condition:
                    _pairs, _refs, cerr = parse_condition(condition)
                    if cerr:
                        report.add(path, ln, 'condition', cerr)
        entry = {'depth': depth, 'col': sym_idx, 'branch': is_branch, 'sym': sym,
                 'content': content, 'body': body, 'has_more': False,
                 'name': name, 'types': types, 'condition': condition, 'parent': parent}
        entries.append(entry)
        stack.append(entry)
    return entries


def _backticks(text):
    return [t for t in TOKEN_RE.findall(text) if t not in TYPES]


def parse_variant_marker(content):
    """解析成员平铺标注；不是多态行返回 None，标注残缺返回 {'error': ...}。

    只有写成括号形式的「（…成员平铺…）」才算标注，散文里提到「成员平铺」不算。
    """
    if not VARIANT_PAREN_RE.search(content):
        return None
    m = VARIANT_MARKER_RE.search(content)
    if not m:
        return {'error': 'member-polymorphic row must read '
                         '"\uff08n \u9009\u4e00\uff0c\u5224\u522b\u952e `X`\uff0c'
                         '\u6210\u5458\u5e73\u94fa\u672c\u7ea7\uff09"'}
    return {'key': m.group(2), 'start': m.start(), 'end': m.end()}


def raw_condition_pairs(text):
    """把「（仅 …）」里的条件解析成 [(key 或 None, value), ...]。

    - 显式写法「（仅 `X`=`v`）」：key 是 X；
    - 同一键的多个取值「（仅 `X`=`a`、`b`）」：裸值归给它前面最近的那个显式键；
    - 跨键合取「（仅 `X`=`a` 且 `Y`=`b`）」：每个键各自成对；
    - 简写「（仅 `v`）」：key 为 None。
    """
    kv = [(m.group(1), m.group(2), m.start(), m.end()) for m in COND_KV_RE.finditer(text)]
    if not kv:
        return [(None, v) for v in _backticks(text)]
    pairs = [(key, value) for key, value, _s, _e in kv]
    for m in TOKEN_RE.finditer(text):
        if any(s <= m.start() < e for _k, _v, s, e in kv):
            continue
        token = m.group(1)
        if token in TYPES:
            continue
        target = None
        for key, _v, s, _e in kv:
            if s < m.start():
                target = key
        pairs.append((target, token))
    return pairs


def marker_domain(content, marker):
    """取判别键的取值集合与「`x` 无附加成员」声明；取不到时返回空集合（跳过取值检查）。"""
    key = marker['key']
    values = set()
    for part in (content[:marker['start']], content[marker['end']:]):
        if '\u4e4b\u4e00' in part:  # 「之一」前的反引号记号就是取值
            values = set(t for t in _backticks(part.split('\u4e4b\u4e00')[0]) if t != key)
            break
    declared_empty = set(m.group(1) for m in EMPTY_DECL_RE.finditer(content))
    return values, declared_empty


def check_variants(path, start, entries, report):
    """校验成员平铺：多态标注、条件段归属、取值集合、取值认领、与分支行互斥。"""
    markers = []
    for i, e in enumerate(entries):
        parsed = parse_variant_marker(e['content'])
        if parsed is None:
            continue
        if 'error' in parsed:
            report.add(path, start + i, 'variant', parsed['error'])
            continue
        parsed['index'] = i
        d = e['depth']
        parsed['content'] = e['content']
        if e.get('name') == parsed['key'] and d >= 1:
            parsed['member_depth'] = d
            scope_end = len(entries)
            for j in range(i + 1, len(entries)):
                if entries[j]['depth'] < d:
                    scope_end = j
                    break
        else:
            parsed['member_depth'] = 1 if d < 0 else d + 1
            scope_end = len(entries)
            for j in range(i + 1, len(entries)):
                if entries[j]['depth'] <= d:
                    scope_end = j
                    break
        parsed['scope_end'] = scope_end
        markers.append(parsed)

    for mk in markers:
        e = entries[mk['index']]
        mk['container'] = id(e['parent']) if (e.get('name') == mk['key'] and e['parent'] is not None) else id(e)

    claimed = {id(m): set() for m in markers}
    external_containers = set()
    for i, e in enumerate(entries):
        if e['branch'] or e['depth'] < 1:
            continue
        cond = e.get('condition')
        if not cond:
            continue
        candidates = [mk for mk in markers
                      if mk['index'] < i < mk['scope_end'] and e['depth'] == mk['member_depth']]
        pairs, refs, _err = parse_condition(cond)
        if refs and candidates:
            external_containers.add(candidates[-1]['container'])
        for key, value, negated in pairs:
            owner = None
            for cand in candidates:
                if cand['key'] == key:
                    owner = cand
            if owner is None:
                report.add(path, start + i, 'variant-member',
                           '条件里的判别键 `%s` 找不到对应的多态行' % key)
                continue
            allowed, _declared = marker_domain(owner['content'], owner)
            if not negated:
                claimed[id(owner)].add(value)
            if allowed and value not in allowed:
                report.add(path, start + i, 'variant-value',
                           'value `%s` is not a discriminator value (%s)'
                           % (value, ', '.join(sorted(allowed))))

    for mk in markers:
        for j in range(mk['index'] + 1, mk['scope_end']):
            other = entries[j]
            if other['branch'] and other['parent'] is entries[mk['index']]:
                report.add(path, start + j, 'variant-mode',
                           'branch row hangs off a member-polymorphic row')
                break
        if mk['container'] in external_containers:
            continue
        allowed, declared_empty = marker_domain(mk['content'], mk)
        if not allowed:
            continue
        missing = allowed - claimed[id(mk)] - declared_empty
        if missing:
            report.add(path, start + mk['index'], 'variant-claim',
                       'discriminator value(s) %s have no member row (write "… '
                       '无附加成员" if that variant is empty)'
                       % ', '.join(sorted(missing)))


def check_open_keyset(path, start, entries, report):
    """校验「（开放键集，键为 …）」：标注格式完整，且该行是复合标签。

    只在出现「（开放键集」时校验；散文里提到「开放键集」不算标注。
    """
    for i, e in enumerate(entries):
        if e['depth'] < 1 or e['branch']:
            continue
        if '\uff08\u5f00\u653e\u952e\u96c6' not in e['content']:
            continue
        if not OPEN_KEYSET_RE.search(e['content']):
            report.add(path, start + i, 'open-keyset',
                       '\u5f00\u653e\u952e\u96c6\u884c\u5fc5\u987b\u5199\u6210\u300c\uff08\u5f00\u653e\u952e\u96c6\uff0c\u952e\u4e3a \u2026\uff09\u300d')
            continue
        syms = e.get('types') or []
        if '{}' not in syms:
            report.add(path, start + i, 'open-keyset',
                       '\u5f00\u653e\u952e\u96c6\u884c\u5fc5\u987b\u662f\u590d\u5408\u6807\u7b7e\u884c\uff08`{}`\uff09')


def check_block(path, start, block, entries, report):
    # has_more：普通节点由符号决定；分支行看后面是否还有同层分支
    for i, e in enumerate(entries):
        if e['depth'] < 1:
            continue
        if not e['branch']:
            e['has_more'] = e['sym'] == TEE
        else:
            nxt = None
            for n in entries[i + 1:]:
                if 0 <= n['depth'] <= e['depth']:
                    nxt = n
                    break
            e['has_more'] = bool(nxt and nxt['depth'] == e['depth'] and nxt['branch'])

    # 祖先列位的竖线：沿 parent 链取每个祖先自己的列位
    for i, e in enumerate(entries):
        if e['depth'] < 1:
            continue
        bars = {}
        anc = e['parent']
        while anc is not None and anc['depth'] >= 0:
            bars[anc['col']] = BAR if anc['has_more'] else ' '
            anc = anc['parent']
        for pos in range(0, e['col']):
            expect = bars.get(pos, ' ')
            c = e['body'][pos] if pos < len(e['body']) else ' '
            if c != expect:
                report.add(path, start + i, 'ancestor' if pos in bars else 'grid',
                           'column %d is %r, expected %r' % (pos, c, expect))

    # 复合标签 / 列表必须有子行（混合元素列表的元素行是多类型行，天然不需要子行）
    for i, e in enumerate(entries):
        if e['depth'] < 1 or e['branch']:
            continue
        syms = e.get('types') or []
        if syms not in (['{}'], ['[]']):
            continue
        if len(block) == 1:
            continue
        nxt_depth = entries[i + 1]['depth'] if i + 1 < len(entries) else -2
        if nxt_depth > e['depth']:
            continue
        if (any(t in e['content'] for t in RELAX_CHILDREN)
                or '\uff08\u5f00\u653e\u952e\u96c6' in e['content']
                or OPEN_KEYSET_RE.search(e['content'])):
            continue
        report.add(path, start + i, 'children',
                   'container row has no child row: %s' % e['content'][:40])

    # 混合元素列表：元素行要么一行并列多个类型，要么按类型分行（每行一个类型）
    mixed_kids = {}
    for i, e in enumerate(entries):
        if e['depth'] < 1 or e['branch']:
            continue
        parent = e['parent']
        if parent is None or '\uff08\u6df7\u5408\u5143\u7d20\uff09' not in parent['content']:
            continue
        if e['depth'] != parent['depth'] + 1:
            continue
        types = [t for t in re.findall(r'`([^`]+)`', e['content'].split(': ')[0]) if t in TYPES]
        mixed_kids.setdefault(id(parent), []).append((i, e, types))
    for kids in mixed_kids.values():
        if len(kids) == 1:
            i, e, types = kids[0]
            if len(types) < 2:
                report.add(path, start + i, 'mixed',
                           'single element row of a mixed list needs 2+ type symbols '
                           '(otherwise write one row per type)')
            else:
                nxt = entries[i + 1]['depth'] if i + 1 < len(entries) else -2
                if nxt > e['depth']:
                    report.add(path, start + i, 'mixed',
                               'combined element row must not have child rows; '
                               'write one row per type to expand fields')
        else:
            for i, e, types in kids:
                if len(types) != 1:
                    report.add(path, start + i, 'mixed',
                               'per-type element rows of a mixed list must list exactly one type symbol')

    # 分支行必须有「n 选一」父行
    for i, e in enumerate(entries):
        if not e['branch']:
            continue
        parent = e['parent']
        if parent is None or parent['depth'] < 0:
            report.add(path, start + i, 'branch-parent', 'branch row has no parent row')
        elif '\u9009\u4e00' not in parent['content']:
            report.add(path, start + i, 'branch-parent',
                       'parent row lacks "（n 选一，判别键 X）"')

    # 「判别键 `X`」：裸名要求同层有该行；路径写法视为位置已明确
    for i, e in enumerate(entries):
        if e['depth'] < 1 or e['branch']:
            continue
        m = re.search('\u5224\u522b\u952e\\s*`([^`]+)`', e['content'])
        if not m:
            continue
        key = m.group(1)
        if '.' in key or key.startswith('[root]'):
            continue
        siblings = [n for n in entries if n['depth'] == e['depth']]
        if not any(n.get('name') == key for n in siblings):
            report.add(path, start + i, 'discriminator',
                       'discriminator `%s` is not a sibling row; write a full path '
                       '(e.g. `tag.%s`) if it is defined elsewhere' % (key, key))

    check_open_keyset(path, start, entries, report)
    check_variants(path, start, entries, report)


OUTER_TITLE_RE = re.compile(r'^#### .+ 分支$')
OUTER_ITEM_RE = re.compile(r'^- ((?:`[^`]+`\s*)+?)`([^`]+)`=(.+?): (.+)$')


def check_outer_section(path, start, block, report):
    """校验树外分支小节块（普通 Markdown 列表，不是树）。"""
    first = block[0].strip()
    if not OUTER_TITLE_RE.match(first):
        report.add(path, start, 'outer', 'outer section must start with "#### <name> 分支"')
    has_key = False
    for k, raw in enumerate(block):
        if k == 0:
            continue
        s = raw.strip()
        if not s:
            continue
        if s.startswith('判别键: '):
            has_key = True
            if '`' not in s:
                report.add(path, start + k, 'outer',
                           'discriminator line must contain a full path in backticks')
            continue
        if s.startswith('- '):
            m = OUTER_ITEM_RE.match(s)
            if not m:
                report.add(path, start + k, 'outer',
                           'branch item must be "- type(s) `key`=value：description"')
                continue
            for token in re.findall(r'`([^`]+)`', m.group(1)):
                if token not in TYPES:
                    report.add(path, start + k, 'type', 'unknown type symbol `%s`' % token)
            continue
        if s.startswith('#'):
            continue
        report.add(path, start + k, 'outer', 'unexpected line in outer section')
    if not has_key:
        report.add(path, start, 'outer', 'outer section missing "判别键：" line')


def detect_cond_cycles(defs):
    """检测命名条件定义之间的循环引用，返回环上的名称列表或 None。"""
    graph = {}
    for name, (_ln, body) in defs.items():
        graph[name] = [m.group(1) for m in COND_REF_RE.finditer(body or '')]
    state = {}

    def visit(node, stack):
        state[node] = 1
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if state.get(nxt) == 1:
                idx = stack.index(nxt) if nxt in stack else 0
                return stack[idx:] + [nxt]
            if state.get(nxt, 0) == 0:
                found = visit(nxt, stack + [nxt])
                if found:
                    return found
        state[node] = 2
        return None

    for name in graph:
        if state.get(name, 0) == 0:
            found = visit(name, [name])
            if found:
                return found
    return None


def collect_files(paths):
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in sorted(names):
                    if n.lower().endswith('.md'):
                        files.append(os.path.join(root, n))
        else:
            files.append(p)
    return files


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='check_tree_format.py',
        description='Validate ```md tree blocks against the nbt-tree-format skill rules.')
    parser.add_argument('paths', nargs='+', metavar='FILE',
                        help='Markdown file(s) or directories (directories are searched recursively for *.md)')
    parser.add_argument('-o', '--output', metavar='FILE',
                        help='write the plain-text report to FILE instead of stdout')
    parser.add_argument('--json', metavar='FILE',
                        help='also write a JSON report to FILE')
    parser.add_argument('--encoding', default='utf-8', metavar='ENC',
                        help='input file encoding (default: utf-8)')
    parser.add_argument('--ignore-non-tree', action='store_true',
                        help='skip ```md blocks that contain no tree rows')
    parser.add_argument('--quiet', action='store_true',
                        help='print only the summary line (and write the full report to -o/--json)')
    parser.add_argument('--exit-zero', action='store_true',
                        help='always exit with 0, even when problems are found')
    opts = parser.parse_args(argv)

    report = Report()
    for path in collect_files(opts.paths):
        try:
            lines = read_text(path, opts.encoding)
        except OSError as exc:
            report.io_error = True
            report.add(path, 0, 'io', 'cannot read file: %s' % exc)
            continue
        report.files += 1
        blocks, unclosed = extract_md_blocks(lines)
        for fence in unclosed:
            report.add(path, fence[3], 'fence', 'unclosed code fence')
        # 树块外的「条件「名称」：…」定义（单行形式）；树里的引用必须都能找到定义
        cond_defs = {}
        for idx, ln in enumerate(lines, 1):
            dm = COND_DEF_RE.match(ln)
            if dm:
                body = ln[dm.end():].strip()
                if dm.group(1) not in cond_defs or not cond_defs[dm.group(1)][1]:
                    cond_defs[dm.group(1)] = (idx, body)
        refs = []
        for start, block in blocks:
            if not block:
                report.add(path, start, 'blank', 'empty ```md block')
                continue
            first = next((ln for ln in block if ln.strip()), '')
            if first.strip().startswith('#### '):
                report.blocks += 1
                sm = COND_SECTION_RE.match(first.strip())
                if sm:
                    # `#### 条件「名称」` 小节：标题下的任意 Markdown 就是逻辑
                    name = sm.group(1)
                    body = [ln for ln in block[1:] if ln.strip()]
                    if not body:
                        report.add(path, start, 'condition',
                                   '条件「%s」小节是空的' % name)
                        cond_defs.setdefault(name, (start, ''))
                    else:
                        cond_defs.setdefault(name, (start, '\n'.join(body)))
                else:
                    check_outer_section(path, start, block, report)
                continue
            has_tree = bool(split_node(first[:-1] if first.endswith('\\') else first))
            has_tree = has_tree or any(any(s in ln for s in (TEE, ELBOW, BRANCH)) for ln in block)
            if opts.ignore_non_tree and not has_tree:
                continue
            if not has_tree:
                report.add(path, start, 'non-tree',
                           '```md block contains no tree rows (use --ignore-non-tree to skip)')
                continue
            report.blocks += 1
            entries = parse_block(path, start, block, report)
            check_block(path, start, block, entries, report)
            for k, raw_line in enumerate(block):
                for ref in COND_REF_RE.finditer(raw_line):
                    refs.append((start + k, ref.group(1)))
        # 引用校验放在所有块之后，条件小节写在前或在后都可以
        for line_no, name in refs:
            if name not in cond_defs:
                report.add(path, line_no, 'condition',
                           '条件「%s」没有定义（用「条件「%s」：…」单行或「#### 条件「%s」」小节）'
                           % (name, name, name))
            elif not cond_defs[name][1]:
                report.add(path, line_no, 'condition',
                           '条件「%s」的定义为空' % name)
        cycle = detect_cond_cycles(cond_defs)
        if cycle:
            report.add(path, cond_defs[cycle[0]][0], 'condition',
                       '命名条件存在循环引用：%s' % ' \u2192 '.join(cycle))

    problems = report.sorted()
    summary = 'nbt-tree-format check — %d file(s), %d tree block(s), %d problem(s)' % (
        report.files, report.blocks, len(problems))
    text_lines = [summary]
    for p in problems:
        text_lines.append('%s:%d: [%s] %s' % (p['file'], p['line'], p['rule'], p['message']))
    text = '\n'.join(text_lines) + '\n'

    if opts.output:
        try:
            with io.open(opts.output, 'w', encoding='utf-8', newline='\n') as f:
                f.write(text)
        except OSError as exc:
            sys.stderr.write('cannot write report: %s\n' % exc)
            return 2
        sys.stdout.write(summary + '\n')
    elif opts.quiet:
        sys.stdout.write(summary + '\n')
    else:
        sys.stdout.write(text)

    if opts.json:
        payload = {
            'summary': {'files': report.files, 'blocks': report.blocks, 'problems': len(problems)},
            'problems': problems,
        }
        try:
            with io.open(opts.json, 'w', encoding='utf-8', newline='\n') as f:
                f.write(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        except OSError as exc:
            sys.stderr.write('cannot write JSON report: %s\n' % exc)
            return 2

    if report.io_error:
        return 2
    if problems and not opts.exit_zero:
        return 1
    return 0


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
    sys.exit(main())
