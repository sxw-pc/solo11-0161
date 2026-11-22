# 保障房轮候资格采证台

完全离线的虚构家庭申请、保障资格复核、房源配租与社区议事账。材料受理时刻、成员冲突、收入住房核验与异议会改变轮候状态；资格迟到变更会释放未签约房源，签约会把家庭标为已安置。原始证据和每版结论留在 SQLite。

```powershell
python -m pip install . --no-deps --no-build-isolation
python -m housingqueue demo --db demo.sqlite
python -m housingqueue audit --db demo.sqlite
python -m unittest discover -s tests -v
```

三个独立入口为 `application-import`、`eligibility-import`、`allocation-import`，可用 `--file fixtures/*-ok.json` 或对应 `*-bad.json`。`report` 给出申请、资格、队列、房源、邀约与签约；`audit` 从原始文件在新库重放比对。所有家庭和房源纯属合成，不对接真实人口、税务或不动产系统；角色和签名只是离线标签。

## 社区议事与选举章程

`governance-import` 的遗留模式仍接受单个已安置家庭的 yes/no 票（`fixtures/governance-ok.json`）。采用选举章程的会议则使用以下事件：

| kind | 作用 |
| --- | --- |
| `meeting` | 建立会议窗口（`opens`/`closes`） |
| `charter` | **开票前**公示并锁定章程：`contest`、`roster_deadline`、`quorum`、`method`（`plurality` / `ir_elimination`）、`tie_break`（`candidate_order`）、`published_at`。锁定后不可更改 |
| `authorize` | 一住户给代理人的书面授权（授权人、代理人、时间、签名） |
| `ballot` | 排序票；`ranked` 为候选顺序，`signer` 须为名册截止时的有效申请人或经书面授权的代理人；空排序为弃权 |
| `correction` | 选票更正；仅在公告前生效，公告后重算生成新版本 |
| `withdrawal` | 候选人退选；公告后重算生成新版本 |
| `announce` | 开票并公告一版不可改写的裁决 |

名册在 **`roster_deadline` 时刻**冻结：家庭必须在 `allocation.sign` 的真实签约时刻（`signature.at`）已签约，且该户在截止时的有效申请人（`member.joined_at <= deadline` 的申请人）才进入名册。投票导入时 `household.status='housed'` 不再构成准入依据。

- 同一户本人与代理票不能各算一票：本人票有效时代理票标为 `duplicate_superseded`；两票代理互相竞争时都隔离为 `duplicate_agent_ballot`，不按到达顺序择一。
- 代理链有环 → `agent_cycle`；授权晚于名册截止 → `authorization_expired`；跨社区授权或跨社区签名人 → `agent_cross_community`；其余无有效授权 → `unauthorized_signer`。争议票先隔离，不参与计票。
- 出席数 = 有效票 + 弃权票；少于 `quorum` 时 `outcome='no_quorum'` 且不宣布胜者。弃权只计出席，不进入任何偏好计数。
- 两套章程均可执行。相对多数取每户第一偏好；即时淘汰制每轮淘汰最低票候选并把仍有效的下一偏好转移，最高票同票或最低票同票都按章程预先公布的 `candidate_order` 处理（不按 `event_id`）。
- 每次公告写入一条不可变的 `decision_version`，附 `decision_round`（每轮计票与转移）、`decision_ballot`（每票状态）、`decision_delta`（相对上一版改变的选票）、`roster_snapshot` 与 `evidence` JSON。公告后的重算追加新版本并列出版本间改变名次的选票，原公告不被改写；疑似伪造可通过 `audit` 暴露。

`event`/`source_file` 仍保证同 ID 异内容拒绝，`audit` 从原始文件重建每轮结果。

### 九户验收 fixture

```powershell
# 两种章程分别选出不同代表
python -m housingqueue application-import --db nine.sqlite --file fixtures/governance-nine-application.json
python -m housingqueue eligibility-import --db nine.sqlite --file fixtures/governance-nine-eligibility.json
python -m housingqueue allocation-import --db nine.sqlite --file fixtures/governance-nine-allocation.json
python -m housingqueue governance-import --db nine.sqlite --file fixtures/governance-nine-plurality.json
python -m housingqueue report --db nine.sqlite        # plurality: X (4 首选票)
python -m housingqueue audit --db nine.sqlite

python -m housingqueue governance-import --db nine-ir.sqlite --file fixtures/governance-nine-ir.json
# ir_elimination: Z 的 2 票转给 Y，Y 5:4 胜

# 截止后签约纠错：family-09 于 2026-05-03 签约，即时淘汰得 X 4 / Y 3 / Z 1，
# Z 转给 Y 后 4:4，按章程同票规则 X 胜
python -m housingqueue allocation-import --db late.sqlite --file fixtures/governance-nine-late-sign-allocation.json
python -m housingqueue governance-import --db late.sqlite --file fixtures/governance-nine-late-sign.json

# 代理重复/代理环
python -m housingqueue governance-import --db proxy.sqlite --file fixtures/governance-proxy-duplicate.json
python -m housingqueue governance-import --db proxy.sqlite --file fixtures/governance-proxy-cycle.json

# 公告后退选 / 选票更正 → 新裁决版本
python -m housingqueue governance-import --db nine.sqlite --file fixtures/governance-nine-withdrawal.json
python -m housingqueue governance-import --db nine-ir.sqlite --file fixtures/governance-nine-correction.json
```
