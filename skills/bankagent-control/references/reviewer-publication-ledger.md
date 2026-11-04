# 本企审核员发布与月度记账

固定审核员独立审核彻底通过后，经受控接口追加本企业RAG并写完成回执。报告审核和知识发布分开；不代写候选后自审、不覆盖已发布版本、不扩展其他域或企业权限。跨审门禁保持。审核失败直退执行员，流程见[审核规则](review-return.md)。

## 实际入口
本地可信操作员CLI（不是独立身份认证服务）：
`python3 -m bankagent enterprise-import --bundle <完整企业候选包.json> --review <确切包PASS审核.json> --publisher-reviewer <程序注册审核员ID>`
既有publish_bundle格式要求保持：审核绑定bundle_sha256、company_id、researcher、reviewer、approved_fact_ids与evidence；报告PASS或表格PASS不能直接充当不同格式知识包PASS。如需组装知识包，交作者处理，再审核该确切包。版本冲突重基后必须重审，不改历史身份来绕过绑定。
程序强制本企固定身份及company_id、非作者自审、独立PASS、原跨审门禁、版本冲突及索引事务。发布与enterprise_publication_ledger数据库账本同事务成功/回滚，账本不可UPDATE/DELETE。CLI中的身份来自可信本地操作员，不宣称新增独立身份鉴权。
读取实际发布账：`python3 -m bankagent enterprise-publication-ledger`。

## 按月按企业完成记账
统一目录 `docs/enterprise_monthly_ledger/`，2026-07、08、09、10、11每月按midea、sf_holding、alibaba隔离，共15个格子。
仅审核彻底完成后，由固定审核员在 `<month>/<company_id>/receipt.<唯一ID>.json` 追加完成回执，旧回执不可覆盖；审核未完成继续在原case留工作记录，不给总控发送失败汇报。完成后必须向总控简报“已完成”及审核、产物和回执路径，总控自行读取核对并登记总账。
回执必填：task_id、month、company_id、author_id、reviewer_id、completed_at、review_file及sha256、全部交付文件hash、审核结论、历史修复轮次及实际状态、历史截止时间、report_status、rag_status（published/already_published/no_change/report_only）、knowledge_version、published_version、proposal_id或数据库账本定位、发布结果及检索验证文件、剩余非阻断缺口。未执行发布不可写published；仅报告通过可记report_only，不冒充知识已入库。存在必需缺项时如实记录并继续估计、替代方法或修订，不写“全部完成”。
历史7/8月可根据实际审核/发布记录补账，明确补账日期及原发生时间，不倒填；9月继续当前工作，10/11月未完成留待办。金融市场、股价图、年报文件与企业月报分别列关联证据，缺图不伪造。
`python3 scripts/refresh_enterprise_monthly_ledger.py` 只汇总审核证据和完成回执，不授予PASS、不发布RAG。总控维护总览，审核员只追加本月本企回执避免并发覆盖。
