"""Local operator CLI. Controller commands are trusted, not an agent API."""
import argparse
import json
from pathlib import Path

from .core import Store, Conflict, DOMAINS


def demo(path):
    if path.exists():
        raise ValueError('demo requires a new database; existing data will not be overwritten')
    store = Store(path)
    controller = store.session('controller')
    bootstrap = store.session('bootstrap')
    reviewer = store.session('reviewer')
    task = controller.assign('bootstrap', '*')
    payload = {
        'title': 'FIXTURE：治理测试材料',
        'content': 'FIXTURE：企业现金流与产品匹配测试，不是真实金融事实。',
        'verification': 'fixture',
        'sources': [{'url': 'https://example.com/fixture', 'title': '未抓取的测试来源',
                     'publisher': 'fixture', 'accessed_at': '2026-10-27',
                     'locator': 'synthetic fixture'}],
    }
    for domain in DOMAINS:
        proposal = bootstrap.propose(task, domain, 'fixture_a', payload)
        reviewer.review(proposal, True, '模拟审核：仅确认测试结构，不核实业务内容')
        controller.publish(proposal)
    controller.retire_bootstrap()
    worker = store.session('enterprise', company_id='fixture_a')
    task = controller.assign('enterprise', 'fixture_a')
    newer = dict(payload, content='FIXTURE：第二项目的测试修订，不是真实企业变化。')
    first = worker.propose(task, 'enterprises', 'fixture_a', newer, 1)
    stale = worker.propose(task, 'enterprises', 'fixture_a', newer, 1)
    rejected = worker.propose(task, 'enterprises', 'fixture_a', payload, 1)
    reviewer.review(rejected, False, '演示拒绝：测试来源不足以支撑业务建议')
    reviewer.review(first, True, '模拟审核：版本测试')
    reviewer.review(stale, True, '模拟审核：冲突测试')
    controller.publish(first)
    conflict = False
    try:
        controller.publish(stale)
    except Conflict:
        conflict = True
    controller.close_task(task)
    return {'mode': 'fixture', 'database': str(path), 'domains': list(DOMAINS),
            'enterprise_versions': [r['version'] for r in worker.history('enterprises', 'fixture_a')],
            'stale_write_blocked': conflict, 'rejected_proposal': rejected,
            'limitations': ['无真实来源采集', '无模型调用', '审核为确定性测试操作']}


def main():
    parser = argparse.ArgumentParser(description='BankAgent 本地可信操作员入口')
    parser.add_argument('--db', type=Path, default=Path('knowledge/bankagent.sqlite'))
    commands = parser.add_subparsers(dest='command', required=True)
    commands.add_parser('init', help='初始化数据库，不导入业务知识')
    commands.add_parser('status', help='查看知识数量和任务账本')
    coverage = commands.add_parser('cross-coverage', help='只读检查外部企业数量，不开展业务审核')
    coverage.add_argument('company_id')
    config = commands.add_parser('cross-configure', help='可信操作员注册固定身份并启用发布门禁')
    config.add_argument('--registry', type=Path, required=True)
    config.add_argument('--audit', type=Path, required=True)
    cross = commands.add_parser('cross-run', help='一次性进程交叉校验；仅结构化操作员输入，不自动调用模型')
    cross.add_argument('--request', type=Path, required=True)
    cross.add_argument('--audit', type=Path, required=True)
    commands.add_parser('demo', help='在新数据库中运行明确标识的 fixture')
    finance = commands.add_parser('finance-import', help='导入独立审核通过的 finance 知识包')
    finance.add_argument('--bundle', type=Path, default=Path('data/staging/finance_v1.json'))
    finance.add_argument('--review', type=Path, default=Path('docs/finance_v1_review.json'))
    products = commands.add_parser('products-import', help='导入独立审核通过的中国银行产品知识包')
    products.add_argument('--bundle', type=Path, default=Path('data/staging/boc_products_v1.json'))
    products.add_argument('--review', type=Path, default=Path('docs/boc_products_v1_review.json'))
    enterprise = commands.add_parser('enterprise-import', help='导入独立审核通过的企业知识包')
    enterprise.add_argument('--bundle', type=Path, required=True)
    enterprise.add_argument('--review', type=Path, required=True)
    enterprise.add_argument('--publisher-reviewer', help='固定本企审核员身份；通过后直接发布并事务记账')
    commands.add_parser('enterprise-publication-ledger', help='读取企业审核员发布账本')
    commands.add_parser('parser-status', help='检查 Docling/pdfplumber 版本与本地模型校验值')
    setup = commands.add_parser('parser-setup', help='安装固定版本模型；默认只读取已有本地缓存')
    setup.add_argument('--cache', type=Path, help='Hugging Face hub 缓存目录')
    setup.add_argument('--download', action='store_true', help='允许下载官方固定快照模型')
    document = commands.add_parser('document-parse', help='Docling 主解析公开 PDF，输出企业分区未审核证据')
    document.add_argument('--pdf', type=Path, required=True)
    document.add_argument('--company-id', required=True)
    document.add_argument('--source-url', required=True)
    document.add_argument('--title', required=True)
    document.add_argument('--publisher', required=True)
    document.add_argument('--period', required=True, help='文档报告期，不自动代表每项事实的期间')
    document.add_argument('--pages', help='物理页码（从1开始），如 9-11,13,132-133；默认全文')
    document.add_argument('--no-auxiliary', action='store_true', help='关闭 pdfplumber 辅助输出')
    kb = commands.add_parser('kb').add_subparsers(dest='action', required=True)
    query = kb.add_parser('query')
    query.add_argument('domain', choices=DOMAINS)
    query.add_argument('query')
    query.add_argument('--limit', type=int, default=5)
    query.add_argument('--knowledge-version')
    query.add_argument('--topic')
    query.add_argument('--company-id')
    query.add_argument('--version', type=int)
    context = kb.add_parser('context', help='生成带引用的 RAG 上下文，不调用 LLM')
    context.add_argument('domain', choices=DOMAINS)
    context.add_argument('query')
    context.add_argument('--limit', type=int, default=5)
    context.add_argument('--knowledge-version')
    context.add_argument('--topic')
    context.add_argument('--company-id')
    context.add_argument('--version', type=int)
    history = kb.add_parser('history')
    history.add_argument('domain', choices=DOMAINS)
    history.add_argument('object_id')
    args = parser.parse_args()
    try:
        if args.command == 'cross-coverage':
            from .cross_review import CrossReview
            # No Store initialization/schema migration on the real read-only check.
            from types import SimpleNamespace
            service = object.__new__(CrossReview)
            service.store = SimpleNamespace(path=str(args.db))
            result = service.coverage(args.company_id)
        elif args.command == 'demo':
            result = demo(args.db)
        elif args.command in ('parser-status', 'parser-setup', 'document-parse'):
            from .documents import parser_status, setup_models, parse_document
            if args.command == 'parser-status':
                result = parser_status()
            elif args.command == 'parser-setup':
                result = setup_models(cache=args.cache, download=args.download)
            else:
                result = parse_document(args.pdf, args.company_id, args.source_url, args.title,
                                        args.publisher, args.period, pages=args.pages,
                                        auxiliary=not args.no_auxiliary)
        else:
            store = Store(args.db)
            controller = store.session('controller')
            if args.command in ('cross-configure', 'cross-run'):
                from .cross_review import CrossReview
                service = CrossReview(store, args.audit)
                if args.command == 'cross-configure':
                    spec = json.loads(args.registry.read_text())
                    for entry in spec['companies']:
                        service.register_company(entry['company_id'], entry['author_id'], entry['reviewer_id'])
                    for company in spec.get('enable', []):
                        service.enable_policy(company)
                    result = {'status': 'configured', 'mode': 'trusted_operator', 'enabled': spec.get('enable', [])}
                else:
                    spec = json.loads(args.request.read_text())
                    for origin in spec.get('origins', []):
                        service.register_origin(**origin)
                    run = service.start(spec['proposal_id'], spec['report_sha256'], spec['claims'])
                    service.select(run, spec.get('candidates'))
                    try:
                        for submission in spec.get('workers', []):
                            with service.open_worker(run, submission['company_id'], submission.get('version')) as worker:
                                for request in submission.get('operations', []):
                                    worker.call(**request)
                    except BaseException:
                        service.abort(run)
                        raise
                    else:
                        result = service.finish(run)
            elif args.command == 'init':
                result = {'database': str(args.db), 'domains': list(DOMAINS), 'status': 'initialized'}
            elif args.command == 'finance-import':
                from .finance import publish_bundle
                result = publish_bundle(store, args.bundle, args.review)
            elif args.command == 'products-import':
                from .finance import publish_bundle
                result = publish_bundle(store, args.bundle, args.review, domain='products')
            elif args.command == 'enterprise-import':
                from .enterprises import publish_bundle
                result = publish_bundle(store, args.bundle, args.review, publisher_identity=args.publisher_reviewer)
            elif args.command == 'enterprise-publication-ledger':
                with store.db() as db:
                    result = [dict(r) for r in db.execute('SELECT * FROM enterprise_publication_ledger ORDER BY created, company_id, version')]
            elif args.command == 'status':
                with store.db() as db:
                    result = {'knowledge_counts': {d: db.execute(f'SELECT COUNT(*) FROM rag_{d}').fetchone()[0] for d in DOMAINS},
                              'tasks': [dict(r) for r in db.execute('SELECT * FROM tasks')],
                              'enterprise_profiles': [dict(r) for r in db.execute(
                                  "SELECT object_id AS company_id,version,"
                                  "json_extract(content,'$.knowledge_version') AS knowledge_version,"
                                  "COALESCE(json_array_length(content,'$.facts'),0) AS facts FROM rag_enterprises")]}
            elif args.action in ('query', 'context'):
                filters = dict(limit=args.limit, knowledge_version=args.knowledge_version,
                               topic=args.topic, version=args.version, company_id=args.company_id)
                if args.action == 'context':
                    from .retrieval import context
                    result = context(controller, args.domain, args.query, **filters)
                else:
                    result = controller.search(args.domain, args.query, **filters)
            else:
                result = controller.history(args.domain, args.object_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, PermissionError, OSError, RuntimeError) as error:
        parser.exit(2, str(error) + '\n')


if __name__ == '__main__':
    main()
