# 架构与边界

核心数据流为：任务 → 候选 → 独立审核记录 → 追加发布版本 → 检索。`core.py` 管理角色能力、事务与不可变记录；`finance.py` 和 `enterprises.py` 校验导入证据；`retrieval.py` 提供词法检索；`cross_review.py`、`cross_worker.py` 实现受限跨企业核验会话；`documents.py` 适配可选 PDF 解析器；`market.py` 等模块校验市场资料及来源。

身份和能力由可信宿主创建，不应向不可信模型开放创建会话、改角色或直接访问数据库的权限。现有能力校验无法隔离具有同等本地文件权限的进程。

跨审测试验证工作流与工具边界，不证明外部模型服务端的上下文删除。自动联系路线通过可选 Codex PreToolUse Hook 检查，详见 integrations/codex-hook/README.md；这不是操作系统隔离。报告制作是独立研究过程，当前 CLI 不会从空数据库自动生成随附 PDF。
