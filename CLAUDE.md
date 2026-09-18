# 项目上下文规则（Trae IDE 规则）

## 📌 以下规则用于过滤敏感内容与无意义文件，避免 AI 读取无关信息、误操作。

---

## 一、文件读取过滤规则

### 1. 以点号开头的隐藏文件/目录

以下路径和文件一律不读取、不分析、不修改：
- `.git/` 及其所有子目录和文件
- `.secret.key`
- `.env、`.env.*`、`.env.local`
- `.copilotignore`、`.gitignore`
- 所有以 `.` 开头的其他文件或目录

### 2. Python 运行时缓存

以下目录和文件一律不读取、不分析、不修改：
- `__pycache__/` 及其所有文件
- 所有 `*.pyc`、`*.pyo`、`*.pyd` 文件
- `.venv/`、`venv/`、`env/` 等虚拟环境目录
- `.pytest_cache/`、`.mypy_cache/`

### 3. 日志与运行数据

以下目录和文件一律不读取、不分析、不修改：
- `logs/` 目录及其所有 `*.log` 文件
- `data/` 目录及其所有子目录和文件
- 所有 `*.log`、`*.sqlite3`、`*.db`、`*.csv` 数据文件
- `cache/`、`tmp/` 临时目录

### 4. 敏感配置文件

以下文件一律不读取、不分析、不修改：
- `config/strategies.yaml`（策略配置，内含交易参数与密钥信息
- `secrets.yaml`、`credentials.json`
- `config/.env`

---

## 二、代码修改规则

1. 修改代码时，注释使用中文
2. 日志输出使用中文
3. 所有文档和说明文字使用中文
4. 不修改、不输出 `.secret.key`、`config/strategies.yaml 中的任何内容

---

## 三、AI 行为约束

当请求涉及以下内容时，明确拒绝或提示用户：
1. 直接读取或展示日志内容（交易数据、持仓信息、账号凭证等

---

## 四、允许读取的内容

仅在以下情况下允许读取：
- Python 源代码（`*.py`）
- 配置示例（`config/*.yaml`、`*.yml`，不含 strategies.yaml）
- 项目文档（`README 除外）
- 项目依赖（`*.md`、`.yml`、`*.toml` 等）
- 项目配置文件（`*.json`、`*.toml` 等）