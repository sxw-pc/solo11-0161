# 保障房轮候资格采证台

完全离线的虚构家庭申请、保障资格复核和房源配租三套业务账。材料受理时刻、成员冲突、收入住房核验与异议会改变轮候状态；资格迟到变更会释放未签约房源，签约会把家庭标为已安置。原始证据和每版结论留在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m housingqueue demo --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口为 `application-import`、`eligibility-import`、`allocation-import`，可用 `--file fixtures/*-ok.json` 或对应 `*-bad.json`。`report` 给出申请、资格、队列、房源、邀约与签约；`audit` 从原始文件在新库重放比对。所有家庭和房源纯属合成，不对接真实人口、税务或不动产系统；角色和签名只是离线标签。

隔离增强副本另有 `facility-import`，记录社区公共房间和已安置家庭的单次预约。`fixtures/facility-ok.json` 可在 demo 后导入。

## 活动室分场与签到（facility-import 增强）

在单次预约之外，`facility-import` 现支持：活动授权（`activity`，记录主办户全体同场目标及时段）、书面分场同意（`activity_consent`，主办户申请人事先签署的可分场时间窗与各窗人数）、分场排房（`session`，可指定大房分隔区域，逐条核对人数、无障碍、噪声与设备并留存设备证据）、换房（`session_move`，换房前后设备与实际人员均须满足条件）、临时取消（`session_cancel`）、外来客人授权（`guest_pass`）、主持人/独立见证签到（`checkin`）与签到纠错（`attendee_correction`；`application-import` 新增 `remove_member` 成员纠错并级联）。

签到按活动发生时的有效成员归属（`application.member`）或该场访客授权核验，人数上限不代替身份校验；同一人同一时段只能签到一场；已发生的签到不可挪到另一时段。纠错只撤回涉及场次的未完成放行，旧签到与房间占用责任留痕可查，不波及其他住户已发生的签到。`report`/`audit` 列出每场实际举办状态、时段、人员来源（member/guest）与哪一场获得原定同场成果，不用一条最大容纳人数替代主办户的活动目标。

```powershell
python -m housingqueue application-import --db evening.sqlite --file fixtures/application-facility.json
python -m housingqueue eligibility-import --db evening.sqlite --file fixtures/eligibility-facility.json
python -m housingqueue allocation-import --db evening.sqlite --file fixtures/allocation-facility.json
python -m housingqueue facility-import --db evening.sqlite --file fixtures/facility-evening.json
python -m housingqueue facility-import --db evening.sqlite --file fixtures/facility-split.json   # 或 facility-joint.json（H1 独占大房同场，H2/H3 延后）
python -m housingqueue application-import --db evening.sqlite --file fixtures/facility-correction.json
python -m housingqueue report --db evening.sqlite
python -m housingqueue audit --db evening.sqlite
```

`facility-evening.json` 登记大房 R1（8 人、可分隔 R1-a/R1-b）与小房 R2（4 人）、H1 的 6 人颁奖活动及其分场书面同意、H2/H3 只能在大房前/后一小时举行的 4 人活动；`facility-split.json` 为 H1 分用小房、H2/H3 按期使用大房的安排；`facility-joint.json` 为 H1 独占大房全体同场的安排；`facility-correction.json` 演示 18:30 证实签到者非本户成员后的成员纠错。source_file 按内容寻址重放，异内容事件 ID 整批拒绝。
