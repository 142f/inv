"""运行完整离线回归并保存结构化证据。"""
import io
import json
from pathlib import Path
import unittest


def main():
    stream=io.StringIO()
    suite=unittest.defaultTestLoader.discover('tests')
    result=unittest.TextTestRunner(stream=stream,verbosity=2).run(suite)
    target=Path('reports/改造验证')
    target.mkdir(parents=True,exist_ok=True)
    payload=dict(tests_run=result.testsRun,failures=len(result.failures),errors=len(result.errors),
                 skipped=len(result.skipped),successful=result.wasSuccessful(),details=stream.getvalue())
    (target/'测试结果.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(stream.getvalue())
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__': raise SystemExit(main())
