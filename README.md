# BankAgent

银行对公企业研究与产品推荐原型。把公开资料、可追溯知识、独立审核与月度产品推荐连接起来，并为 Codex 多角色协作提供可选的固定联系路线检查。

**当前版本：本地研究原型 + 桌面原生 Hook 集成。不是生产级完全隔离系统。**

## 已经完成

- finance、products、enterprises、engineering 四个知识域；企业资料按 company_id 管理。
- 候选、独立审核、追加发布分离；版本、来源摘要和企业发布账本留痕。
- SQLite/FTS5 词法检索、中文分词、文档解析适配及市场资料校验。
- 企业研究、总控和市场研究的业务 Skill，包含产品推荐与审核规则。
- 可选 Codex PreToolUse Hook：检查自动聊天发送的固定联系人路线，保留用户在原聊天直接发消息。
- 两份研究报告，按企业需求、可争取业务规模、双方价值和银行收益风险给出前三推荐。

## 实际验证过什么

本次集成只使用两个正常功能场景：用户确认一个原聊天可以直接发送并收到回复；市场角色完成央行公开资料读取、文件保存和向总控回报，实际发送触发 Hook。没有把这些结果扩大为所有聊天或全部权限路径的验证。

核心代码在前期发布准备时已有 170 项测试通过；这是历史验证记录。详见 [验证记录](docs/validation.md)。

## 需要改善

1. **强制隔离**：Hook 是固定联系路线检查，不是不可绕过的操作系统权限边界。文件、网络、进程与凭据隔离仍需完善。
2. **完整工作流授权**：当前 Hook 不按任务状态判断送审、退修或发布权限，相关业务门禁仍由核心程序负责。
3. **部署适配**：旧活动聊天的配置重载、跨版本兼容和专项市场争议路线需要进一步完善。
4. **研究复现**：公开仓库不含真实 RAG 和完整报告计算输入，不能从空库一键重建随附报告。
5. **工程化**：远程身份认证、生产部署、监控及模型后端独立审核仍需建设，现有模拟检查不代表独立模型审核。

## 快速开始

Python 3.10+，核心仅依赖标准库及 SQLite FTS5：

```sh
python3 -m bankagent --db knowledge/local.sqlite init
python3 -m bankagent --db knowledge/local.sqlite status
python3 -m bankagent --db knowledge/demo.sqlite demo
python3 -m bankagent --db knowledge/demo.sqlite kb query finance 现金流
```

`demo` 是虚构数据，数据库路径必须尚不存在。演示不联网、不需要模型密钥。真实资料导入须自行准备来源及审核文件。[复现说明](docs/reproducibility.md)

## 代码、Skill 与 RAG

| 目录 | 内容 |
|---|---|
| `bankagent/` | 知识、检索、审核发布及市场研究机制 |
| `skills/` | 可读、可改的项目角色 Skill，部署前替换本地绑定 |
| `integrations/codex-hook/` | 官方 Hook 的最小路线适配及配置模板 |
| `docs/` | 架构、已完成与待改善、RAG 方法和复现边界 |
| `tests/` | 保留的历史核心测试，发布时不自动全量执行 |
| `reports/` | 署名 MIHUKUAKU 的两份报告 |

RAG 只公开方法与代码，不公开实际数据库、企业知识包、原始资料缓存、聊天记录或账号标识。[RAG 方法](docs/rag-method.md) · [Skill 使用说明](skills/README.md) · [Hook 安装说明](integrations/codex-hook/README.md)

## 报告

- [2026 年 10 月报告](reports/AI_BANK_2026-10.pdf)
- [2026 年 11 月报告](reports/AI_BANK_2026-11.pdf)

报告包含估计与情景测算，不是已实现收益或自动授信结论；原始来源及假设见报告。报告复现范围见文档。

## 许可证

代码、Skill 和本项目原创说明采用 [MIT](LICENSE)。报告作为研究展示提供；其中引用的第三方资料保留各自权利，MIT 不授予第三方内容的权利。真实知识库不随仓库发布。
