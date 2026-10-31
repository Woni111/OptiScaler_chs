#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Baidu large-model text translation client used by the localization tools."""

import hashlib
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request


API_URL = 'https://fanyi-api.baidu.com/ait/api/aiTextTranslate'
MAX_QUERY_CHARS = 5500
RETRYABLE_CODES = {
    '31001', '31006', '31101', '31102', '31104',
    '52001', '52002', '54003', '54005',
}

DEFAULT_REFERENCE = (
    '这是 OptiScaler 游戏超分辨率与帧生成工具的界面文本。'
    '请翻译成简洁、自然的简体中文；保留 DLL 名称、路径、URL、Markdown/INI 语法、'
    '格式说明符和所有技术缩写。不要扩写，不要解释。'
    '术语：Upscaler=超分辨率，Upscaling=超分，Frame Generation/FG=帧生成，'
    'Motion Vectors/MV=运动矢量，Ghosting=拖影，Artifacts=伪影，Overlay=覆盖层，'
    'V-Sync=垂直同步，Shader=着色器，Preset=预设，Sharpness=锐化，Latency=延迟。'
    'DLSS/FSR/XeSS/RCAS/OptiFG/DLSSG/XeFG/HUDLess/Reactive Mask 保留英文。'
)

TOKEN_RE = re.compile(
    r'%(?:\d+\$)?[-+#0 ]*\d*(?:\.\d+)?[diouxXeEfFgGaAcspn%]'
    r'|\{[^{}\r\n]{1,40}\}'
    r'|##[A-Za-z0-9_]+'
    r'|\\[nrt]'
    r'|https?://[^\s<>()\[\]]+'
    r'|`[^`\r\n]+`'
    r'|</?[A-Za-z][^>\r\n]{0,80}>'
)


class BaiduTranslateError(RuntimeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = str(code) if code is not None else None


def _read_config(app_id=None, api_key=None, secret_key=None):
    app_id = app_id if app_id is not None else os.environ.get('BAIDU_APP_ID', '')
    api_key = api_key if api_key is not None else os.environ.get('BAIDU_API_KEY', '')
    secret_key = secret_key if secret_key is not None else os.environ.get('BAIDU_SECRET_KEY', '')
    app_id = app_id.strip()
    api_key = api_key.strip()
    secret_key = secret_key.strip()
    if not app_id:
        raise BaiduTranslateError('缺少 BAIDU_APP_ID')
    if not api_key and not secret_key:
        raise BaiduTranslateError('缺少 BAIDU_API_KEY 或 BAIDU_SECRET_KEY')
    return app_id, api_key, secret_key


def _split_text(text, limit=MAX_QUERY_CHARS):
    if len(text) <= limit:
        return [text]

    chunks = []
    current = []
    current_len = 0
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                chunks.append(''.join(current))
                current = []
                current_len = 0
            chunks.append(line[:limit])
            line = line[limit:]
        if current and current_len + len(line) > limit:
            chunks.append(''.join(current))
            current = []
            current_len = 0
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append(''.join(current))
    return [chunk for chunk in chunks if chunk]


def _protect_tokens(text):
    replacements = []

    def replace(match):
        token = f'ZQX{len(replacements)}QXZ'
        replacements.append((token, match.group(0)))
        return token

    return TOKEN_RE.sub(replace, text), replacements


def _restore_tokens(text, replacements):
    for token, original in replacements:
        if token not in text:
            raise BaiduTranslateError(f'百度翻译丢失受保护标记 {token}')
        text = text.replace(token, original)
    return text


def _request_once(text, app_id, api_key, secret_key, from_lang, to_lang, reference, timeout):
    body = {
        'appid': app_id,
        'q': text,
        'from': from_lang,
        'to': to_lang,
    }
    if reference:
        body['reference'] = reference

    headers = {'Content-Type': 'application/json;charset=utf-8'}
    if api_key:
        headers['Authorization'] = f'Bearer {api_key}'
    else:
        salt = str(random.randint(32768, 65536))
        sign_src = app_id + text + salt + secret_key
        body['salt'] = salt
        body['sign'] = hashlib.md5(sign_src.encode('utf-8')).hexdigest()

    request = urllib.request.Request(
        API_URL,
        data=json.dumps(body, ensure_ascii=False).encode('utf-8'),
        headers=headers,
        method='POST',
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode('utf-8', errors='replace')[:500]
        raise BaiduTranslateError(f'百度翻译 HTTP {exc.code}: {detail}') from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise BaiduTranslateError(f'百度翻译请求失败: {exc}') from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BaiduTranslateError(f'百度翻译返回非 JSON: {raw[:300]}') from exc

    if data.get('error_code') not in (None, '', 0, '0'):
        code = str(data.get('error_code'))
        message = data.get('error_msg') or data.get('error') or 'unknown error'
        raise BaiduTranslateError(f'百度翻译错误 {code}: {message}', code=code)

    results = data.get('trans_result')
    if not isinstance(results, list) or not results:
        raise BaiduTranslateError(f'百度翻译缺少 trans_result: {raw[:300]}')
    translated = '\n'.join(
        str(item.get('dst', ''))
        for item in results
        if isinstance(item, dict)
    )
    if not translated.strip():
        raise BaiduTranslateError(f'百度翻译返回空译文: {raw[:300]}')
    return translated


def translate_text(
    text,
    *,
    app_id=None,
    api_key=None,
    secret_key=None,
    from_lang='auto',
    to_lang='zh',
    reference=DEFAULT_REFERENCE,
    max_retries=3,
    timeout=120,
):
    """Translate one string through Baidu's large-model text endpoint."""
    if text is None:
        raise BaiduTranslateError('待翻译文本不能为空')
    if not text:
        return ''

    app_id, api_key, secret_key = _read_config(app_id, api_key, secret_key)
    translated_chunks = []
    for chunk in _split_text(text):
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                protected, replacements = _protect_tokens(chunk)
                translated = _request_once(
                    protected, app_id, api_key, secret_key,
                    from_lang, to_lang, reference, timeout,
                )
                translated_chunks.append(_restore_tokens(translated, replacements))
                last_error = None
                break
            except BaiduTranslateError as exc:
                last_error = exc
                if exc.code not in RETRYABLE_CODES or attempt >= max_retries:
                    raise
                time.sleep(min(8, 2 ** (attempt - 1)))
        if last_error is not None:
            raise last_error
    return '\n'.join(translated_chunks)


def translate_texts(
    texts,
    *,
    app_id=None,
    api_key=None,
    secret_key=None,
    from_lang='auto',
    to_lang='zh',
    reference=DEFAULT_REFERENCE,
    max_retries=3,
    request_interval=None,
):
    """Translate strings one by one so output order and cardinality stay exact."""
    if not texts:
        return []
    app_id, api_key, secret_key = _read_config(app_id, api_key, secret_key)
    if request_interval is None:
        request_interval = float(os.environ.get('BAIDU_REQUEST_INTERVAL', '0.12'))

    results = []
    for index, text in enumerate(texts):
        if index:
            time.sleep(max(0.0, request_interval))
        results.append(
            translate_text(
                text,
                app_id=app_id,
                api_key=api_key,
                secret_key=secret_key,
                from_lang=from_lang,
                to_lang=to_lang,
                reference=reference,
                max_retries=max_retries,
            )
        )
    return results


def credentials_available():
    try:
        _read_config()
        return True
    except BaiduTranslateError:
        return False


if __name__ == '__main__':
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    if len(sys.argv) < 2:
        print('用法: python baidu_translate.py <text>')
        sys.exit(1)
    print(translate_text(sys.argv[1]))
