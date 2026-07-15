# runtime-260518-1025: Runtime 状态一致性与多节点收敛规则 - 项目日志

## 2026-05-18 10:25 CST

创建 runtime 状态一致性 S3 spec 草稿，目标是把“runtime 状态是否实时同步到所有节点”的讨论降级为可验证的最终一致模型。

已确认不进入代码修复、不继续镜像构建或部署。

## 2026-05-18 专家审查

本轮专家团分工：

| 角色 | 结论 |
| ---- | ---- |
| Runtime 后端 | 结论成立；Python Admin 是 probe 写回 + 10 秒 health loop + SSE/fast polling，不是实时订阅。Docker `ensure_runtime()`、pending guard、残留 runtime 恢复、reload generation/hash 是主要缺口。 |
| Go Gateway | 共享 MySQL 下可做最终一致；无 DB 多 Gateway 不成立。heartbeat 覆盖 `in_flight/drain_mode`、proxy session secret、runtime sync last-writer-wins、DB/store degraded 可观测性是主要风险。 |
| Admin UI | SSE/fallback/freshness 字段已有雏形，但 per-assistant freshness 展示、fallback 覆盖、操作后最终态证明、浏览器证据不足。 |
| 反方复审 | spec 方向正确，但必须把临时 `output/` 复审证据固化到 specs，补 `DECISIONS.md` / `PROJECT_LOG.md` / `REVIEW_ROUNDS.md`，并处理 `TEST_REPORT` 不是验收证明的矛盾。 |

## 2026-05-18 写回动作

写回范围：

- `PRODUCT.md`
- `TECH.md`
- `TEST_REPORT.md`
- `DECISIONS.md`
- `PROJECT_LOG.md`
- `REVIEW_ROUNDS.md`

本次写回只固化 spec 和复审结论，不修改业务代码，不部署，不更新镜像。
