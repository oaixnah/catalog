# Catalog 维护手册

## 分支与数据流

```
catalog.sqlite（数据源，main 分支）
    │  python3 catalog.py export [--bump-sequence]
    ▼
reviewed-registry.json
    │  python3 catalog.py sign --key catalog.pem
    ▼
reviewed-registry.signed.json
logo/<providerId>.svg
logo/<providerId>_dark.svg
    │  GitHub Actions（.github/workflows/publish.yml）force push
    ▼
prod 分支（两个 JSON + logo/）→ CDN https://catalog.ravel.oaix.tech
```

- **main**：放 `catalog.sqlite`、`catalog.py`、`pyproject.toml`、`logo/`、文档。两个 JSON 由 `.gitignore` 忽略，**不要提交到 main**。
- **prod**：由 CI 自动维护的孤立分支，永远只包含 `reviewed-registry.json`、`reviewed-registry.signed.json` 与 `logo/`，**不要手工提交**。
- 旧流程（编辑 JSON → 导入 SQLite）已废弃，`import_to_sqlite.py` / `import.sql` 已删除。

## 环境要求

- Python 3.10+
- 核心 CRUD / export 仅需标准库；TUI 需要 Textual（`pip install -e .` 会自动安装）
- OpenSSL >= 3（仅签名需要；macOS 自带的 LibreSSL 不支持，请用 `brew install openssl@3`）

## 安装 CLI

在仓库根目录的虚拟环境中做一次 editable 安装：

```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -e .    # 或者 uv pip install -e .
catalog-cli --help
```

必须使用 `-e`（editable）：`catalog.sqlite`、`catalog.pem` 与导出产物都在仓库根目录，CLI 默认按 `catalog.py` 所在目录定位它们。`catalog-cli ...` 与 `python3 catalog.py ...` 完全等价；CI 仍直接调用 `python3 catalog.py`。

## CLI 速查

```bash
catalog-cli --help                 # 全部命令
catalog-cli validate               # 检查外键/枚举等一致性
catalog-cli init                   # 在全新文件上建表（灾难恢复用）

catalog-cli model list
catalog-cli model show <id>
catalog-cli model add --id gpt-5.6-nova --name "GPT-5.6 Nova" \
    --context 1050000 --input 922000 --output 128000 --efforts none,low,medium,high,xhigh,max
catalog-cli model set <id> --temperature --efforts low,high,max
catalog-cli model rm <id>

catalog-cli provider list
catalog-cli provider add --id groq --name Groq --authentication bearer-api-key \
    --base-url https://api.groq.com/openai --model-list-path /v1/models \
    --logo-source default
catalog-cli provider set <id> --base-url https://api.example.com
catalog-cli provider set <id> --logo-source ./assets/groq.svg
catalog-cli provider rm <id>

catalog-cli logo sync                  # 为所有 provider 生成缺失的 logo
catalog-cli logo sync groq --force     # 强制重新抓取指定 provider
catalog-cli link list
catalog-cli link bind <provider> <model_ref> [--api-model-id <api_model_id>] \
    --protocol openai-compatible --request-path /v1/chat/completions
catalog-cli link unbind <provider> <model_id>

catalog-cli registry show          # format_version / sequence / created_at

catalog-cli tui                    # 交互式 TUI（默认 Models）
catalog-cli tui --view links       # 直接打开 Links
```

- `model set --efforts` 会**整体替换**推理强度列表，顺序即导出顺序。
- `--input null` 表示不单独限制输入。
- `--logo-source` 支持 `default`（models.dev）、含 `{provider}` 的 http(s) URL 模板，或本地 SVG 路径；详见下文「Provider Logo」。
- 冒烟测试请复制数据库后加 `--db`：`cp catalog.sqlite /tmp/x.sqlite && catalog-cli --db /tmp/x.sqlite ...`（logo 会写到 `/tmp/logo/`）

## 交互式 TUI

`catalog-cli tui` 基于 Textual，直接读写 `catalog.sqlite`；不会自动 export / sign / bump sequence。改动后仍需提交数据库，由 CI 发布。

### 键位

| 键 | 列表 | 详情 | 关联 |
|---|---|---|---|
| `1/2/3` | Models / Providers / Links | - | - |
| `↑/↓` | 移动当前行 | - | 移动 |
| `→` / `←` | 详情 / 清除动作栏或勾选 | - / 返回 | - |
| `Space` | 勾选多行 | - | 标记绑定/解绑 |
| `Enter` | 打开动作栏或执行 | - | 应用变更 |
| `Tab` | 正向循环切换视图；动作栏打开时切换 Delete / Bind | - | - |
| `Shift+Tab` | 反向循环切换视图；动作栏打开时切换 Delete / Bind | - | - |
| `d` | 删除（Link 为解绑） | 删除/解绑 | 标记解绑 |
| `e` | 编辑当前行 | 编辑 | - |
| `m` | 关联管理 | 关联管理 | - |
| `n` | 新增 | - | - |
| `b` / `u` | - | - | 标记绑定 / 解绑 |
| `r` | 刷新 | 刷新 | 刷新 |
| `?` / `q` | 帮助 / 退出 | 帮助 / 退出 | 帮助 / 退出 |
| `Esc` | 取消动作/勾选 | 返回 | 返回 |

表单：`Tab` 下一项、`Space` 切换开关/推理强度、`Ctrl+S` 或 `F2` 保存、`Esc` 取消。

### 行为约定

- 删除 Model/Provider 会二次确认并提示级联删除的 efforts / 链接数量。
- 多选后 `Enter` 打开动作栏：Model/Provider 的 Bind 批量绑定到同一个目标；Link 的 Bind 新建链接。
- Link 列表四列：`ProviderId`、`modelId`、`协议`、`协议路径`；同一 Provider 只在组内首行显示 ProviderId，后续行留空以模拟合并。
- TUI 列表按 id 字母序（Link 按 ProviderId、modelId）；**导出仍按 rowid**，不要在 `build_document()` 中引入显示排序。
- 关联变更（`m`）会显示所有可关联对象；待绑定的每个目标单独确认协议/路径，最后原子提交。
- 关联视图按 `model_ref` 去重；同一 Provider 对同一 Model 需要多个 API model id 时，用 Link 视图按 `n` 新增。
- Model/Provider 编辑时 id 不可改；Link 编辑时 provider 不可改。修改 Link 的 API model id 使用 `UPDATE`，保留 rowid。
- Model 的推理强度顺序会保留；TUI 不提供拖动排序，新增勾选项追加到末尾。
- Provider 表单可配置 Logo source（models.dev / 自定义 URL / 本地文件）；新建或来源变化时立即尝试同步，失败只警告。

### 测试

```bash
python3 -m unittest discover -s tests -v
```

测试使用临时数据库，不会修改仓库中的 `catalog.sqlite`，也不会访问真实网络。

## Provider Logo

每个 provider 在 `logo/` 下有两套 SVG：

```
logo/<providerId>.svg        # 浅色/原始版本，保持来源字节不变
logo/<providerId>_dark.svg   # 深色版本，所有 <path> 的 fill 为 #CED0D6
```

获取方式保存在 `provider.logo_source`：

| 值 | 含义 |
|---|---|
| `default` | `https://models.dev/logos/{provider}.svg` |
| `https://.../{provider}.svg` | 自定义 URL 模板，仅支持 `{provider}` 占位符；不含占位符的固定 URL 也可 |
| `./assets/foo.svg` / `/abs/foo.svg` | 本地 SVG；相对路径以仓库根目录为基准，支持 `~` |

- `provider add` 默认 `default`；`provider set --logo-source ...` 可修改并立即同步。
- `catalog-cli logo sync [provider ...]`：无参数时处理全部 provider；默认只补缺失文件，`--force` 强制重新抓取。
- 下载失败、文件不存在、SVG 非法都只警告，provider 仍会保存；修复后重跑 `logo sync` 即可。
- dark 版本仅重写 `<path>` 的 `fill`（包括内联 `style` 中的 `fill:`）；`<circle>`、`<rect>` 等其他元素保持原样。
- 相对本地路径按 `catalog.py` 所在仓库根目录解析，与当前工作目录无关。
- CDN 路径：`https://catalog.ravel.oaix.tech/logo/<providerId>.svg` 与 `..._dark.svg`。
- 删除 provider 会同时删除对应 logo 文件（best effort）。

## 数据库表结构（5 张表）

```
registry                          -- 注册表元信息（format_version / sequence）
    │
model                             -- 模型定义
    ├── model_reasoning_effort     -- 推理强度（一对多，sort_order 决定导出顺序）
    │
provider                          -- 提供商
    └── provider_model             -- 提供商↔模型映射（多对多）
```

#### `model`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 模型唯一标识，如 `gpt-5.6-sol` |
| `display_name` | TEXT | 展示名 |
| `context_limit` | INTEGER | 上下文窗口大小（tokens） |
| `input_limit` | INTEGER? | 输入限制，可为 NULL |
| `output_limit` | INTEGER | 输出限制（tokens） |
| `tool_calling` | INTEGER | 0/1 |
| `structured_output` | INTEGER | 0/1 |
| `temperature` | INTEGER | 0/1 |

#### `model_reasoning_effort`

| 字段 | 类型 | 说明 |
|---|---|---|
| `model_id` | TEXT FK | 关联 model.id |
| `effort` | TEXT | `none` / `low` / `medium` / `high` / `xhigh` / `max` |
| `sort_order` | INTEGER | 数组导出顺序，从 0 开始 |

#### `provider`

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | TEXT PK | 如 `openai` |
| `display_name` | TEXT | 展示名 |
| `authentication` | TEXT | `bearer-api-key` / `x-api-key` |
| `base_url` | TEXT | API 基础 URL |
| `model_list_path` | TEXT | 模型列表路径 |
| `logo_source` | TEXT | logo 来源，默认 `default`（models.dev）；也可是 URL 模板或本地路径 |

#### `provider_model`

| 字段 | 类型 | 说明 |
|---|---|---|
| `provider_id` | TEXT FK | 关联 provider.id |
| `model_id` | TEXT | API 调用时使用的模型 ID |
| `model_ref` | TEXT FK | 关联 model.id（必须已存在） |
| `protocol_type` | TEXT | `openai-responses` / `anthropic-messages` / `openai-compatible` |
| `request_path` | TEXT | 请求端点路径 |

协议属于「provider ↔ model 链接」而非 provider，同一 provider 的不同模型可以用不同协议。

导出顺序：model / provider / provider_model 一律按插入顺序（`rowid`）。

## 导出结构（reviewed-registry.json v2）

- 顶层：`formatVersion`（当前 2）、`sequence`、`models`、`providers`。
- `models` 段：`reviewed-models-v1` schema（`schemaVersion` = 1），本次 v2 未改动。
- `providers` 段：`reviewed-providers-v2` schema（`schemaVersion` = 2）：
  - provider：`{id, displayName, baseUrl, modelList: {path, authentication}, models}`。
  - `models[]` 条目：`{modelId, modelRef, protocol: {type, requestPath}}`（v1 中协议在 provider 级，v2 下沉到每个链接）。
- `logo_source` 不进入导出 JSON；客户端按 provider id 约定推导 `/logo/<providerId>.svg`。

## 本地构建（不发布）

```bash
python3 catalog.py export                       # 生成 reviewed-registry.json
python3 catalog.py sign --key catalog.pem       # 生成 reviewed-registry.signed.json
cmp reviewed-registry.json <(git show prod:reviewed-registry.json)         # 与 CDN 内容对拍
cmp reviewed-registry.signed.json <(git show prod:reviewed-registry.signed.json)
```

## 发布流程（GitHub Actions）

推送到 main 且 `catalog.sqlite` 或 `logo/**` 有变化时，`.github/workflows/publish.yml` 自动：

1. 仅当 `catalog.sqlite` 变化时 `export --bump-sequence`（自增 sequence），否则普通 export
2. 用仓库 Secret `CATALOG_SIGNING_KEY` 签名（PKCS8 ed25519 PEM，明文或 base64 均可）
3. 把自增后的 `catalog.sqlite` 以 `[skip ci]` 提交回 main（未 bump 时不会产生提交）
4. force push 到 prod 分支（两个 JSON + `logo/`）

也可在 Actions 页手动 `workflow_dispatch`，可选择不 bump sequence（密钥轮换后重签）。

发布前请先 `git pull`，确保本地 `catalog.sqlite` 包含 CI 回写的 sequence。

## 签名格式（改动会破坏 CDN 客户端校验）

```jsonc
{
  "formatVersion": 1,
  "algorithm": "ed25519",
  "keyId": "ravel-public-ed25519",
  "signature": "<base64url 无填充，64 字节 ed25519 签名>",
  "payload": "<base64url 无填充，内容为 reviewed-registry.json 的原始字节>"
}
```

- 信封的 `formatVersion` 独立于 registry 的 `formatVersion`（信封固定 1 = `SIGNED_FORMAT_VERSION`，registry 当前为 2）。
- `payload` 与 `signature` 均为无填充 base64url：客户端用 URL-safe 解码，且要求 `urlsafe_encode(decoded) == value`，不得出现 `+` / `/` / `=`。
- `payload` 解码后必须与 `reviewed-registry.json` **逐字节一致**。
- 签名的消息是 `payload` 解码后的字节（即 JSON 原始字节）。
- ed25519 是确定性签名：同一 payload + 同一私钥必须产生相同 `signature`。
- `catalog.pem` 与 GitHub Secret 中的私钥为同一把；公钥与 keyId 见 `env` 文件（客户端配置）。
- 本地签名依赖 `openssl pkeyutl -sign -rawin`，其 `-in` 参数必须是真实文件（stdin / 进程替换会报错）。

## 常用验证查询

```sql
-- 查看支持 max 推理强度的模型
SELECT DISTINCT m.id, m.display_name FROM model m
JOIN model_reasoning_effort r ON r.model_id = m.id WHERE r.effort = 'max';

-- 查看某 provider 的所有模型
SELECT pm.model_id, m.display_name FROM provider_model pm
JOIN model m ON m.id = pm.model_ref WHERE pm.provider_id = 'openai';
```
