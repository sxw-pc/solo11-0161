# 保障房轮候资格采证台

完全离线的虚构家庭申请、保障资格复核和房源配租三套业务账。材料受理时刻、成员冲突、收入住房核验与异议会改变轮候状态；资格迟到变更会释放未签约房源，签约会把家庭标为已安置。原始证据和每版结论留在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m housingqueue demo --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口为 `application-import`、`eligibility-import`、`allocation-import`，可用 `--file fixtures/*-ok.json` 或对应 `*-bad.json`。`report` 给出申请、资格、队列、房源、邀约与签约；`audit` 从原始文件在新库重放比对。所有家庭和房源纯属合成，不对接真实人口、税务或不动产系统；角色和签名只是离线标签。

本独立增强副本增加 `power-import`，记录已分配房源的瞬时用电读数。`power_demand` 只汇总**已实体交付**（`delivery` 见证）入住单元的最新读数；已签约但未交付的房源不算有人用电，缺测保持 `pending` 而不是按 0 处理；`fixtures/power-ok.json` 可在 demo 后导入。

## 断网电池调度（`dispatch-import`）

停电窗口内由电池+逆变器分时供电，生成逐 5 分钟片的 `supply_decision`（`supply_plan`/`supply_segment`）：

- **硬约束**：已核医疗维生设备（`device.critical=true`）按申报最低功率连续供电；普通负载只有住户书面允许中断（`interruptible=true, consent=written`）才可切断，普通负载一旦接通必须连续满 30 分钟最短保持。
- **瞬时校验**：每个 5 分钟片都校验逆变器瞬时上限（`inverter.limit_w`），不能只按一小时总 kWh 平衡；每段记录电池剩余电量（瓦时秒）、瞬时功率、实际通电设备与未兑现承诺（`unfulfilled`）。
- **连续与不可变**：电池电量沿切换时序逐段连续；现场切换由 `switch` 事件带见证人和证据固定为 `executed`，后到的签约、交付、电表数据只能重算尚未执行的段，已执行段永不覆盖。
- **可行时序枚举**：同一窗口保存全部互不可支配的可行时序并标记实际采用的一条（`adopted`），按枚举发现顺序编号，不按住户/房源 ID 排序；`adopt` 可在任何段执行前改采。
- **动态入住**：`allocation.sign` 新增签约后从当前已交付单元重算 `power_demand`；`delivery` 完成实体交付后重排待执行段并把新入住设备纳入后续窗口。
- **申报冲突**：设备申报与电表读数冲突时设备标 `conflict_pending`，缺测不当 0，冲突设备退出调度直到核实。

`dispatch-import` 接受事件：`battery`、`inverter`、`grid`、`device`、`delivery`、`switch`、`dispatch_window`、`adopt`、`meter_conflict`。原 `application-import`、`eligibility-import`、`allocation-import`、`power-import` 入口与 `demo`、`report`、`audit` 行为不变，`audit` 从原始事件重放并逐段比对调度状态。

```bash
# 两可行时序（U2 冰柜优先 / U3 水泵优先），电池 1.5 kWh、逆变器 2 kW
python -m housingqueue dispatch-import --db disp.sqlite --file fixtures/dispatch-full-occupancy.json
python -m housingqueue dispatch-import --db disp.sqlite --file fixtures/dispatch-window-full.json
python -m housingqueue report --db disp.sqlite
python -m housingqueue audit --db disp.sqlite
# 18:20 现场签约+实体交付，已执行 U1/U2 电段保留，U3 纳入后半小时
python -m housingqueue allocation-import --db disp.sqlite --file fixtures/dispatch-sign-u3.json
python -m housingqueue dispatch-import --db disp.sqlite --file fixtures/dispatch-deliver-u3.json
```

