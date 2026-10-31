#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
sync_docs.py - 汉化文档增量同步工具

背景：workflow 同步上游时排除了已汉化文档（README/Config/Features/Issues/Spoofing/
CONTRIBUTING/OptiScaler.ini），防止上游覆盖中文版。本脚本负责"排除后的跟随更新"：

1. OptiScaler.ini（结构化，全自动合并）：
   - 对比上游英文版与本地中文版的 section/key 结构
   - 上游新增 key -> 用 strings_map.json 已有翻译，没有的调百度大模型文本翻译注释，插入中文版
   - 上游删除 key -> 从中文版删除
   - key 顺序变化 -> 按上游顺序重排（本地已翻译的注释保留不动）

2. md 文档（README/Config/Features/Issues/Spoofing/CONTRIBUTING）：
   - 保存上游英文快照到 docs/upstream_ref/（首次运行建立基线）
   - 上游英文版与快照对比 -> 有变化才处理：
     * README.md：定制内容多，仅生成变更报告（docs/UPSTREAM_CHANGES.md）
     * 其他文档：自动整篇翻译并覆盖中文版（内容基本来自上游，可接受）
     * 百度翻译不可用：全部走报告

退出码：0 = 无变更；2 = 有变更（需提交）

用法：
  python scripts/sync_docs.py --repo <仓库根> [--dry-run]

依赖：Python 3.8+，标准库（urllib/json/re），无第三方依赖。
"""
import argparse
import datetime
import io
import json
import os
import re
import subprocess
import sys

from baidu_translate import (
    BaiduTranslateError,
    credentials_available,
    translate_text,
    translate_texts,
)

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

# 需要跟随上游的文档清单（已汉化、从 sync checkout 排除的文件）
# mode: report = 只报告（人工处理）；auto = 自动翻译覆盖
MD_DOCS = {
    'README.md': 'report',
    'Config.md': 'auto',
    'Features.md': 'auto',
    'Issues.md': 'auto',
    'Spoofing.md': 'auto',
    'CONTRIBUTING.md': 'auto',
}
INI_DOCS = ['OptiScaler.ini']
UPSTREAM_REMOTE = 'upstream'
UPSTREAM_BRANCH = 'master'
_UPSTREAM_REF = [f'{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}']
REF_DIR = 'docs/upstream_ref'

def run_git(repo, args, check=True):
    """在 repo 中执行 git 命令，返回 stdout 文本"""
    p = subprocess.run(['git', '-C', repo] + args, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if check and p.returncode != 0:
        raise RuntimeError(f'git {" ".join(args)} 失败: {p.stderr[:500]}')
    return p.stdout


def read_file(path):
    if not os.path.exists(path):
        return None
    with io.open(path, encoding='utf-8-sig', errors='replace') as f:
        return f.read()


def write_file(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with io.open(path, 'w', encoding='utf-8', newline='\n') as f:
        f.write(content)


def get_upstream_file(repo, path):
    """从 upstream remote 读取文件内容（不覆盖工作区）"""
    try:
        return run_git(repo, ['show', f'{_UPSTREAM_REF[0]}:{path}'])
    except RuntimeError:
        return None


def load_strings_map(repo):
    """读取 strings_map.json，返回 {英文: 中文} 映射"""
    path = os.path.join(repo, 'scripts', 'strings_map.json')
    try:
        with io.open(path, encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def parse_ini(text):
    """解析 ini 为结构化条目列表。"""
    entries = []
    cur_comments = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):
            # section 前的注释（装饰线等）也保留，merge 时输出，避免格式缩水
            entries.append({'type': 'section', 'name': stripped, 'comments': cur_comments})
            cur_comments = []
        elif stripped.startswith(';'):
            cur_comments.append(line)
        elif '=' in stripped and not stripped.startswith(';'):
            key = stripped.split('=', 1)[0].strip()
            entries.append({
                'type': 'entry',
                'key': key,
                'comments': cur_comments,
                'value_line': line,
            })
            cur_comments = []
    return entries


def merge_ini(upstream_text, local_text):
    """增量合并：以本地中文版为基准，跟随上游结构。
    返回 (新内容, 变更统计 dict)
    """
    up = parse_ini(upstream_text)
    local = parse_ini(local_text)

    local_by_key = {}
    local_by_section = {}
    for e in local:
        if e['type'] == 'entry':
            local_by_key[e['key']] = e
        elif e['type'] == 'section':
            local_by_section[e['name']] = e

    up_keys = [e['key'] for e in up if e['type'] == 'entry']

    # 需要翻译的：上游有、本地没有的 entry
    to_translate = [e for e in up if e['type'] == 'entry' and e['key'] not in local_by_key]

    if to_translate:
        texts = ['\n'.join(e['comments']) if e['comments'] else '' for e in to_translate]
        try:
            translated = translate_texts(texts)
        except BaiduTranslateError as exc:
            print(f'[ini] 百度翻译失败，保留英文注释: {exc}')
            translated = texts
        for e, t in zip(to_translate, translated):
            e['_translated_comments'] = t.splitlines() if t else []

    out_lines = []
    stats = {'added': 0, 'removed': 0, 'kept': 0}

    local_keys = set(local_by_key.keys())
    up_key_set = set(up_keys)
    stats['removed'] = len(local_keys - up_key_set)

    prev_type = None
    for e in up:
        if e['type'] == 'section':
            if prev_type == 'entry':
                out_lines.append('')
            # section 前的装饰注释：优先用本地版，本地没有则用上游版
            sec_comments = local_by_section.get(e['name'], {}).get('comments')
            if sec_comments:
                out_lines.extend(sec_comments)
            elif e.get('comments'):
                out_lines.extend(e['comments'])
            out_lines.append(e['name'])
            prev_type = 'section'
        elif e['type'] == 'entry':
            if e['key'] in local_by_key:
                le = local_by_key[e['key']]
                if le['comments']:
                    out_lines.extend(le['comments'])
                else:
                    out_lines.extend(e['comments'])
                out_lines.append(le['value_line'])
                stats['kept'] += 1
            else:
                if e.get('_translated_comments'):
                    out_lines.extend(e['_translated_comments'])
                else:
                    out_lines.extend(e['comments'])
                out_lines.append(e['value_line'])
                stats['added'] += 1
            prev_type = 'entry'

    new_content = '\n'.join(out_lines) + '\n'
    return new_content, stats


def sync_md_docs(repo):
    """md 文档跟随：上游英文快照对比 + 分级处理。
    返回 (changed_files, report_lines)
    """
    changed = []
    report = []

    for doc, mode in MD_DOCS.items():
        up_text = get_upstream_file(repo, doc)
        if up_text is None:
            continue

        ref_path = os.path.join(repo, REF_DIR, doc + '.en')
        prev_ref = read_file(ref_path)

        if prev_ref is None:
            # 首次：建立基线快照，并标记为需要提交（快照必须进仓库，否则下次 CI 又是全新 checkout）
            if not args_dry_run_global():
                write_file(ref_path, up_text)
            changed.append(doc)
            report.append(f'- {doc}: 建立上游基线快照（首次）')
            print(f'[md] {doc}: 建立上游基线快照（首次，需提交）')
            continue

        if prev_ref == up_text:
            print(f'[md] {doc}: 上游无变化')
            continue

        # 上游有变化
        changed.append(doc)
        if not args_dry_run_global():
            write_file(ref_path, up_text)  # 更新快照

        if mode == 'report':
            report.append(f'- {doc}: 上游有更新，需要人工处理（README 含定制内容，不自动覆盖）')
            print(f'[md] {doc}: 上游有更新（report 模式，人工处理）')
            continue

        # auto 模式：自动翻译覆盖
        if credentials_available():
            try:
                translated_text = translate_text(up_text)
                if translated_text and translated_text != up_text and translated_text.strip():
                    if not args_dry_run_global():
                        write_file(os.path.join(repo, doc), translated_text)
                    report.append(f'- {doc}: 已自动翻译上游更新')
                    print(f'[md] {doc}: 已自动翻译覆盖')
                    continue
            except BaiduTranslateError as e:
                report.append(f'- {doc}: 自动翻译失败 ({e})')
        report.append(f'- {doc}: 上游有更新，百度翻译不可用，需要人工处理')
        print(f'[md] {doc}: 上游有更新，自动翻译不可用')

    return changed, report


_DRY_RUN = [False]


def args_dry_run_global():
    return _DRY_RUN[0]


def main():
    global _DRY_RUN
    parser = argparse.ArgumentParser(description='汉化文档增量同步')
    parser.add_argument('--repo', required=True, help='仓库根目录')
    parser.add_argument('--app-id', default=os.environ.get('BAIDU_APP_ID', ''), help='百度翻译 APP ID')
    parser.add_argument('--api-key', default=os.environ.get('BAIDU_API_KEY', ''), help='百度 API Key（Bearer）')
    parser.add_argument('--secret-key', default=os.environ.get('BAIDU_SECRET_KEY', ''), help='百度密钥（MD5 sign 回退）')
    parser.add_argument('--upstream-ref', default=f'{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}',
                        help='要读取的上游 git ref，默认 upstream/master')
    parser.add_argument('--dry-run', action='store_true', help='只检测不写文件')
    args = parser.parse_args()
    _DRY_RUN[0] = args.dry_run
    _UPSTREAM_REF[0] = args.upstream_ref
    os.environ['BAIDU_APP_ID'] = args.app_id
    os.environ['BAIDU_API_KEY'] = args.api_key
    os.environ['BAIDU_SECRET_KEY'] = args.secret_key

    repo = os.path.abspath(args.repo)
    load_strings_map(repo)  # 兼容旧参数（保留）

    # 确保 upstream remote 存在
    remotes = run_git(repo, ['remote'])
    if UPSTREAM_REMOTE not in remotes.split():
        run_git(repo, ['remote', 'add', UPSTREAM_REMOTE, 'https://github.com/optiscaler/OptiScaler.git'])
    if _UPSTREAM_REF[0] == f'{UPSTREAM_REMOTE}/{UPSTREAM_BRANCH}':
        run_git(repo, ['fetch', UPSTREAM_REMOTE, UPSTREAM_BRANCH, '--depth', '50'])
    else:
        run_git(repo, ['cat-file', '-e', f'{_UPSTREAM_REF[0]}^{{commit}}'])

    all_changed = []
    report_lines = []

    # 1. ini 增量合并
    for doc in INI_DOCS:
        up_text = get_upstream_file(repo, doc)
        if up_text is None:
            print(f'[ini] {doc}: 上游无此文件，跳过')
            continue
        local_text = read_file(os.path.join(repo, doc))
        if local_text is None:
            local_text = ''
        if up_text == local_text:
            print(f'[ini] {doc}: 上游无变化')
            continue
        new_content, stats = merge_ini(up_text, local_text)
        print(f'[ini] {doc}: added={stats["added"]} removed={stats["removed"]} kept={stats["kept"]}')
        if stats['added'] or stats['removed']:
            all_changed.append(doc)
            report_lines.append(f'- {doc}: 新增配置 {stats["added"]} 项，移除 {stats["removed"]} 项')
            if not args.dry_run:
                write_file(os.path.join(repo, doc), new_content)

    # 2. md 变更检测
    md_changed, md_report = sync_md_docs(repo)
    all_changed.extend(md_changed)
    report_lines.extend(md_report)

    # 3. 写变更报告
    if report_lines and not args.dry_run:
        ts = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
        content = f'# 上游文档变更记录\n\n> 生成时间：{ts}\n> 说明：以下变更已检测到，需要人工或后续自动处理。\n\n'
        content += '\n'.join(report_lines) + '\n'
        write_file(os.path.join(repo, 'docs', 'UPSTREAM_CHANGES.md'), content)

    if all_changed:
        print(f'变更文件: {", ".join(all_changed)}')
        print('需要提交')
        sys.exit(2)
    print('无变更')
    sys.exit(0)


if __name__ == '__main__':
    main()
