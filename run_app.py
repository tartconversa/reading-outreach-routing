#!/usr/bin/env python3
"""隔离模式（python3 -I）下的命令行启动器。

``-I`` 会忽略 PYTHONPATH，因此在脚本内显式加入 src/ 路径；
资料目录始终以参数传入，运行解释器的当前目录不参与模块搜索，
避免资料目录中被植入同名模块（如 json.py）。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from mobile_reading.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
