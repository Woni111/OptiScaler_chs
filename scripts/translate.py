#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
自动翻译新字符串（百度大模型文本翻译）并更新汉化文件
用法: python translate.py --input pending.json
流程:
  1. 读待翻译列表
  2. 调用百度大模型文本翻译 API
  3. 生成新 LK key → 更新 localization.h / lang_en.h / lang_zh_cn.h / strings_map.json
失败语义: 翻译失败直接 exit 1、不写任何文件（防英文兜底污染 strings_map 后永久跳过）
"""
import io
import json
import os
import re
import sys

from baidu_translate import BaiduTranslateError, translate_texts

# Windows 控制台默认 cp1252，强制 UTF-8 输出避免 UnicodeEncodeError
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')


HERE = os.path.dirname(os.path.abspath(__file__))
LOC_H = os.path.normpath(os.path.join(HERE, '..', 'overlay', 'OptiScaler', 'localization', 'localization.h'))
EN_H = os.path.normpath(os.path.join(HERE, '..', 'overlay', 'OptiScaler', 'localization', 'lang_en.h'))
ZH_H = os.path.normpath(os.path.join(HERE, '..', 'overlay', 'OptiScaler', 'localization', 'lang_zh_cn.h'))
MAP = os.path.join(HERE, 'strings_map.json')

def key_from_en(en):
    """英文串 → 合法 key 名"""
    words = re.findall(r'[A-Za-z0-9]+', en)
    name = '_'.join(words)[:60] or 'Str'
    if not name[0].isalpha():
        name = 'S_' + name
    return f'UI_{name}'

def c_lit(s):
    """译文 → C 字符串字面量内容。
    翻译 API 的 JSON 响应经 json.loads 后 \\n 会变成真实换行，直接写进
    头文件会让字符串常量跨行断掉（C2001，2026-09-10 nightly 编译事故）。
    模型也可能输出字面 \\n，先归一成真实换行再统一转义。
    仅用于 zh 侧：en 侧字符串本身就是上游 C 源码的字面量片段，透传即可。"""
    s = s.replace('\\n', '\n').replace('\\t', '\t')
    return (s.replace('\\', '\\\\')
             .replace('"', '\\"')
             .replace('\n', '\\n')
             .replace('\r', '\\r')
             .replace('\t', '\\t'))

def main():
    args = sys.argv[1:]
    inp = None
    if '--input' in args:
        inp = args[args.index('--input') + 1]
    if not inp:
        print('用法: translate.py --input pending.json')
        sys.exit(1)

    with io.open(inp, encoding='utf-8') as f:
        pending = json.load(f)
    if not pending:
        print('没有待翻译字符串')
        return

    try:
        zh_all = translate_texts(pending)
    except BaiduTranslateError as exc:
        print(f'!! 百度翻译失败: {exc}')
        sys.exit(1)

    if len(zh_all) != len(pending):
        print(f'!! 翻译数量不匹配: {len(zh_all)} != {len(pending)}')
        sys.exit(1)

    # 生成 key（避免与现有冲突）
    with io.open(LOC_H, encoding='utf-8') as f:
        loc_h = f.read()
    existing = set(re.findall(r'^\s{4}([A-Za-z_][A-Za-z0-9_]*),', loc_h, re.M))

    new_entries = []  # (key, en, zh)
    for en, zh in zip(pending, zh_all):
        key = key_from_en(en)
        base = key
        i = 2
        while key in existing:
            key = f'{base}_{i}'
            i += 1
        existing.add(key)
        new_entries.append((key, en, zh))

    # 更新 localization.h
    with io.open(LOC_H, encoding='utf-8') as f:
        loc_h = f.read()
    members = '\n'.join(f'    {k},' for k, _, _ in new_entries)
    loc_h = loc_h.replace('    COUNT\n};', members + '\n    COUNT\n};')
    with io.open(LOC_H, 'w', encoding='utf-8', newline='\n') as f:
        f.write(loc_h)

    # 更新 lang_en.h / lang_zh_cn.h
    for path, is_zh in ((EN_H, False), (ZH_H, True)):
        with io.open(path, encoding='utf-8') as f:
            c = f.read()
        lines = []
        for k, en, zh in new_entries:
            v = c_lit(zh) if is_zh else en
            lines.append(f'    table[static_cast<int>(LK::{k})] = "{v}";')
        # 先剥掉已有的结尾 '}'（防止重复运行导致结构错乱：'}' 被夹在中间）
        c = c.rstrip()
        if c.endswith('}'):
            c = c[:c.rfind('}')].rstrip()
        c = c + '\n' + '\n'.join(lines) + '\n}\n'
        with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
            f.write(c)

    # 更新 strings_map.json
    with io.open(MAP, encoding='utf-8') as f:
        smap = json.load(f)
    for k, en, _ in new_entries:
        smap[en] = k
    with io.open(MAP, 'w', encoding='utf-8', newline='\n') as f:
        json.dump(smap, f, ensure_ascii=False, indent=1)

    print(f'✅ 新增 {len(new_entries)} 条翻译')
    for k, en, zh in new_entries[:10]:
        print(f'  {k}: {en[:40]} -> {zh[:40]}')
    if len(new_entries) > 10:
        print(f'  ... 其余 {len(new_entries) - 10} 条')

if __name__ == '__main__':
    main()
