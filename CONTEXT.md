# OIR 领域语言

本文固定 Open Intent Router 的通用领域词义，供需求、设计、代码命名和协作沟通共同使用。它只定义概念，不记录实现、配置、任务状态或阶段性结论。

## Language

### 系统边界

**Open Intent Router（OIR）**：
接收自然语言请求并提供意图路由、Agent 编排和受治理上下文能力的通用产品。
_避免_：IRS、OAC、中控旧系统

**OIR Core**：
OIR 中不依赖任何特定宿主、业务或第三方平台的通用领域部分。
_避免_：OAC Core、IRS Core、Host Adapter

**Host App（宿主应用）**：
拥有用户体验、用户身份和业务流程，并把自然语言能力接入 OIR 的应用。
_避免_：OIR Core、Host Adapter

**Host Adapter（宿主适配器）**：
在 Host App 专有契约与 OIR 通用契约之间隔离并转换语义的边界。
_避免_：OIR Core、业务 Service、通用 Agent Adapter

**OAC**：
接入 OIR 的一个具体 Host App；其业务词汇和授权语义不属于 OIR Core。
_避免_：OIR、Host Adapter、IRS

**IRS**：
OAC 曾依赖、现由 OIR 逐步替代的旧中控系统。
_避免_：OIR Legacy 模式、OIR Core、Host Adapter

**Native Contract（原生契约）**：
由 OIR 自身领域语言定义、供新接入方直接使用的通用契约。
_避免_：IRS Contract、Legacy Contract

**Legacy Contract（兼容契约）**：
为既有调用方保留的旧协议语义；它由 Host Adapter 承接，不反向定义 OIR Core。
_避免_：Native Contract、已停用契约

**Canonical Data（规范事实）**：
对某一领域事实具有最终解释权的受治理状态。
_避免_：缓存、展示投影、派生索引

**Derived Projection（派生投影）**：
从 Canonical Data 生成、可丢弃并可重建的查询、展示或检索表示。
_避免_：事实源、Canonical Data

### 身份与权限

**Principal（主体）**：
一次受信请求中被认证的用户或服务身份。
_避免_：未经验证的 body user、Session

**Tenant（租户）**：
隔离身份、权限和领域数据的首要所有权边界。
_避免_：Group、Role、Host App

**User Context（用户上下文）**：
OIR 用于路由和执行授权的规范化用户身份与权限投影。
_避免_：聊天上下文、前端任意 metadata

**Entitlement（授权能力）**：
Principal 被明确授予的一项稳定能力，用于判断其能否访问某个 Agent 或操作。
_避免_：Role、Group、页面标签

### 路由与编排

**Intent（意图）**：
用户当前希望达成的结果，而不是实现该结果的 Agent 名称或页面入口。
_避免_：Agent ID、Route Action、关键词标签

**Agent**：
可被 OIR 路由到的能力单元，可以执行后端能力、工作流或 Host UI 交接，并不限定为 LLM Bot。
_避免_：统一称作 Bot、统一称作子智能体

**Agent Definition（Agent 定义）**：
对一个 Agent 的稳定身份、能力、访问边界、输入输出与调用形态的声明。
_避免_：Agent 实例、Agent Run、业务配置杂项

**Agent Registry（Agent 注册表）**：
受治理的 Agent Definition 集合，是路由可发现能力的目录。
_避免_：Agent 执行器、候选集、页面菜单

**Candidate Set（候选集）**：
对当前 Principal 可用、且允许进入本次路由判断的 Agent 子集。
_避免_：完整 Registry、模型自行猜测的 Agent 列表

**Route Decision（路由决策）**：
OIR 对当前请求下一步处理方式的结构化判断；它描述去向，不代表执行已经完成。
_避免_：Agent Result、Plan 执行结果

**Assistant Message（助手消息）**：
面向用户展示的自然语言回应，不承担路由、运行或计划状态的事实源职责。
_避免_：Route Decision、Agent Result、Canonical Turn

**Invocation（调用）**：
要求一个确定 Agent 执行一次能力的动作。
_避免_：Route Decision、Plan、Agent Run

**UI Handoff（界面交接）**：
OIR 请求 Host App 打开页面或继续完成交互的协作结果，而不是后端业务执行。
_避免_：后端 Invocation、直接页面跳转实现

**Workflow（工作流）**：
封装自身内部步骤的可路由能力；对 OIR 而言，它可以作为一个 Agent 被调用。
_避免_：OIR Plan、任意多轮对话

**Plan（计划）**：
OIR 对多步骤任务的结构化主契约，包含步骤、依赖、状态和推进责任。
_避免_：Workflow、Route Decision、`show_plan` 动作

**Plan Step（计划步骤）**：
Plan 中具有稳定身份、依赖关系和执行状态的一个工作单元。
_避免_：Agent Run、聊天消息

**Execution Policy（执行策略）**：
规定 Plan 是否执行、何时执行以及由 OIR 还是 Host App 推进的协作约定。
_避免_：Route Action、Plan Status、权限策略

**Next Action（下一步动作）**：
OIR 明确要求用户或 Host App 在流程继续前完成的协作事项。
_避免_：Route Decision、Plan Status、内部任务队列

### 运行事实

**Session（会话）**：
用于关联一段连续交互的范围，不是单次语义请求或业务状态的事实源。
_避免_：Canonical Turn、Plan、Agent Run

**Message（消息）**：
会话中供展示或上下文使用的一条参与者内容。
_避免_：Canonical Turn、Agent Result、业务事件

**Canonical Turn（规范轮次）**：
一个受信语义请求从用户输入到最终语义响应的完整事实，归属于确定的 Principal、Tenant 和 Session。
_避免_：Message、Session、前端对话轮次

**Turn Capsule（轮次胶囊）**：
从已完成 Canonical Turn 得到的有界语义摘要，用于下游治理判断，而不是新的事实源。
_避免_：完整聊天记录、Canonical Turn 副本

**Agent Run（Agent 运行）**：
一个 Agent 接受某次执行要求后的单次运行事实。
_避免_：Invocation、Canonical Turn、Plan Step

**Agent Result（Agent 结果）**：
Agent Run 产生的结构化结果与终态说明。
_避免_：Assistant Message、Artifact、Route Decision

**Agent Event（Agent 事件）**：
关于 Agent Run 进度、完成、失败、澄清或取消的受信状态事实。
_避免_：用户消息、重复执行命令、Route Decision

**Execution Trace（执行轨迹）**：
将同一 Canonical Turn 涉及的 Context、Route Decision、Agent Run、UI Handoff、Agent Result 和 Memory 等运行事实按关联关系组织成的有界观察投影；它不取代这些事实各自的权威状态。
_避免_：Context Trace、Route Log、完整日志、流程图、运行事实源

**Runtime Observation（运行观察）**：
供人理解 Execution Trace 当前进展、判断依据和结果变化的只读解释，不改变被观察对象的执行状态。
_避免_：执行控制、原始日志调试、自由生成的过程描述

**Trace Completeness（轨迹完整性）**：
Execution Trace 对已知运行事实的覆盖程度；它独立于被观察业务本身的成功或失败状态。
_避免_：Agent Run 状态、业务结果、默认完整

**Recovered Snapshot（恢复快照）**：
轨迹事件存在缺口时从 Canonical Data 恢复的当前状态投影，不代表原始事件顺序或缺失期间的实时过程。
_避免_：Canonical Data、原始 Agent Event、推测的执行时间线

**Delegated Run（委托运行）**：
由 Host App 或外部 Agent 执行、但仍由 OIR 跟踪所有权和生命周期的 Agent Run。
_避免_：普通 UI Handoff、失去治理的外部调用

**Execution Ticket（执行票据）**：
授权外部参与方更新一个特定 Delegated Run 的短期、不透明、最小权限凭证。
_避免_：用户身份 Token、通用 API Key、Run ID

**Artifact（产物）**：
Agent Run 或 Plan Step 产生、可被后续步骤稳定引用的输出对象。
_避免_：Agent Result、Assistant Message、临时日志

### Context、Memory 与 Knowledge

**Governed Context（受治理上下文）**：
按身份、用途、时效、敏感性和预算约束后，可供某个决策者或执行者使用的信息集合。
_避免_：完整聊天历史、任意 Prompt 拼接、User Context

**Context Candidate（上下文候选）**：
进入治理流程、但尚未确定可被消费的一项潜在上下文信息。
_避免_：Context Pack、模型输入

**Context Pack（上下文包）**：
面向特定用途和消费者，经治理选出的有界 Context Candidate 集合。
_避免_：完整历史、Context Projection、Context Trace

**Context Projection（上下文投影）**：
从 Context Pack 生成、某个消费者实际获准看到的表示。
_避免_：Debug 数据、Context Trace、原始候选集合

**Context Trace（上下文轨迹）**：
解释上下文如何被纳入、排除、裁剪和投影的有界审计记录。
_避免_：模型输入、Context Projection、Route Log

**Evidence（路由证据）**：
用于支持或约束 Route Decision 的有来源信息或稳定信号。
_避免_：Agent Knowledge、Memory Item、固定答案

**Evidence Provider（证据提供者）**：
在路由判断前提供 Evidence、意图提示或受控固定命中的能力边界。
_避免_：Agent Registry、Knowledge Retriever、Agent Invoker

**Memory（记忆）**：
从受信交互事实中形成、可在未来请求中复用的用户或任务相关长期信息。
_避免_：聊天记录、Knowledge、Context Pack

**Recall（记忆召回）**：
为当前用途选择相关 Memory 的过程，不创建或修改 Memory。
_避免_：Formation、Knowledge Search

**Formation（记忆形成）**：
从受信且已完成的事实中提出并治理 Memory 新增、更新、删除或不操作决策的过程。
_避免_：Recall、聊天日志落库、模型摘要

**Memory Item（记忆项）**：
一个具有稳定身份、范围和生命周期的当前 Memory 事实。
_避免_：Memory Revision、向量记录、Turn Capsule

**Memory Revision（记忆修订）**：
Memory Item 在一次受治理变化后的不可混淆版本。
_避免_：新的 Memory Item、Provider 历史记录

**Knowledge（知识）**：
由组织管理、可被多个请求检索引用的参考内容，不是从某个用户对话形成的长期记忆。
_避免_：Memory、Evidence、Prompt

**Knowledge Asset（知识资产）**：
具有稳定身份、访问边界和生命周期的一组受治理知识内容。
_避免_：文件路径、向量集合、单条 Evidence

**Knowledge Chunk（知识片段）**：
Knowledge Asset 中可独立检索和引用的有界内容单元。
_避免_：Knowledge Asset、向量命中、Memory Item

### 迁移与运行治理

**Decision Shadow（决策影子）**：
用同一输入比较路由判断而不推进主业务状态或产生可见副作用的演练方式。
_避免_：State Rehearsal、流量镜像写入、Fallback

**State Rehearsal（状态演练）**：
在隔离数据域中推进完整状态生命周期，用于验证状态不变量而不触碰主数据域。
_避免_：Decision Shadow、生产灰度、主数据回放

**Fallback（回退调用）**：
在明确允许且确认主路径未接受请求时，改由备用能力处理同一意图。
_避免_：双写、提交状态未知时重放、紧急配置回滚

**Cutover（切流）**：
把某类真实请求的权威处理责任从旧系统转交给目标系统的受控过程。
_避免_：代码部署、Decision Shadow、State Rehearsal

**Write Fence（写入围栏）**：
在迁移或故障期间禁止不安全写入、双写或重放的治理边界。
_避免_：普通访问控制、只读 Fallback、Circuit Breaker
