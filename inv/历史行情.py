"""仅从明确授权的行情目录读取行情目录索引与 Parquet，不读取交易运行记录。"""
import hashlib
import sqlite3
import sys
from pathlib import Path


def load_history(root):
    import pandas as pd
    from inv.domain.models import Bar
    root=Path(root).resolve()
    local_dependencies=Path(__file__).resolve().parents[1]/'依赖'
    if local_dependencies.is_dir():
        sys.path.insert(0,str(local_dependencies))
    databases=list((root/'metadata').glob('*.sqlite3'))
    if len(databases)!=1:
        raise ValueError('历史行情目录索引不唯一或不存在')
    # mode=ro，禁止更新外部数据或其索引。
    connection=sqlite3.connect(databases[0].as_uri()+'?mode=ro',uri=True)
    query='''SELECT h.symbol,h.timeframe,o.path,o.stored_sha256,v.version
        FROM dataset_heads h JOIN dataset_versions v ON v.version=h.version
        JOIN data_aliases a ON a.logical_path=v.artifact_path
        JOIN data_objects o ON o.object_id=a.object_id
        WHERE (h.symbol IN ('BTC','ETH') AND h.channel='curated' AND h.timeframe='D1')
        OR (h.symbol IN ('BTCUSDT_BINANCE','ETHUSDT_BINANCE') AND h.timeframe='H4')
        OR (h.symbol='XAUUSD_DUKAS' AND h.timeframe IN ('D1','H4'))'''
    entries=connection.execute(query).fetchall()
    connection.close()
    result={}
    for source,tf,path,expected_hash,version in entries:
        asset='BTC' if source.startswith('BTC') else ('ETH' if source.startswith('ETH') else 'XAU')
        full=(root/path).resolve()
        if not full.is_relative_to(root/'processed') or any(part.startswith('.') for part in Path(path).parts):
            raise ValueError('行情索引路径越界')
        digest=hashlib.sha256(full.read_bytes()).hexdigest()
        if digest!=expected_hash:
            raise ValueError('历史行情内容哈希不匹配')
        import pyarrow.parquet as parquet
        frame=pd.DataFrame(parquet.read_table(full).to_pydict()).sort_values('timestamp')
        if frame.timestamp.duplicated().any():
            raise ValueError('历史行情时间戳重复')
        if not frame.is_complete.all():
            frame=frame[frame.is_complete]
        records=[]
        for row in frame.itertuples():
            records.append(Bar(row.timestamp.timestamp(),row.open,row.high,row.low,row.close,
                               spread=max(0,float(getattr(row,'spread',0) or 0)),volume=float(row.volume or 0)))
        result[(asset,tf)]=(records,dict(source=source,path=str(full),sha256=digest,version=version,
            rows=len(records),quality_status=sorted(frame.quality_status.unique().tolist()),
            quality_flags=sorted(frame.quality_flags.fillna('').unique().tolist()),
            source_basis=sorted(frame.price_basis.unique().tolist())))
    return result
