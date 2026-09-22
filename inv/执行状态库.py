"""执行与风控状态的原子存储；损坏时拒绝启动，不静默清空幂等记录。"""
import json
import os
from pathlib import Path


class ExecutionStateStore:
    def __init__(self,path,*,exclusive=False):
        self.path=Path(path)
        self._lock=None
        if exclusive:
            self.path.parent.mkdir(parents=True,exist_ok=True)
            self._lock=self.path.with_suffix('.lock').open('a+b')
            if self._lock.tell()==0:
                self._lock.write(b'0')
                self._lock.flush()
            self._lock.seek(0)
            try:
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(self._lock.fileno(),msvcrt.LK_NBLCK,1)
                else:
                    import fcntl
                    fcntl.flock(self._lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                self._lock.close()
                self._lock=None
                raise RuntimeError('同一执行状态已有运行进程，拒绝重复启动') from None
        self.values=json.loads(self.path.read_text(encoding='utf-8')) if self.path.exists() else {}
        if not isinstance(self.values,dict): raise ValueError('执行状态文件格式无效')

    def get(self,key):
        return self.values.get(key)

    def set(self,key,value):
        self.values[key]=value

    def flush(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary=self.path.with_suffix('.writing')
        with temporary.open('w',encoding='utf-8') as stream:
            json.dump(self.values,stream,ensure_ascii=False,allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary,self.path)

    def close(self):
        if self._lock is not None:
            self._lock.close()
            self._lock=None
