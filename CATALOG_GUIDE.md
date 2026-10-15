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
    │  GitHub Actions（.github/workflows/publish.yml）force push
    ▼
prod 分支（仅这两个 JSON 文件）→ CDN https://catalog.ravel.oaix.tech
```

- **main**：只放 `catalog.sqlite`、`catalog.py`、文档。两个 JSON 由 `.gitignore` 忽略，**不要提交到 main**。
- **prod**：由 CI 自动维护的孤立分支，永远只包含 `reviewed-registry.json` 与 `reviewed-registry.signed.json`，**不要手工提交**。
- 旧流程（编辑 JSON → 导入 SQLite）已废弃，`import_to_sqlite.py` / `import.sql` 已删除。

## 环境要求

- Python 3.10+（仅标准库）
- OpenSSL >= 3（仅签名需要；macOS 自带的 LibreSSL 不支持，请用 `brew install openssl@3`）

## CLI 速查

```bash
python3 catalog.py --help                 # 全部命令
python3 catalog.py validate               # 检查外键/枚举等一致性
python3 catalog.py init                   # 在全新文件上建表（灾难恢复用）

python3 catalog.py model list
python3 catalog.py model show <id>
python3 catalog.py model add --id gpt-5.6-nova --name "GPT-5.6 Nova" \
    --context 1050000 --input 922000 --output 128000 --efforts none,low,medium,high,xhigh,max
python3 catalog.py model set <id> --temperature --efforts low,high,max
python3 catalog.py model rm <id>

python3 catalog.py provider list
python3 catalog.py provider add --id groq --name Groq --authentication bearer-api-key \
    --base-url https://api.groq.com/openai --model-list-path /v1/models
python3 catalog.py provider set <id> --base-url https://api.example.com
python3 catalog.py provider rm <id>

python3 catalog.py link list
python3 catalog.py link bind <provider> <model_ref> [--api-model-id <api_model_id>] \
    --protocol openai-compatible --request-path /v1/chat/completions
python3 catalog.py link unbind <provider> <model_id>

python3 catalog.py registry show          # format_version / sequence / created_at
```

- `model set --efforts` 会**整体替换**推理强度列表，顺序即导出顺序。
- `--input null` 表示不单独限制输入。
- 冒烟测试请复制数据库后加 `--db`：`cp catalog.sqlite /tmp/x.sqlite && python3 catalog.py --db /tmp/x.sqlite ...`

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

## 本地构建（不发布）

```bash
python3 catalog.py export                       # 生成 reviewed-registry.json
python3 catalog.py sign --key catalog.pem       # 生成 reviewed-registry.signed.json
cmp reviewed-registry.json <(git show prod:reviewed-registry.json)         # 与 CDN 内容对拍
cmp reviewed-registry.signed.json <(git show prod:reviewed-registry.signed.json)
```

## 发布流程（GitHub Actions）

推送到 main 且 `catalog.sqlite` 有变化时，`.github/workflows/publish.yml` 自动：

1. `export --bump-sequence`：sequence 自增后导出 JSON
2. 用仓库 Secret `CATALOG_SIGNING_KEY` 签名（PKCS8 ed25519 PEM，明文或 base64 均可）
3. 把自增后的 `catalog.sqlite` 以 `[skip ci]` 提交回 main
4. force push 到 prod 分支（仅两个文件）

也可在 Actions 页手动 `workflow_dispatch`，可选择不 bump sequence（密钥轮换后重签）。

发布前请先 `git pull`，确保本地 `catalog.sqlite` 包含 CI 回写的 sequence。

## 签名格式（改动会破坏 CDN 客户端校验）

```jsonc
{
  "formatVersion": 1,
  "algorithm": "ed25519",
  "keyId": "ravel-public-ed25519",
  "signature": "<base64url 无填充，64 字节 ed25519 签名>",
  "payload": "<标准 base64 无填充，内容为 reviewed-registry.json 的原始字节>"
}
```

- 信封的 `formatVersion` 独立于 registry 的 `formatVersion`（信封固定 1 = `SIGNED_FORMAT_VERSION`，registry 当前为 2）。
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
