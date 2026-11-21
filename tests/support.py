"""测试公共装载：在仓库根目录下以隔离方式导入应用与资料。"""
import sys
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from inspect_materials import read_materials  # noqa: E402

from mobile_reading.models import load_bundle  # noqa: E402
from mobile_reading.planner import PlannerConfig  # noqa: E402

WEEK_START = date(2026, 8, 3)


def make_bundle():
    data, receipt = read_materials(ROOT / "materials")
    return load_bundle(
        ROOT / "materials",
        fingerprint=receipt["sha256"],
        row_counts=receipt["rows"],
    )


def make_config(**kwargs):
    kwargs.setdefault("week_start", WEEK_START)
    return PlannerConfig(**kwargs)


class AppTestCase(unittest.TestCase):
    def setUp(self):
        self.bundle = make_bundle()
        self.cfg = make_config()
