"""固定输入下验证尾部风险计算的数值一致性和性能。"""
import json
import random
import statistics
import time
import tracemalloc
from pathlib import Path
from inv.backtest.metrics import _cvar


def reference(values, q):
    return abs(statistics.fmean(sorted(values)[:max(1, int(len(values)*q))]))


def measure(fn, values):
    fn(values, .05)
    samples = []
    for _ in range(5):
        start = time.perf_counter()
        result = fn(values, .05)
        samples.append(time.perf_counter()-start)
    tracemalloc.start()
    fn(values, .05)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return dict(seconds=statistics.median(samples), peak_bytes=peak, value=result)


def main():
    rng = random.Random(42)
    rows = []
    for n in (1000, 10000, 100000, 1000000):
        values = [rng.gauss(0, .01) for _ in range(n)]
        before, after = measure(reference, values), measure(_cvar, values)
        assert abs(before['value']-after['value']) < 1e-12
        rows.append(dict(size=n, before=before, after=after,
                         speedup=before['seconds']/after['seconds']))
    target = Path('reports/改造验证')
    target.mkdir(parents=True, exist_ok=True)
    (target/'性能基准.json').write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(rows, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
