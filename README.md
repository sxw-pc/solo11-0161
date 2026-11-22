# 保障房轮候资格采证台

完全离线的虚构家庭申请、保障资格复核、房源配租与维修停用四套业务账。材料受理时刻、成员冲突、收入住房核验与异议会改变轮候状态；资格迟到变更会释放未签约房源，签约会把家庭标为已安置。供水缺陷走独立验房、停用决定、维修工单、完工复验、临时安置与复住交接的版本化裁决，已签租约和已发生入住不会被改写成“从未分配”。原始证据和每版结论留在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m housingqueue demo --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
python -m unittest discover -s tests -v
```

四个独立入口为 `application-import`、`eligibility-import`、`allocation-import`、`maintenance-import`，可用 `--file fixtures/*-ok.json` 或对应 `*-bad.json`。`report` 给出申请、资格、队列、房源、邀约、签约以及缺陷/停用/工单/复验/临时安置/永久替换账，并在 `placement_adjudication` 分列“原租约签约时资格”“当前替换资格”“临时安全安置责任”三项，不压成最新 `household.status`；`audit` 从原始文件在新库重放比对。

## 维修停用两条处置路径（H1=family-a 已签 U1，H2=family-b 等候两居室，U3 为合规周转房）

```powershell
# 路径 A：U1 修好复验通过，H1 复住原房，U2 按原队列邀约 H2
python -m housingqueue maintenance-import --db case-a.sqlite --file fixtures/maintenance-path-a-repair.json
python -m housingqueue allocation-import  --db case-a.sqlite --file fixtures/maintenance-path-a-h2-offer.json
# 路径 B：独立验房证实 U1 长期不可住，H1 书面同意后 U2 永久替换，H2 继续等待
python -m housingqueue maintenance-import --db case-b.sqlite --file fixtures/maintenance-path-b-replacement.json
# 维修单位自报完工与独立复验冲突：U1 继续停用并留追责记录
python -m housingqueue maintenance-import --db case-c.sqlite --file fixtures/maintenance-conflict.json
# 迟到缺陷证据：冻结未签邀约、恢复轮候位置；早于已签入住则开安全争议
python -m housingqueue maintenance-import --db case-l.sqlite --file fixtures/maintenance-late-defect.json
python -m housingqueue report --db case-a.sqlite
python -m housingqueue audit  --db case-a.sqlite
```

停用窗口按来源事件重建：停用期间任何新邀约被 `blocked`，周转房（`usage="swing"`）占用真实容量但不承接普通邀约、也不改变原轮候资格；只有独立复验通过并经 `return_to_service` 才能复住。迟到收入核验使 H1 不合格时，临时安全安置继续有效，未履行的永久替换承诺冻结、U2 回到按当时资格核对的 H2 邀约机会；核验更正且 U2 尚未签给他人时原承诺重新核定（见 `fixtures/maintenance-late-ineligible.json` 与 `tests/test_maintenance.py`）。所有家庭和房源纯属合成，不对接真实人口、税务或不动产系统；角色和签名只是离线标签。
