# 数据服务开放与订阅管理平台

面向数据API注册、查询路由、订阅审批、用量计量与SLA保障的企业数据服务开放平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| API注册 | `api_registry` | 数据API | API编号、API名称、接口路径 |
| 查询路由 | `query_route` | 路由规则 | 规则编号、关联API、路由条件 |
| 订阅管理 | `subscription` | 订阅申请 | 申请编号、订阅API、申请方 |
| 频率限制 | `rate_limiter` | 限流策略 | 策略编号、目标API、调用方 |
| 用量计量 | `usage_billing` | 用量记录 | 记录编号、调用方、API名称 |
| 缓存分层 | `cache_layer` | 缓存策略 | 策略编号、关联API、缓存层级 |
| 数据过滤 | `data_filter` | 过滤规则 | 规则编号、关联API、过滤字段 |
| 字段脱敏 | `field_mask` | 脱敏规则 | 规则编号、目标字段、脱敏算法 |
| SLA监控 | `sla_monitor` | SLA指标 | 指标编号、关联API、可用率目标 |
| 结果格式化 | `result_format` | 格式模板 | 模板编号、关联API、输出格式 |
| 查询优化 | `query_optimize` | 优化规则 | 规则编号、关联API、索引建议 |
| 异常归类 | `error_classify` | 异常类型 | 类型编号、类型名称、错误码段 |
| 数据归并 | `data_merge` | 归并规则 | 规则编号、主API、从API |
| 导出任务 | `export_task` | 导出任务 | 任务编号、关联API、查询参数 |
| 调用审计 | `call_audit` | 调用记录 | 记录编号、调用方、目标API |
| 版本兼容 | `version_compat` | 版本映射 | 映射编号、关联API、旧版本 |
| 批量查询 | `batch_query` | 批量任务 | 任务编号、关联API、查询清单 |
| SDK配置 | `sdk_config` | SDK配置 | 配置编号、关联API、SDK语言 |
| 账期更正 | `billing_correction` | 账期更正明细 | 更正编号、原账单编号、账期 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 账期更正与重新结算

原「完成结算」只覆盖首次汇总；误结后的重新核算走 `billing_correction` 模块：

- **版本化凭证**：`data/usage/settlements/<账期>.json` 按版本追加结算凭证，
  `GET /api/usage_billing/settlements/{账期}/versions/{版本}` 可按原版本取数，
  原已出账明细的 `billed_amount` 不会被再次收取。
- **更正用量**：`POST /api/billing_correction/apply` 逐条登记来源调用，
  每条用量同时关联「原账单编号 + 来源调用编号」；同一来源调用只入账一次，
  `idempotency_key` 重放直接返回原更正单。
- **状态流转**：`草稿 → 已申请 → 确认中 → 已确认 → 已通知`，可 `中断更正`
  后用 `恢复更正` 从断点继续；已完成的步骤记录在更正单里，不会重复结算。
- **金额凭据**：确认时只对本次修订金额追加结算版本，凭证行区分 `usage` /
  `correction`，先前确认的用量与本次修订都可逐行核对。
- **对账通知**：`POST /api/billing_correction/{id}/notify` 向对账方再发结果，
  记录 `reopen_required` 与 `重开追踪`，并用 `GET
  /api/usage_billing/settlements/{账期}/reconciliation` 复核两次结算的实际应收差额。

回归测试：`cd backend && .venv/bin/python -m pytest -q tests/`

领域：数据工程
子方向：数据分析与数据应用
