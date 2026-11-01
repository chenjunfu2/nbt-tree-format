#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_tree_format.py — nbt-tree-format 的辅助校验脚本。

扫描给定 Markdown 文件里所有 ```md 树块，按本技能的输出规则做机器校验：
缩进网格、竖线贯穿、分支行（`↳`）、成员平铺（`（仅 …）`）、复合标签子行、
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
NODE_RE = re.compile(r'^((?:`[^`]+`\s*)+)(?:\*\*(.+?)\*\*)?\uff1a(.+)$')

RELAX_CHILDREN = ('\u7a7a', '\u9012\u5f52', '\u5206\u652f\u89c1')  # 空 / 递归 / 分支见

# 成员平铺（对象级 / 键级多态）：多态行写「（n 选一，判别键 X，成员平铺本级）」
VARIANT_PAREN_RE = re.compile('\uff08[^\uff09]*\u6210\u5458\u5e73\u94fa[^\uff09]*\uff09')
VARIANT_MARKER_RE = re.compile(
    '\uff08([^\uff09]*?)\u9009\u4e00\uff0c\u5224\u522b\u952e\\s*`([^`]+)`([^\uff09]*)\uff09')
COND_RE = re.compile('\uff08\u4ec5\\s*([^\uff09]+)\uff09')          # （仅 …）
COND_KV_RE = re.compile('`([^`]+)`\\s*=\\s*`([^`]+)`')
TOKEN_RE = re.compile('`([^`]+)`')
EMPTY_DECL_RE = re.compile('`([^`]+)`\\s*\u65e0\u9644\u52a0\u6210\u5458')  # `x` 无附加成员


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



BRANCH_TYPE_RE = re.compile(r'^((?:`[^`]+`\s*)+?)`([^`]+)`=(.*)$')


def parse_branch_content(content):
    """解析分支行内容「type(s) `key`=value：description」，返回 (info, error)。"""
    if '\uff1a' not in content:
        return None, 'branch row must contain "："'
    left, desc = content.split('\uff1a', 1)
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
            root = NODE_RE.match(body)
            if root and body == body.lstrip():
                entry = {'depth': -1, 'col': 0, 'branch': False, 'sym': None, 'content': body,
                         'body': body, 'has_more': False,
                         'name': root.group(2), 'parent': None}
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
        if is_branch:
            _parsed, err = parse_branch_content(content)
            if err:
                report.add(path, ln, 'branch', err)
            if '**' in content.split('\uff1a')[0]:
                report.add(path, ln, 'branch', 'branch row must not have a bold name')
        else:
            node = NODE_RE.match(content)
            if not node:
                report.add(path, ln, 'node', 'node must be `type` [**name**]：description')
            else:
                name = node.group(2)
                for token in re.findall(r'`([^`]+)`', node.group(1)):
                    if token not in TYPES:
                        report.add(path, ln, 'type', 'unknown type symbol `%s`' % token)
        entry = {'depth': depth, 'col': sym_idx, 'branch': is_branch, 'sym': sym,
                 'content': content, 'body': body, 'has_more': False,
                 'name': name, 'parent': parent}
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


def condition_pairs(text, default_key):
    """把「（仅 …）」里的条件解析成 [(key, value), ...]；简写「仅 `值`」用 default_key。"""
    pairs = [(m.group(1), m.group(2)) for m in COND_KV_RE.finditer(text)]
    if pairs:
        return pairs
    return [(default_key, v) for v in _backticks(text)]


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
    """校验成员平铺：多态标注、成员归属、取值集合、取值认领、与分支行互斥。"""
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
        # 判别键就是本行 -> 成员与它同级；否则成员是本行的子行
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

    claimed = {id(m): set() for m in markers}
    for i, e in enumerate(entries):
        if e['branch'] or e['depth'] < 1:
            continue
        m = COND_RE.search(e['content'])
        if not m or '`' not in m.group(1):
            continue  # 反引号之外的「（仅 …）」只是普通说明文字
        owner = None
        for mk in markers:
            if mk['index'] < i < mk['scope_end'] and e['depth'] == mk['member_depth']:
                owner = mk
        if owner is None:
            report.add(path, start + i, 'variant-member',
                       '\u300c\uff08\u4ec5 \u2026\uff09\u300d row has no member-polymorphic row')
            continue
        allowed, _declared = marker_domain(owner['content'], owner)
        for key, value in condition_pairs(m.group(1), owner['key']):
            if key != owner['key']:
                report.add(path, start + i, 'variant-member',
                           'condition key `%s` does not match the discriminator `%s`'
                           % (key, owner['key']))
                continue
            claimed[id(owner)].add(value)
            if allowed and value not in allowed:
                report.add(path, start + i, 'variant-value',
                           'value `%s` is not a discriminator value (%s)'
                           % (value, ', '.join(sorted(allowed))))

    for mk in markers:
        # 同一对象不得混用分支行与成员平铺（直接挂在多态行下的分支行即混用）
        for j in range(mk['index'] + 1, mk['scope_end']):
            other = entries[j]
            if other['branch'] and other['parent'] is entries[mk['index']]:
                report.add(path, start + j, 'variant-mode',
                           'branch row hangs off a member-polymorphic row')
                break
        # 判别键的每个取值都应有成员行认领，或显式声明无附加成员
        allowed, declared_empty = marker_domain(mk['content'], mk)
        if not allowed:
            continue
        missing = allowed - claimed[id(mk)] - declared_empty
        if missing:
            report.add(path, start + mk['index'], 'variant-claim',
                       'discriminator value(s) %s have no member row (write "\u2026 '
                       '\u65e0\u9644\u52a0\u6210\u5458" if that variant is empty)'
                       % ', '.join(sorted(missing)))


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
        syms = re.findall(r'`([^`]+)`', e['content'].split('\uff1a')[0])
        if syms not in (['{}'], ['[]']):
            continue
        if len(block) == 1:
            continue
        nxt_depth = entries[i + 1]['depth'] if i + 1 < len(entries) else -2
        if nxt_depth > e['depth']:
            continue
        if any(t in e['content'] for t in RELAX_CHILDREN):
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
        types = [t for t in re.findall(r'`([^`]+)`', e['content'].split('\uff1a')[0]) if t in TYPES]
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

    check_variants(path, start, entries, report)


OUTER_TITLE_RE = re.compile(r'^#### .+ 分支$')
OUTER_ITEM_RE = re.compile(r'^- ((?:`[^`]+`\s*)+?)`([^`]+)`=(.+?)\uff1a(.+)$')


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
        if s.startswith('判别键：'):
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
        for start, block in blocks:
            if not block:
                report.add(path, start, 'blank', 'empty ```md block')
                continue
            first = next((ln for ln in block if ln.strip()), '')
            if first.strip().startswith('#### '):
                report.blocks += 1
                check_outer_section(path, start, block, report)
                continue
            has_tree = bool(NODE_RE.match(first[:-1] if first.endswith('\\') else first))
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
