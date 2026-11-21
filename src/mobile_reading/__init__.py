"""乡镇流动阅读服务：需求覆盖估计与周计划兑现分析应用。

仅使用 Python 3.11 标准库。所有业务编号均来自本地合成资料包，
不包含任何真实个人信息。
"""

__all__ = ["models", "io", "network", "demand", "planner", "replan", "feedback", "reports"]

SESSION_MINUTES = 60
"""单场服务时长（分钟），与已通知场次口径一致。"""
