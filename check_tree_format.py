#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_tree_format.py — nbt-tree-format 的辅助校验脚本。

扫描给定 Markdown 文件里所有 ```md 树块，按本技能的输出规则做机器校验：
缩进网格、竖线贯穿、分支行（`↳`）、复合标签子行、行尾反斜杠、节点形状等。
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
BRANCH_RE = re.compile(r'^`[^`]+`=')

RELAX_CHILDREN = ('\u7a7a', '\u9012\u5f52', '\u5206\u652f\u89c1')  # 空 / 递归 / 分支见


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



def parse_block(path, start, block, report, opts):
    """解析一个树块并登记问题；返回解析后的行信息列表。"""
    entries = []          # dict(depth, branch, sym, content, body, has_more, fragment)
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
                entries.append({'depth': -1, 'branch': False, 'sym': None, 'content': body,
                                'body': body, 'has_more': False, 'fragment': False,
                                'name': root.group(2)})
                continue
            if body.startswith(' ') and len(body) > 1 and body[1] in (TEE, ELBOW, BRANCH):
                pass  # 片段示例：首行就是第 1 层
            else:
                report.add(path, ln, 'root', 'root line must be `symbol`[ **name**]：description at column 0')
                entries.append({'depth': -1, 'branch': False, 'sym': None, 'content': body,
                                'body': body, 'has_more': False, 'fragment': True,
                                'name': None})
                continue

        m = re.match(r'^([ \u2502]*)([\u251c\u2514\u21b3])(.?)', body)
        if not m:
            report.add(path, ln, 'tree-symbol', 'line has no tree symbol (├─/└─/↳)')
            entries.append({'depth': -1, 'branch': False, 'sym': None, 'content': body,
                            'body': body, 'has_more': False, 'fragment': False,
                            'name': None})
            continue
        sym_idx = len(m.group(1))
        sym = m.group(2)
        after = m.group(3)
        is_branch = sym == BRANCH
        if (sym_idx - 1) % 4 != 0:
            report.add(path, ln, 'grid', 'symbol at column %d, not on the 4-space grid' % sym_idx)
        depth = (sym_idx - 1) // 4 + 1
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
        for lvl in range(1, depth):
            col = 1 + 4 * (lvl - 1)
            c = body[col] if col < len(body) else ' '
            if c not in (BAR, ' '):
                report.add(path, ln, 'ancestor', 'column %d is %r, expected "│" or space' % (col, c))
        name = None
        if is_branch:
            if not BRANCH_RE.match(content):
                report.add(path, ln, 'branch', 'branch row must start with `key`=value')
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
        entries.append({'depth': depth, 'branch': is_branch, 'sym': sym, 'content': content,
                        'body': body, 'has_more': False, 'fragment': False, 'name': name})
    return entries


def check_block(path, start, block, entries, report, opts):
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

    # 祖先列位的竖线
    for i, e in enumerate(entries):
        if e['depth'] < 1:
            continue
        for lvl in range(1, e['depth']):
            anc = None
            for j in range(i - 1, -1, -1):
                if entries[j]['depth'] == lvl:
                    anc = entries[j]
                    break
            if anc is None:
                report.add(path, start + i, 'ancestor', 'no ancestor row at level %d' % lvl)
                continue
            expect = BAR if anc['has_more'] else ' '
            col = 1 + 4 * (lvl - 1)
            c = e['body'][col] if col < len(e['body']) else ' '
            if c != expect:
                report.add(path, start + i, 'ancestor',
                           'column %d is %r, expected %r' % (col, c, expect))

    text = '\n'.join(e['body'] for e in entries)
    illustrate_mixed = '\uff08\u6df7\u5408\u5143\u7d20\uff09' in text

    # 复合标签 / 列表必须有子行
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
        if any(t in e['content'] for t in RELAX_CHILDREN) or illustrate_mixed:
            continue
        report.add(path, start + i, 'children',
                   'container row has no child row: %s' % e['content'][:40])

    # 分支行必须有「n 选一」父行
    for i, e in enumerate(entries):
        if not e['branch']:
            continue
        parent = None
        for j in range(i - 1, -1, -1):
            if entries[j]['depth'] == e['depth'] - 1:
                parent = entries[j]
                break
        if parent is None:
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
        siblings = [n for n in entries if n is not e and n['depth'] == e['depth']]
        if not any(n.get('name') == key for n in siblings):
            report.add(path, start + i, 'discriminator',
                       'discriminator `%s` is not a sibling row; write a full path '
                       '(e.g. `tag.%s`) if it is defined elsewhere' % (key, key))


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
            has_tree = bool(NODE_RE.match(first[:-1] if first.endswith('\\') else first))
            has_tree = has_tree or any(any(s in ln for s in (TEE, ELBOW, BRANCH)) for ln in block)
            if opts.ignore_non_tree and not has_tree:
                continue
            if not has_tree:
                report.add(path, start, 'non-tree',
                           '```md block contains no tree rows (use --ignore-non-tree to skip)')
                continue
            report.blocks += 1
            entries = parse_block(path, start, block, report, opts)
            check_block(path, start, block, entries, report, opts)

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
