# 流动阅读服务的需求覆盖与周计划兑现

本项目在原有合成资料检查能力之上，提供一个仅依赖 Python 3.11 标准库的
分析应用，把居民需求、未参加原因、站点可达条件与历次实际服务连接起来，
生成可兑现的周计划并复核计划改变是否填补了真正的覆盖缺口。所有人员、
学校、机构与业务记录均为演练数据。

## 目录结构

```
materials/              原始合成资料（只读）：data.json + contract.json
inspect_materials.py    资料结构、编号、外键检查与指纹（原有能力）
src/mobile_reading/     分析应用
  models.py             领域模型与资料装载（null 不解释为零）
  network.py            道路图、中断覆盖、最短路、整日路线联合检验
  demand.py             三层需求漏斗、家庭聚类区间、自报障碍
  planner.py            承诺锁定、最低频率回溯、边际覆盖/成本排期、对照基线
  replan.py             承诺保护、改期拒绝、取消逐户台账、等效替代
  feedback.py           服务回录、覆盖审计、下一轮需求信号
  reports.py            结构化 JSON 与中文文本复核件
  cli.py                命令行入口
run_app.py              python3 -I 隔离模式启动器
tests/                  62 个 unittest 用例
操作手册.md             运行步骤、口径落点与结构化复核方法
```

## 快速开始

```sh
# 资料检查
python3 -I inspect_materials.py

# 全部测试
python3 -I -m unittest discover -s tests -v

# 端到端演练（结果写入 output/，该目录已被 git 忽略）
python3 -I run_app.py demo
```

常用子命令（详见 [操作手册.md](操作手册.md)）：

```sh
python3 -I run_app.py demand                      # 三层需求漏斗与不确定性
python3 -I run_app.py plan                        # 保障优先周计划 + 按到场排满基线
python3 -I run_app.py --fresh-cycle plan          # 新周期最低频率保障验证
python3 -I run_app.py replan --remove PL-003      # 承诺保护下改期
python3 -I run_app.py cancel CW1 --reason "车辆故障"
python3 -I run_app.py feedback --simulate-cancel # 回录与下一轮需求信号
```

## 核心业务口径

- 同一人在学校调查与站点报名中的重复登记按人去重，不重复放大需求；
- 潜在受益人、愿意参加者、给定时间地点实际可达者三层分开，各带家庭
  聚类稳健标准误与 95% 区间及适用范围说明；
- 未到场原因只采用登记人本人自报，出席侧事实（取消、拒绝）单独建表；
- 已通知家庭的场次为服务承诺，路线调整后须仍在原时段形式可达，删除
  锁定场次的请求被拒绝；取消须逐户区分等效替代与未兑现补兑；
- 偏远站点最低频率由回溯搜索优先保障，排不下则列入未决缺口，不由
  邻近站点出席人数抵消；
- 每次插入场次都对车辆当日整条路线（行驶顺序、返场、班次窗、辅导员
  技能与工时）联合检验，逐站可行不等于同日可行；
- 取消造成的缺席不计为需求下降，名册外临时访客不计入居民基数，被拒
  请求结转为下一轮扩容信号，覆盖按人区分新增与重复。

原始资料保留不变；运行生成的状态与导出结果只写入 `output/`。
