# 低空航线空域时窗审查台

用 **Python Flask + SQLite + 原生 JavaScript/SVG** 实现的本地审查工具：在画布上录入带**高度层、速度、起降时刻**的航点航段，维护带**生效区间**的禁飞多边形；系统把航段按走廊半宽展开为带时间的**扫掠走廊**，计算与禁飞区的三维冲突（**平面相交 ∩ 高度层重叠 ∩ 时间窗重叠**），给出**首个冲突区间**和**见证位置**；支持拖动航点/禁飞区顶点后自动重算，并可保存/载入命名版本。

此外支持**发布航线**：从任一命名版本取**航线**，配上**当前工作区的禁飞区**重新审查；仅当版本存在、航线含非零长度航段、审查无错误无冲突、且提交时读到的工作区修订号与已发布号未变化时，才原子落库一条**递增发布号**的发布记录（来源版本 ID、组合状态、审查结果快照）。发布记录不可改写：之后编辑工作区或删除来源版本都不影响已发布内容。

对已发布航线，另有**禁飞区变更核准**：已有发布记录时，页面对禁飞区的增删、拖动与修改（含载入版本带来的禁飞区变化）只存为**草稿**，航线编辑仍自动保存、但不顺带落库草稿禁飞区；提交草稿时**不预先保存工作区**——若有在途的航线自动保存则先等它落库以协调顺序与修订号，随后仅以一次核准请求携带**所见工作区修订号与发布号**，服务端把候选禁飞区与**最新发布快照中的航线**组合审查。审查报错、有冲突或任一号已变化时返回原因与冲突/并发明细，**修订号不增加**，草稿保留在本页可继续修改，该提交不改写工作区或已发布内容；通过时仅原子替换工作区禁飞区并递增修订号，最新工作区航线与发布快照保持不动。尚未发布时禁飞区沿用原自动保存。

对已发布航线还可**登记航次**：侧栏输入起飞时刻、按**当前发布号**登记一次执行。服务端在单个事务内取该发布快照中的航线、**仅替换起飞时刻**（航线几何/速度/走廊等全部沿用快照），与**当前工作区已核准禁飞区**重新审查：无发布、提交的发布号已变、时刻无效或审查报错/冲突时一律拒绝且**不留任何航次记录**；安全时原子落库航次编号、发布号、起飞时刻与**航线快照**（工作区与发布快照均不变），供页面查询与结束航次。航次执行期间（未结束）会持续约束禁飞区：提交禁飞区草稿核准时，除原有的“候选禁飞区 ∩ 最新发布快照航线”审查外，还须用候选禁飞区**复核所有未结束航次**——包括登记于旧发布号的航次（各自沿用其自身发布快照航线与登记起飞时刻）；任一冲突即返回 `voyage_conflicts`，附**航次及冲突明细**，草稿、工作区与发布快照均不变。登记航次与提交核准均在单个 `BEGIN IMMEDIATE` 事务内读取“当时全部未结束航次”，SQLite 写事务串行使两类操作交错时**不会漏检任何已登记航次**；航次结束（幂等）后不再阻拦后续核准，但航次记录仍可查询。

已登记的未结束航次可**改期**：侧栏选取航次、输入新起飞时刻并携带**所见原时刻**提交。服务端保留航次编号、登记时间与原发布号，仅替换**该航次自己保存的航线快照**中的起飞时刻，用**当前已核准禁飞区**重新审查——即使已有新发布，也沿用原航次的航线几何。航次不存在或已结束、原时刻已被他页改动（乐观并发）、输入无效、审查报错或有冲突时返回原因及相应明细，**原时刻与快照不变**；通过时在单个事务内**原子更新同一航次**的时刻与快照。改期、结束与禁飞区核准交错时，核准事务始终读到**最终生效的未结束航次**复核，不漏检。页面展示更新结果并保留被拒的输入。

为避免多页面互相覆盖，已发布后的航线自动保存会在 `PUT /api/state` 请求头携带所见修订号（`X-Expected-Workspace-Rev`）。他页已先变更时本次保存收到 `409 stale` 且**不写库**，页面取回服务器最新工作区后按三种情况分流，**本页未保存航线、禁飞区草稿与服务器最新航线三者始终同时保留**：

1. **本页航线相对编辑前所见未改**（本页没有待保存的航线改动）：直接采用服务器最新航线与他页已核准禁飞区，不再写库；本页禁飞区草稿保留。
2. **本页航线已改，但服务器航线仍与本页编辑前所见一致**（差异仅是他页核准了禁飞区）：合入已核准禁飞区（仅更新基线，**草稿不落库、不进请求体**），用取回的最新修订号仅带本页航线再做一次条件写入，成功后提示用户；连续多轮撞 409 时最多自动合入重试 2 次。
3. **双方都改了航线**：**暂停自动保存**，侧栏顶部展示「本页航线 / 服务器最新航线」双方摘要供选择：
   - **加载服务器航线**：本页航线切换为他页已保存航线（无需写库），**禁飞区草稿原样保留**，自动保存恢复；
   - **确认提交本页航线**：以所见最新修订号做条件写入；若再遇 `409`，重新按上述规则分流——仅禁飞区被他页核准就合入，航线仍冲突则**继续保留本页编辑并再次展示冲突**，绝不静默覆盖或提示成功。

保存定时器或在途保存请求与禁飞区核准/发布相邻时，若在途保存落为未解决的航线冲突，核准/发布一律中止并提示先解决冲突，不会把「航线冲突的保存」当成保存成功而继续核准；已核准禁飞区与发布快照不受自动保存影响。未发布时的自动保存不携带该头，保持原无条件落库行为。

### 打开页面时的发布状态加载竞态

`GET /api/state` 通常比 `GET /api/release` 先返回：发布状态尚未读到时页面**已经可编辑**，但此时**无法区分“确无发布”与“已有发布但响应还没到”**。为此页面维护一个四态发布状态（`loading / published / none / error`），并用一道就绪承诺门控**所有工作区写请求**（防抖自动保存、载入版本时的保存、禁飞区核准、发布前的待发保存）：

- **`loading`（发布状态未明确）**：页面保持可编辑，禁飞区/航线编辑照常进画布与本地审查，但**不发任何工作区写请求**（不会因为 `currentRelease` 尚为空就误走未发布分支做无条件 `PUT /api/state`，从而避免把草稿禁飞区写入已发布工作区、或用他页已核准前的旧修订号覆盖他页航线）；待发保存会等状态明确后再按对应分支发出。
- **确认已有发布（`published`）**：禁飞区编辑仍是**待核准草稿**（不落库）；航线编辑按**所见修订号条件保存**（携带 `X-Expected-Workspace-Rev`，走上述 409 三分流）。
- **确无发布（`none`）**：沿用原无条件自动保存。
- 若 `GET /api/state` 与 `GET /api/release` 两次读取之间**工作区修订号已变**，**不把新修订号配编辑前所见的旧航线写回**：先再次 `GET /api/state` 取回最新工作区，**合入他页已核准的禁飞区**（本页有草稿则只换基线、草稿不进请求体），再按航线变化分流：
  - **远端航线未变**（差异仅是他页核准了禁飞区）：采用最新修订号；本页改了航线就用最新修订号条件保存，本页没改则不写库。
  - **仅他页改了航线**（本页航线仍与编辑前所见一致）：直接采用远端航线，**不发写请求**，本页禁飞区草稿保留。
  - **双方都改了航线**：沿现有冲突选择流程处理（暂停自动保存、并列双方航线供二选一）并保留草稿。
- **协调所需的再次 `GET /api/state` 失败**：`/api/release` 已把权威新修订号告诉本页，若此刻放行待发保存，条件 `PUT` 携带“**新修订号 + 编辑前所见旧航线/旧禁飞区**”会被服务端当作最新内容 **200 接受**（而非 409），把他页的航线改动与已核准禁飞区整份覆盖。因此失败时：**保留本页航线与禁飞区草稿**，侧栏「当前发布」处提示并提供「重试」，**阻断自动保存以及相邻的禁飞区核准、发布写入**（就绪承诺保持挂起）；点重试取回最新工作区后再按上面三分支处理，随后才放行待发保存。
- **读取失败（`error`）**：保留页面编辑、侧栏提示刷新（可点“重试”重新读取），**绝不当作“从无发布”**而放行无条件保存；重试期间写请求继续被门控。

## 一条命令启动

```bash
docker compose up --build
```

启动后浏览器访问：

> **http://localhost:8000**

（容器内 gunicorn 监听 `0.0.0.0:8000`，SQLite 数据文件持久化在 Docker 卷 `airspace-data` 中的 `/data/console.db`。）

不用 Docker 时：

```bash
pip install -r requirements.txt
python app.py          # 同样监听 http://localhost:8000
```

## 模型与判定口径

画布为**局部平面坐标系**（高度 m；速度 km/h；时间本地时区）。坐标单位不预设，通过侧栏「画布比例（单位/km）」声明 **1 km 对应多少画布单位**（默认 100，即画布单位视为 10 m），航段长度据此换算。

### 航线
- 有序航点 `(x, y, 高度)`；相邻航点构成航段。
- 每段独立速度（km/h），由 `起飞时刻 + Σ 航段长度/速度` 逐点推出 ETA（航点预达时刻），长度 = 画布距离 ÷ 画布比例。
- 航段折线按「走廊半宽（km）」做 round-cap/round-join 缓冲（`shapely.buffer`），即扫掠走廊平面范围；画布上以同宽描边近似可视化。

### 禁飞区
- 多边形顶点（画布上绘制或表单维护）。
- 高度层 `[minAlt, maxAlt]`（m）。
- 生效时窗 `[startTime, endTime]`，端点可空表示不限。
- 可勾选启用/停用。

### 三维 + 时间冲突
对每条航段与每个禁飞区：

1. **平面（按连通部件分别计算，不合并）**：走廊缓冲多边形 ∩ 禁飞多边形；相交结果可能是 `MultiPolygon / MultiLineString / MultiPoint / GeometryCollection`，系统先递归展平为**各自独立的连通部件**，再把每个部件分别投影到航段方向，得到一组相互独立的航段参数区间 `f∈[0,1]`。航段先穿多边形一侧、再穿另一侧（如凹形禁飞区）时，两段区间**中间的空隙保留为空隙**，不会被外包区间 `[f_min, f_max]` 填平。
2. **高度（逐部件求交）**：航段高度在 `a0→a1` 间线性变化，先求出落在禁飞高度层 `[minAlt,maxAlt]` 的 `f` 子区间（解析端点裁剪），再**分别**与每个平面部件区间求交。
3. **时间（逐段映射、逐段截短）**：把每个子区间独立映射为绝对时刻区间，再与禁飞生效窗求交；空窗端点视为 ±∞。区间被时窗截短后，按**实际进入/退出时刻反推最终的 `f`**，因此 `enterFrac/exitFrac` 与 `enterTime/exitTime` 始终对应同一段最终冲突。
4. 三者按**闭区间实际重叠**判定，非空即冲突：平面上的线接触（走廊边界擦过多边形边）、点接触（擦过顶点/圆端帽尖点），以及时刻区间端点恰好落在生效窗沿，都算命中。平面区间退化为点、或被时窗夹成点时报告为瞬时擦碰（`instant: true`）。

每条结果的 `enterFrac、exitFrac、enterTime、exitTime、altLow/altHigh、witness` 都取自**同一段最终冲突**；时窗截短区间时，见证点随实际进入时刻移动到被截短区间的进入端中心线位置。多部件相交会输出多条互不合并的结果，结果按实际进入时刻排序，**首个冲突取最早进入的一段**，画布以红色★标出。

> 注意：走廊缓冲采用 round 端帽，航段衔接处为圆弧过渡；冲突线段在画布上沿中心线绘制，宽度等于走廊宽，仅供定位参考，**计算以 shapely 的真实缓冲区相交为准**。


## 操作说明

| 操作 | 方式 |
|---|---|
| 平移 / 缩放 | 拖动空白处平移；滚轮以光标为中心缩放 |
| 追加航点 | 工具栏「＋ 航点」→ 单击画布；侧栏可改名称/坐标/高度 |
| 航段速度 | 侧栏航点卡片之间的航段行（km/h），ETA 自动刷新 |
| 画禁飞区 | 「✎ 绘制禁飞区」→ 单击落点，**双击或回车**完成；Esc 取消 |
| 编辑禁飞区 | 点选多边形后，画布上拖动黄色顶点，侧栏改高度/时窗/名称 |
| 重算 | 任何编辑后自动重算（约 350ms 防抖），也可点「重新审查」 |
| 保存版本 | 顶部输入版本名（可空，自动用时间戳）→「💾 保存版本」；侧栏可载入/删除 |
| 发布航线 | 版本列表点「发布」：取该版本航线 + 当前工作区禁飞区重新审查，通过后落库；侧栏「当前发布」查看发布号、来源版本与审查结果 |
| 禁飞区变更核准 | 已有发布时，禁飞区编辑先存为草稿（不落库）；侧栏「提交核准」与发布快照航线组合审查，通过才替换工作区禁飞区；「放弃草稿」还原为已核准内容 |
| 航次登记 | 侧栏「航次登记」输入起飞时刻，按当前发布号登记；服务端以发布快照航线替换起飞时刻、与已核准禁飞区重审，安全才落库；航次列表可查询、执行中点「结束航次」 |
| 航次改期 | 航次列表对执行中航次点「改期」，输入新起飞时刻提交（携带所见原时刻）；服务端仅替换该航次航线快照中的起飞时刻、与当前已核准禁飞区重审，通过才原子更新；被拒时原因与明细展示、输入保留 |
| 双页航线冲突 | 两页各改航线、后保存方撞 409 时自动保存暂停：侧栏顶部对比双方航线，可选「加载服务器航线」（草稿保留）或「确认提交本页航线」（最新修订号条件写入，再撞 409 仍保留并重新提示） |

首次启动会写入一条演示数据（4 个航点、3 个禁飞区，含平面+高度+时窗命中、高度不命中、时窗不命中三种样例）。

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/state` | 当前工作状态（响应头 `X-Workspace-Rev` 为工作区修订号） |
| PUT | `/api/state` | 保存工作状态（编辑时自动防抖落库；修订号 +1，响应 `{ok, rev}`）。可选请求头 `X-Expected-Workspace-Rev`：携带时仅当工作区修订号仍等于该值才写入，否则 `409 {"reason":"stale","details":...}` 且不写库、不递增修订号（已发布后航线自动保存用它防止覆盖他页内容；未发布时不携带，保持无条件保存）。页面收到 409 后**不再盲目换新号整份重写**，而是取回最新工作区分流：服务器航线未变（仅禁飞区获核准）才合入已核准禁飞区后条件重试；双方都改航线则暂停自动保存、并列双方航线供选择 |
| POST | `/api/analyze` | 提交状态，返回航段时刻、错误、全部冲突条目、首个冲突 |
| GET | `/api/versions` | 版本列表 |
| POST | `/api/versions` | 保存命名版本 `{name, state}` |
| GET/DELETE | `/api/versions/<id>` | 取出版本快照 / 删除 |
| GET | `/api/release` | 当前发布（发布号、来源版本、组合状态、审查结果）及当前 `workspaceRev`/`releaseSeq` |
| POST | `/api/release` | 发布航线 `{versionId, workspaceRev, releaseSeq}`，见下文示例 |
| POST | `/api/nfz-approval` | 已发布航线的禁飞区变更核准 `{nfzs, workspaceRev, releaseSeq}`，见下文示例；除最新发布航线审查外还复核所有未结束航次 |
| GET | `/api/voyages` | 全部航次（新登记在前）：航次编号、发布号、起飞时刻、航线快照 `routeSnapshot`、结束时刻 `endedAt` 与是否未结束 `open` |
| POST | `/api/voyages` | 登记航次 `{releaseSeq, departureTime}`，见下文示例 |
| POST | `/api/voyages/<id>/reschedule` | 航次改期 `{departureTime, originalDepartureTime}`，见下文示例 |
| POST | `/api/voyages/<id>/end` | 结束航次（幂等；不存在 → 404）；结束后不再阻拦禁飞区核准 |

### 发布航线（POST /api/release）

请求体三个字段均为整数：`versionId`（来源版本）、`workspaceRev`（读取时的工作区修订号）、`releaseSeq`（读取时的已发布号，从未发布为 `0`）。服务器在**单个事务**内依次校验，任一不满足即整体回滚、**原发布不变**：

| 情形 | 状态码 | 响应 `reason` | 说明 |
|---|---|---|---|
| 字段缺失/非整数 | 400 | `missing_fields` | — |
| 版本不存在或已删除 | 404 | `version_not_found` | — |
| 工作区或已发布号已变化 | 409 | `stale` | `details` 附 `{expected, actual}` 明细 |
| 航线没有非零长度航段 | 400 | `no_nonzero_segment` | — |
| 审查存在错误 | 400 | `analysis_errors` | `errors` 附错误列表 |
| 审查存在冲突 | 409 | `analysis_conflicts` | `entries`/`first` 附冲突明细 |
| 通过 | 200 | — | 原子写入 `{ok, release}`，发布号递增 |

发布记录包含递增发布号 `seq`、来源版本 `sourceVersionId`/`sourceVersionName`、组合状态 `state`（版本航线 + 工作区禁飞区）与审查结果 `analysis`，落库后不再被改写。

### 禁飞区变更核准（POST /api/nfz-approval）

已有发布记录时，页面上的禁飞区变更只存为草稿；本接口把**候选禁飞区**与**最新发布快照中的航线**组合审查。请求体：`nfzs`（候选禁飞区列表，可为空数组）、`workspaceRev`、`releaseSeq`（均为整数）。服务器在**单个事务**内依次校验，任一不满足即整体回滚——**工作区与已发布内容均不变**，草稿可修改后重新提交：

| 情形 | 状态码 | 响应 `reason` | 说明 |
|---|---|---|---|
| 字段缺失 / `nfzs` 非列表 | 400 | `missing_fields` | — |
| 工作区或已发布号已变化 | 409 | `stale` | `details` 附 `{expected, actual}` 明细 |
| 尚无发布记录 | 409 | `no_release` | 未发布时禁飞区随工作区自动保存，无需核准 |
| 审查存在错误 | 400 | `analysis_errors` | `errors` 附错误列表 |
| 审查存在冲突（最新发布航线） | 409 | `analysis_conflicts` | `entries`/`first` 附冲突明细 |
| 候选与未结束航次冲突 | 409 | `voyage_conflicts` | `voyageConflicts` 附逐航次的 `voyageId`/`releaseSeq`/`departureTime`/`entries`/`first` |
| 通过 | 200 | — | `{ok, rev, releaseSeq}`：仅原子替换工作区禁飞区，修订号 +1 |

注意审查用的是**发布快照中的航线**，不是当前工作区航线：工作区航线在发布后被继续编辑不影响核准判定；通过时也只替换工作区禁飞区，最新工作区航线与发布快照都保持不动。

页面提交流程要点：「提交核准」**不会先 PUT 工作区**——页面只在有待发的航线自动保存时先等它落库（保证提交的是所见最新修订号），随后仅发出一次本接口请求；冲突、审查错误或修订号/发布号已变化时修订号都不增加，草稿原样保留并展示上述明细。核准成功后页面只把禁飞区基线更新为候选内容，航线仍是最新工作区航线。

### 航次登记（POST /api/voyages）与结束（POST /api/voyages/&lt;id&gt;/end）

请求体：`releaseSeq`（当前发布号，整数）、`departureTime`（起飞时刻字符串）。服务端在**单个事务**内：核对发布号即当前最新发布（无发布 / 号已变直接拒绝）→ 取该发布**快照航线**并仅把起飞时刻替换为提交值 → 与**当前工作区已核准禁飞区**组合审查。任一环节失败都整体回滚，**不留航次记录、不改工作区与发布快照**：

| 情形 | 状态码 | 响应 `reason` | 说明 |
|---|---|---|---|
| 缺少/非整数 `releaseSeq` | 400 | `missing_fields` | — |
| 起飞时刻缺失/格式错误 | 400 | `invalid_departure_time` | — |
| 尚无发布记录 | 409 | `no_release` | — |
| 提交的发布号已非当前发布号 | 409 | `stale` | `details.releaseSeq` 附 `{expected, actual}` |
| 审查存在错误 | 400 | `analysis_errors` | `errors` 附错误列表 |
| 与已核准禁飞区冲突 | 409 | `analysis_conflicts` | `entries`/`first` 附冲突明细 |
| 通过 | 200 | — | `{ok, voyage}`：落库航次，`voyage` 含 `id`（航次编号）、`releaseSeq`、`departureTime`、`routeSnapshot`（替换了起飞时刻的航线快照）、`createdAt`、`endedAt`（未结束为 `null`）、`open: true` |

`GET /api/voyages` 返回 `{ok, voyages:[...]}`（新登记在前），已结束航次 `open:false` 并带 `endedAt`。`POST /api/voyages/<id>/end` 幂等结束航次（重复调用不改变首次结束时刻），结束后该航次不再参与核准复核；航次不存在返回 `404 {"reason":"voyage_not_found"}`。

要点：登记审查用的是**发布快照航线的几何 + 新起飞时刻**——即使同一航线在发布时刻安全，换一个起飞时刻可能落入某已核准禁飞区的生效窗而被拒；登记时工作区航线是什么无关。登记不改变工作区修订号与发布号。新发布出现后，旧航次仍按其自身的发布快照与登记时刻复核，直到被结束。

### 航次改期（POST /api/voyages/&lt;id&gt;/reschedule）

请求体：`departureTime`（新起飞时刻字符串）、`originalDepartureTime`（页面所见原时刻，用于乐观并发）。服务端在**单个事务**内：核对航次存在且未结束 → 所见原时刻与库中保存值一致 → 取**该航次自己保存的航线快照**并仅把起飞时刻替换为新值（几何/速度/走廊等不动，即使已有新发布也沿用原航次航线）→ 与**当前工作区已核准禁飞区**组合审查。任一环节失败都整体回滚，**原时刻与快照不变**：

| 情形 | 状态码 | 响应 `reason` | 说明 |
|---|---|---|---|
| 缺少 `originalDepartureTime` | 400 | `missing_fields` | — |
| 新起飞时刻缺失/格式错误 | 400 | `invalid_departure_time` | — |
| 航次不存在 | 404 | `voyage_not_found` | — |
| 航次已结束 | 409 | `voyage_ended` | 结束后航次记录仍可查询，但不能再改期 |
| 原时刻已被他页改动 | 409 | `stale` | `details.departureTime` 附 `{expected, actual}`；原时刻无法解析视同不一致 |
| 审查存在错误 | 400 | `analysis_errors` | `errors` 附错误列表 |
| 与已核准禁飞区冲突 | 409 | `analysis_conflicts` | `entries`/`first` 附冲突明细 |
| 通过 | 200 | — | `{ok, voyage}`：原子更新同一航次的 `departureTime` 与 `routeSnapshot`，`id`/`createdAt`/`releaseSeq` 不变 |

改期不校验当前发布号（旧发布航次可改期），也不改变工作区修订号与发布号。改期、结束与禁飞区核准交错时，三者均在 `BEGIN IMMEDIATE` 事务内串行：核准读到的是**最终生效的未结束航次**（含改期后的新时刻），改期与结束也总是看到最新已核准禁飞区，不会漏检。页面上被拒的输入保留在改期表单中，`stale` 时同步最新航次列表后以最新原时刻为基线重提。

### 可执行示例（端口 8000）

服务启动后（`docker compose up --build` 或 `python app.py`），依次执行：

```bash
# ① 保存工作区（本例清空禁飞区；发布用的是“当前工作区”的禁飞区），记下返回的 rev
curl -s -X PUT http://localhost:8000/api/state \
  -H 'Content-Type: application/json' \
  -d '{"route":{"departureTime":"2026-09-24T08:00","unitScale":100,"corridorHalfWidth":0.3,"speeds":[25],"waypoints":[{"id":"w1","x":100,"y":100,"alt":200},{"id":"w2","x":500,"y":100,"alt":200}]},"nfzs":[]}'
# → {"ok":true,"rev":2}

# ② 保存一个命名版本（发布只取它的“航线”），记下返回的 id
curl -s -X POST http://localhost:8000/api/versions \
  -H 'Content-Type: application/json' \
  -d '{"name":"发布演示","state":{"route":{"departureTime":"2026-09-24T08:00","unitScale":100,"corridorHalfWidth":0.3,"speeds":[25],"waypoints":[{"id":"w1","x":100,"y":100,"alt":200},{"id":"w2","x":500,"y":100,"alt":200}]},"nfzs":[]}}'
# → {"ok":true,"id":1,"name":"发布演示",...}

# ③ 发布：versionId=②的 id，workspaceRev=①的 rev，releaseSeq=当前已发布号（首次为 0）
curl -s -X POST http://localhost:8000/api/release \
  -H 'Content-Type: application/json' \
  -d '{"versionId":1,"workspaceRev":2,"releaseSeq":0}'
# → {"ok":true,"release":{"seq":1,"sourceVersionId":1,"sourceVersionName":"发布演示",...}}

# ④ 查看当前发布（发布号、来源、组合状态、审查结果）
curl -s http://localhost:8000/api/release

# ⑤ 已有发布后，提交候选禁飞区核准（与发布快照航线不冲突）→ 通过，工作区禁飞区被替换
#    workspaceRev/releaseSeq 用 GET /api/release 读到的当前值（此处为 2 和 1）
curl -s -X POST http://localhost:8000/api/nfz-approval \
  -H 'Content-Type: application/json' \
  -d '{"nfzs":[{"id":"n9","name":"远侧限飞区","vertices":[[100,500],[200,500],[200,600],[100,600]],"minAlt":0,"maxAlt":400,"startTime":"","endTime":"","enabled":true}],"workspaceRev":2,"releaseSeq":1}'
# → {"ok":true,"rev":3,"releaseSeq":1}

# ⑥ 核准生效后的工作区：禁飞区已替换、航线保留、修订号变为 3；发布快照不变
curl -s http://localhost:8000/api/state

# ⑦ 候选禁飞区压住发布快照航线 → 409 附冲突明细，工作区与已发布内容均不变
curl -s -X POST http://localhost:8000/api/nfz-approval \
  -H 'Content-Type: application/json' \
  -d '{"nfzs":[{"id":"nX","name":"阻断区","vertices":[[250,50],[350,50],[350,150],[250,150]],"minAlt":0,"maxAlt":400,"startTime":"2026-09-24T07:00","endTime":"2026-09-24T09:00","enabled":true}],"workspaceRev":3,"releaseSeq":1}'
# → 409 {"ok":false,"reason":"analysis_conflicts","entries":[...],"first":{...}}

# ⑧ 航次登记：按当前发布号（①–④后为 1）+ 起飞时刻；服务端以发布 #1 快照
#    航线替换起飞时刻，与工作区已核准禁飞区（⑤的“远侧限飞区”）重新审查
curl -s -X POST http://localhost:8000/api/voyages \
  -H 'Content-Type: application/json' \
  -d '{"releaseSeq":1,"departureTime":"2026-09-24T08:00"}'
# → {"ok":true,"voyage":{"id":1,"releaseSeq":1,"departureTime":"2026-09-24T08:00",
#     "routeSnapshot":{...发布快照航线，起飞时刻已替换...},"endedAt":null,"open":true,...}}

# ⑨ 查询航次（新登记在前；已结束航次带 endedAt、open=false）
curl -s http://localhost:8000/api/voyages
# → {"ok":true,"voyages":[{"id":1,...}]}

# ⑩ 航次未结束时，候选禁飞区还要复核该航次：夜间窗对发布快照（08:00）安全，
#    但若该航次登记为 20:30 起飞就会冲突；此处先再登记一个夜间航次试试——
#    ⑤核准的“远侧限飞区”无时间窗但不在航线附近，所以 20:30 航次仍可登记：
curl -s -X POST http://localhost:8000/api/voyages \
  -H 'Content-Type: application/json' \
  -d '{"releaseSeq":1,"departureTime":"2026-09-24T20:30"}'
# → {"ok":true,"voyage":{"id":2,...}}
#    再提交夜间窗候选：发布快照航线 08:00 不冲突，但航次 #2（20:30）冲突 →
curl -s -X POST http://localhost:8000/api/nfz-approval \
  -H 'Content-Type: application/json' \
  -d '{"nfzs":[{"id":"nN","name":"夜间管控区","vertices":[[250,50],[350,50],[350,150],[250,150]],"minAlt":0,"maxAlt":400,"startTime":"2026-09-24T20:00","endTime":"2026-09-24T21:00","enabled":true}],"workspaceRev":3,"releaseSeq":1}'
# → 409 {"ok":false,"reason":"voyage_conflicts","voyageConflicts":[{"voyageId":2,"releaseSeq":1,"departureTime":"2026-09-24T20:30","entries":[...],"first":{...}}]}
#    草稿、工作区（仍是“远侧限飞区”）、发布快照均不变

# ⑪ 结束航次 #2（幂等）后再提交同一候选 → 通过（08:00 的发布快照与航次 #1 都不冲突）
curl -s -X POST http://localhost:8000/api/voyages/2/end
# → {"ok":true,"voyage":{"id":2,"open":false,"endedAt":"..."}}
curl -s -X POST http://localhost:8000/api/nfz-approval \
  -H 'Content-Type: application/json' \
  -d '{"nfzs":[{"id":"nN","name":"夜间管控区","vertices":[[250,50],[350,50],[350,150],[250,150]],"minAlt":0,"maxAlt":400,"startTime":"2026-09-24T20:00","endTime":"2026-09-24T21:00","enabled":true}],"workspaceRev":3,"releaseSeq":1}'
# → {"ok":true,"rev":4,"releaseSeq":1}

# ⑫ 航次改期：携带所见原时刻提交新起飞时刻；服务端保留航次编号/登记时间/
#     原发布号，仅替换该航次航线快照中的起飞时刻，用当前已核准禁飞区重审。
#     航次 #1 当前起飞时刻为 08:00（⑧登记）：
curl -s -X POST http://localhost:8000/api/voyages/1/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"departureTime":"2026-09-24T10:00","originalDepartureTime":"2026-09-24T08:00"}'
# → {"ok":true,"voyage":{"id":1,"releaseSeq":1,"departureTime":"2026-09-24T10:00",
#     "routeSnapshot":{...同一航线快照，起飞时刻已替换...},"createdAt":"...(不变)...","open":true}}
# 原时刻已被他页改动 → 409 附明细，库中时刻与快照不变：
curl -s -X POST http://localhost:8000/api/voyages/1/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"departureTime":"2026-09-24T12:00","originalDepartureTime":"2026-09-24T08:00"}'
# → 409 {"ok":false,"reason":"stale",
#     "details":{"departureTime":{"expected":"2026-09-24T08:00","actual":"2026-09-24T10:00"}}}
# 改进已核准禁飞区生效窗（⑪的夜间管控区 20:00–21:00）→ 409 附冲突明细：
curl -s -X POST http://localhost:8000/api/voyages/1/reschedule \
  -H 'Content-Type: application/json' \
  -d '{"departureTime":"2026-09-24T20:30","originalDepartureTime":"2026-09-24T10:00"}'
# → 409 {"ok":false,"reason":"analysis_conflicts","entries":[...],"first":{...}}
# 航次已结束/不存在 → 409 voyage_ended / 404 voyage_not_found；
# 新时刻无效 → 400 invalid_departure_time

# 时刻无效 / 发布号已变 / 无发布 → 拒绝且不留记录：
#   400 {"reason":"invalid_departure_time"} / 409 {"reason":"stale","details":{...}}
#   / 409 {"reason":"no_release"}
```

> 提示：工作区再被编辑后 `workspaceRev` 会变化、每次成功发布后 `releaseSeq` 会递增；用旧值重新发布或提交核准会得到 `409 {"reason":"stale","details":...}`，先 `GET /api/release` 取最新两个号再重试即可。

## 回归测试

判定口径（含凹形禁飞区空隙误报、多部件两段命中、时窗截短见证点移动、线/点/时窗端点擦碰）由 `tests/test_analyze.py` 覆盖；发布航线（组合审查、递增发布号、乐观并发、失败回滚、发布不可变）由 `tests/test_release.py` 覆盖；禁飞区变更核准（发布快照航线组合审查、仅替换工作区禁飞区、乐观并发、失败不改写）由 `tests/test_nfz_approval.py` 覆盖；**航次登记/结束与核准对航次的复核**（快照航线替换起飞时刻后与已核准禁飞区重审、无发布/号变/时刻无效/报错/冲突均拒绝不留记录、航次快照与列表、结束幂等与 404、未结束航次——含旧发布航次——阻拦核准并逐航次附冲突明细、结束后放行、登记与核准交错不漏检）由 `tests/test_voyages.py` 覆盖；**航次改期**（保留编号/登记时间/原发布号、仅替换该航次快照起飞时刻并与当前已核准禁飞区重审、已有新发布仍沿用原航次航线几何、不存在/已结束/原时刻被改动/输入无效/报错/冲突均拒绝且原时刻与快照不变、改期-结束-核准交错按最终生效的未结束航次复核不漏检）由 `tests/test_voyage_reschedule.py` 覆盖；页面提交（核准不预存工作区、相邻保存与核准的修订号协调、条件 PUT 409 合流）与**双页面交错**（他页核准后本页保存不得覆盖已核准禁飞区、**两页各改航线时后保存方 409 后不得换新号整份覆盖、双方航线并列可选、加载远端保留禁飞区草稿、确认提交用最新修订号条件写入且再撞 409 仍保留本页编辑**、在途保存与核准相邻时未解决冲突不得继续核准、发布号变化拒绝、快照不动）以及**启动请求交错**（`GET /api/state → GET /api/release` 期间他页改航线/核准禁飞区并推进修订号：协调再读失败时不得用新修订号把旧航线/旧禁飞区整份写回、保留草稿并阻断自动保存及相邻核准/发布、重试取回后三分支分流——远端未变按最新修订号条件保存、仅他页改航线直接采用远端不写库、双方都改才进冲突选择；另含“新号配旧内容会被服务端 200 接受”的危害固定用例）由 `tests/test_workspace_concurrency.py` 覆盖，均仅用标准库 `unittest`：

```bash
python3 -m unittest discover -s tests        # 需已 pip install -r requirements.txt
```

## 目录结构

```
app.py                 # Flask 后端：SQLite、时窗/高度/平面冲突计算（Shapely）、发布航线、禁飞区变更核准、航次登记/改期/结束
tests/test_analyze.py  # 判定口径回归（凹形空隙、两段命中、时窗截断、擦碰）
tests/test_release.py  # 发布航线回归（组合审查、并发回退、不可变性）
tests/test_nfz_approval.py  # 禁飞区变更核准回归（快照航线审查、草稿提交、并发回退）
tests/test_voyages.py   # 航次回归（快照换时刻登记、拒绝不留记录、未结束航次阻拦核准、结束放行、交错不漏检）
tests/test_voyage_reschedule.py  # 航次改期回归（快照换时刻原子更新、原时刻乐观并发、拒绝不变、交错复核不漏检）
tests/test_workspace_concurrency.py  # 页面提交、双页面交错与启动请求交错回归（核准不预存、条件 PUT、修订号协调、协调失败阻断）
templates/index.html   # 单页布局
static/app.js          # 原生 JS：SVG 渲染、拖动、绘制、重算、版本、发布、禁飞区草稿、航次登记/改期
static/style.css       # 样式
Dockerfile             # python:3.12-slim + gunicorn
docker-compose.yml     # 一条命令启动，端口 8000，卷持久化
requirements.txt
```
