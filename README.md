# 保障房轮候资格采证台

完全离线的虚构家庭申请、保障资格复核和房源配租三套业务账。材料受理时刻、成员冲突、收入住房核验与异议会改变轮候状态；资格迟到变更会释放未签约房源，签约会把家庭标为已安置。原始证据和每版结论留在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m housingqueue demo --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口为 `application-import`、`eligibility-import`、`allocation-import`，可用 `--file fixtures/*-ok.json` 或对应 `*-bad.json`。`report` 给出申请、资格、队列、房源、邀约与签约；`audit` 从原始文件在新库重放比对。所有家庭和房源纯属合成，不对接真实人口、税务或不动产系统；角色和签名只是离线标签。

本独立增强副本另有 `waste-import`：已安置家庭可交纸、塑料或金属并记录称重，`waste_decision` 保留入站毛重但**不再直接换算积分**；毛重永不被后续去皮、分拣或证书改写。

## 回收物料流与实际再生物积分（recycling-import）

`recycling-import` 在离线台账上重建从门店到加工厂的完整物料流，所有质量以整数克记账并满足质量守恒：

- **容器封签/投递装箱**（`container_seal`、`drop_pack`）：封签容器记录皮重，投递按逐笔 `waste_drop` 归因；同一投递不能在两个容器重复出现，装箱量不得超过入站毛重。
- **去皮复称/蒸发损耗**（`container_tare`、`evaporation_loss`）：毛重减皮重得净重，去皮差异、风干损耗分别有复称人/检测人依据并按实际切片分摊，不允许负重量。
- **容器拆分/合并**（`container_split`、`container_merge`）：转移在容器间成对记账，同一投入不会被两个下游容器重复持有。
- **出站与双边接收**（`shipment_out`、`plant_receive`）：门店出站单只代表在途；没有加工厂双边接收的车次永远不能记为已回收，也不能登记加工证书。
- **检测与分摊**（`contamination_check`、`recyclable_ratio`）：逐户（`scope=drop`）检测由对应投递自行承担污染物；只有混装后整体检测（`scope=batch`）时才按**事前公布**的可回收净重比例（基点）分摊，绝不按原毛重平均扣减。
- **两条加工路线**（`processing_outcome`）：`recycling` 产出实际再生物+残渣（授权次日分拣）；`energy` 把全部物料送合规能源回收，实际再生物为 0，两户不得领“回收积分”。证书产出（再生物+残渣+能源+损耗+逐户污染物）必须精确等于入厂质量，否则整文件拒绝并回滚。
- **积分**：`reward_decision.points` 只来自可归属的实际再生物质量（每 100 克 1 分），报告同时列出在途、加工、再生物、能源回收、残渣、待核质量和每户已核积分。
- **兑付与追偿**（`reward_payout`）：迟到复称或证书只修订未兑付积分；已兑付部分在 `reward_recovery` 形成可追偿差额，不伪造负重量。
- **重复证书**（`certificate_reuse`）：证书被另一车次引用时，冻结其覆盖的积分（`reward_lot.state=frozen`、证书 `state=duplicate`），原入站称重与实体交接全部保留。

来源事件遵循与其他入口相同的幂等规则：同内容重放幂等（文件级 `file_replayed`、事件级 `event_replayed`），同 id 异内容拒绝（`immutable event id conflict`）；`audit` 从原始文件重建全部质量流与积分账并逐表比对。

### 复现命令

```bash
# 1) 两个已安置家庭与投递基线（三个住房入口 + waste-import 保持不变）
for pair in "application-import recycling-application.json" \
            "eligibility-import recycling-eligibility.json" \
            "allocation-import recycling-allocation.json" \
            "waste-import recycling-waste.json"; do
  set -- $pair
  python -B -m housingqueue $1 --db demo-recycling.sqlite --file fixtures/$2
done

# 2) 路线一：整批检测 + 事前比例 + 授权次日分拣（6kg 再生物、2kg 残渣）
python -B -m housingqueue recycling-import --db demo-recycling.sqlite --file fixtures/recycling-route.json
# 路线二：合规能源回收（8kg 全有去向、再生物为 0、零回收积分），用独立库导入 fixtures/recycling-energy.json
# 容器拆合：fixtures/recycling-split-merge.json；去皮/蒸发：fixtures/recycling-tare.json
# 守恒违例整文件拒绝：fixtures/recycling-bad-conservation.json
# 兑付后重复证书：fixtures/recycling-reuse.json（积分冻结 + 可追偿差额）

# 3) 报告：在途/加工/再生物/能源回收/残渣/待核质量与每户已核积分
python -B -m housingqueue report --db demo-recycling.sqlite

# 4) 审计：从原始事件重建质量流与积分账
python -B -m housingqueue audit --db demo-recycling.sqlite
```

所有家庭、单位、加工厂和证书均为合成离线数据，不对接任何真实系统。
