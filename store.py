# -*- coding: utf-8 -*-
"""数据文件原子写：先写临时文件，再 os.replace 原子替换。

直接 write_text 一旦断电/崩溃会把 JSON 写一半，整个文件报废；
os.replace 在同一磁盘上是原子操作，主文件要么是旧的要么是新的。
"""

import json
import os
from pathlib import Path


def write_text(path, text):
    path = Path(path)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.replace(tmp, path)


def write_json(path, obj):
    write_text(path, json.dumps(obj, ensure_ascii=False, indent=1))
