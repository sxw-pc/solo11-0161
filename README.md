# 保障房轮候资格采证台

完全离线的虚构家庭申请、保障资格复核和房源配租三套业务账。材料受理时刻、成员冲突、收入住房核验与异议会改变轮候状态；资格迟到变更会释放未签约房源，签约会把家庭标为已安置。原始证据和每版结论留在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m housingqueue demo --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口为 `application-import`、`eligibility-import`、`allocation-import`，可用 `--file fixtures/*-ok.json` 或对应 `*-bad.json`。`report` 给出申请、资格、队列、房源、邀约与签约；`audit` 从原始文件在新库重放比对。所有家庭和房源纯属合成，不对接真实人口、税务或不动产系统；角色和签名只是离线标签。

本隔离增强副本另有 `water-import`，把房源水龙头和单点水样纳入报告；`fixtures/water-ok.json` 可在房源导入后运行。当前只按每个采样点的阈值判断，不记录水流、混合、阀门或住户饮水范围。

## 供水路径与饮用指令（water-flow-import）

`water-flow-import` 在同一事件账上构建楼栋水箱、带容积管段、可控阀门/泵与冲洗出口，按一分钟离散步长演算：端点先向上游拉取当分钟用水量，水团 FIFO 通过管段，零来水时管段保留原有水团而不是排空；每段拉流结束后水箱才把已确认清水与箱内剩水充分混合（先流出、后混合）。每个节点每分钟做质量守恒校验，已流出的水团不会因后来的阴性样本倒流消失。

指令状态机输出三类可执行指令：`direct`（直接饮用）、`boil`（煮沸后饮用）、`bottled`（停水取瓶装水）。模拟值低于阈值本身不解除指令，必须有该龙头在模拟首次达标之后的有效复采且实验室质控合格；质控失格样本不具证明力，采样时刻晚于收样时刻的事件被原子拒绝。阀门/泵动作只在 `approved=true` 时改变流量分配，并从动作时刻前的实际水团继续演算；泵停转记为停水，零流量不会被当作污染归零。指令按 `allocation.py` 的签约事实送达实际入住家庭（签约后资格争议仍保留入住），空房只进入物业房源处置清单，不伪造住户签收。

复现同一楼栋的两条反例路线（水箱初始 100 L / 200 CFU，清水 20 L/分钟，U1 管段 40 L 滞留水 140，阈值低于 100）：

```bash
# 共用前置：申请、资格、签约、空房 U2、龙头登记
python -m housingqueue application-import --db flow.sqlite --file fixtures/application-ok.json
python -m housingqueue eligibility-import --db flow.sqlite --file fixtures/eligibility-ok.json
python -m housingqueue allocation-import  --db flow.sqlite --file fixtures/allocation-ok.json
python -m housingqueue allocation-import  --db flow.sqlite --file fixtures/unit-u2.json
python -m housingqueue water-import       --db flow.sqlite --file fixtures/water-flow-taps.json
# 路线甲：关入户阀+专用排污口冲洗+瓶装水，复采合格后开阀（独立库请换 --db 路径）
python -m housingqueue water-flow-import  --db flow-alpha.sqlite --file fixtures/water-flow-route-alpha.json
# 路线乙：保持供水+煮沸指令，正常用水带走管内旧水，复采合格后解除
python -m housingqueue water-flow-import  --db flow-beta.sqlite  --file fixtures/water-flow-route-beta.json
# 泵停转、未批准动作不改流、质控失格与采样/收样倒序见对应 fixtures
python -m housingqueue water-flow-import  --db flow-pump.sqlite  --file fixtures/water-flow-pump-outage.json
python -m housingqueue report --db flow-alpha.sqlite
python -m housingqueue audit  --db flow-alpha.sqlite
```

两条路线在分钟 4 都不会因水箱浓度降到 81.92 就宣布 U1 安全：甲路线分钟 1–4 为 `bottled/interrupted`，乙路线分钟 0–6 为 `boil/delivered`，乙路线分钟 7 凭合格复采 S-B6 才恢复 `direct`。`report` 含逐分钟节点水量/浓度/守恒差异、水团快照和每户指令与通知对象；`audit` 从原始事件在内存库重放，比对全部派生表。
