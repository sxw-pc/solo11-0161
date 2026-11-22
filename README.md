# 保障房轮候资格采证台

完全离线的虚构家庭申请、保障资格复核和房源配租三套业务账。材料受理时刻、成员冲突、收入住房核验与异议会改变轮候状态；资格迟到变更会释放未签约房源，签约会把家庭标为已安置。原始证据和每版结论留在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m housingqueue demo --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口为 `application-import`、`eligibility-import`、`allocation-import`，可用 `--file fixtures/*-ok.json` 或对应 `*-bad.json`。`report` 给出申请、资格、队列、房源、邀约与签约；`audit` 从原始文件在新库重放比对。所有家庭和房源纯属合成，不对接真实人口、税务或不动产系统；角色和签名只是离线标签。

本隔离增强副本另有 `shuttle-import`，记录已安置家庭的一次社区接送预约；`fixtures/shuttle-ok.json` 可在 demo 后导入。

## 多站接送路线（ride-route-import）

`shuttle-import` 仍只按整段预约区间独占车辆。真实接送是一辆车在同一次行程里先后服务多户居民，必须按**实际路段**核算在车人数、轮椅位与司机连续驾驶/休息，并用两名见证人记录上、下车。`ride-route-import` 记录这些事实：

- `place` / `stop` / `segment`：站点与有向路段时刻（同一对站点可记录一次行驶分钟数）。
- `driver` / `shift`：司机连续驾驶上限、休息时长与班次。
- `wheelchair_auth`：轮椅住户书面同意固定。
- `ride_request` / `ride_stop` / `authorization`：一辆车的一次行程、各站与每户书面授权窗（`original` 原预约、`rebook` 本人/医院授权改约）。`context_only` 表示签约前的线路咨询。
- `execute`：一次性排定可执行路线。只有**已实际交付住房**（`allocation-import` 的 `deliver` 事件）的住户才能获得确认席位；只签名未交房者被视为咨询，不进确认名单。车辆同一时间不能承诺给两趟行程。
- `secure` / `boarding`：轮椅固定/解锁与上/下车，均须两名不同见证人。座位只由**已见证下车**释放，绝不按预约结束时间推断。
- `hospital_rebook`：凭证明记录医院改约，生成 `rebook` 授权窗，不臆造授权。
- `closure` + `observe_closure`：记录临时封路。封路只重排**未行驶**部分，已完成的上车、下车与到诊时刻保留；无可行路径时标为不可达。

`execute` 用最短路在当前封路状态下为各站排序，逐段检查：在车人数不超席位、轮椅位不超上限、司机连续驾驶与班次、以及各户授权窗。报告对每户列出原预约窗、本人授权的改约窗、真实上/下车与是否赶上原预约（`caught_original`）。`audit` 从原始文件在新库重放，比对包含 `ride_leg`、`ride_boarding` 与每户报告在内的全部表。

复现（假设已用 `application-import`、`eligibility-import`、`allocation-import` 导入 `fixtures/ride-*-application|eligibility|allocation.json` 造出 H1、H2 并交付住房）：

```powershell
# 直送 H1：09:30 到医院，赶上 09:45 原诊
python -m housingqueue ride-route-import --db demo.sqlite --file fixtures/ride-route-direct.json
# 先送 H2：H2 09:20 到图书馆，H1 10:05 到医院（按 10:15 改约）
python -m housingqueue ride-route-import --db demo.sqlite --file fixtures/ride-route-h2-first.json
# 图书馆->H1 在 09:25 封闭：H2 已送达保留，后半程不可达
python -m housingqueue ride-route-import --db demo.sqlite --file fixtures/ride-route-closure.json
# 封闭 h1-door->hospital 后经 library 绕行重排未行部分
python -m housingqueue ride-route-import --db demo.sqlite --file fixtures/ride-route-replan.json
# 缺下车见证：H2 座位仍被占用，两户都标 partial
python -m housingqueue ride-route-import --db demo.sqlite --file fixtures/ride-route-missing-alight.json
python -m housingqueue report --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
```
