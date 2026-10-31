# nbt-tree-format

把 NBT 层级信息（源码定义、NBT 文件、dump、日志或口述等）输出为「类型符号 + 加粗名称 + 中文冒号 + 说明」的树形 Markdown 大纲。

## 效果

```md
`{}` **tag**：父标签。\
 └─ `{}` **Fireworks**：烟花火箭的参数。\
     ├─ `B` **Flight**：（可能不存在，默认0）烟花火箭的飞行的时间，单位为“火药”（即表现为和工作台上合成烟花火箭时所用的火药数相等）。\
     └─ `[]` **Explosions**：（可能不存在）包含烟花火箭每次爆裂的数据，元素为复合标签。\
         └─ `{}`：一个烟火样式。\
             ├─ `[I]` **Colors**：（可能不存在）表示爆裂时的粒子颜色。\
             └─ `B` **Type**：（可能不存在）爆裂时的形态。
```

## 规则摘要

- 每行：`类型符号` → `**名称**` → `：` → 说明；名称加粗，用中文冒号。
- 类型符号：`{}` compound、`B` byte、`S` short、`I` int、`L` long、`F` float、`D` double、`T/F` boolean、`""` string、`[]` list、`[B]`/`[I]`/`[L]` 数组。
- 缩进：根节点顶格，第一层符号前 1 个空格，每深一层加 4 个空格；祖先为 `├─` 的列位画 `│` 并贯穿其子树。
- 只有 `{}` 和 `[]` 有子行；空集合也必须给出子行，写作 `` `{}`：（空） `` 或 `` `[]`：（空） ``。
- 可能不存在的节点，说明开头用「（可能不存在）」，不加逗号。
- 混合元素列表用「（混合元素列表）」标注。
- 整体输出必须包进 ```md 代码块，以保留缩进。

完整规则见 [SKILL.md](SKILL.md)。

## 安装

把本目录复制到 agent 的 skills 目录下：

```powershell
Copy-Item -Recurse -Force .\nbt-tree-format "$env:USERPROFILE\.agents\skills\nbt-tree-format"
```

也可以直接 `git clone` 到该目录。

## 许可

MIT，详见 [LICENSE](LICENSE)。
