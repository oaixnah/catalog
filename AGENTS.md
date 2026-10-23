# AGENTS.md

## 项目概览

Ravel 的自建模型/提供商目录。**`catalog.sqlite` 是唯一数据源**，由它生成 `reviewed-registry.json` 与 `reviewed-registry.signed.json`；`logo/` 存放 provider 的明暗两套 SVG。CI 发布到 `prod` 分支（两个 JSON + `logo/`）供 CDN 托管：https://catalog.ravel.oaix.tech。CLI 通过 editable 安装暴露为 `catalog-cli`。

## 分支与数据流（最容易踩错）

```
catalog.sqlite ─┬→ catalog.py export → reviewed-registry.json ─┐
                │                                             ├→ sign → prod → CDN
logo/<id>.svg ──┴─────────────────────────────────────────────┘
logo/<id>_dark.svg
```

- main：放 `catalog.sqlite` / `catalog.py` / `pyproject.toml` / `logo/` / 文档；两个 JSON 已被 `.gitignore` 忽略，**不要提交到 main**。
- prod：由 `.github/workflows/publish.yml` 强制推送，永远只含两个 JSON 与 `logo/`；用 `git show origin/prod:reviewed-registry.json` 查看线上内容（先 `git fetch origin prod`），**不要手工提交**；不要给 prod 开分支保护（CI 需要 force push）。
- 旧的「编辑 JSON → 导入 SQLite」脚本已删除；不要把 JSON 当数据源改回去。
- CI **不再回写 main**：发布时从 `prod` 读取当前 `sequence`，导出内容与 prod 相同则复用，不同才 +1；纯 logo 更新不会 bump。`catalog.sqlite` 里的 `sequence` 只是本地兜底值，发布序列以 `prod` 为准。

## 常用命令

- 安装 CLI（进入虚拟环境后，必须 editable）：`uv pip install -e .` 或 `python3 -m pip install -e .`；之后 `catalog-cli` 与 `python3 catalog.py` 等价
- CRUD：`catalog-cli model|provider|link list|show|add|set|rm ...`，帮助 `catalog-cli --help`
- Logo：`catalog-cli logo sync [provider ...] [--force]`；add/set 时用 `--logo-source` 指定来源
- TUI：`catalog-cli tui [--view models|providers|links]`（依赖 Textual，随 `pip install -e .` 安装）
- 校验：`catalog-cli validate`；`export` 会先校验，失败拒绝导出
- 本地构建（不发布）：`catalog-cli export [--sequence N]`，再 `catalog-cli sign --key catalog.pem`；默认使用 DB 里的 sequence，`--sequence` 只覆盖本次导出、不改 DB
- 测试务必用 DB 副本：`cp catalog.sqlite /tmp/x.sqlite && catalog-cli --db /tmp/x.sqlite ...`
- 自动化测试：`python3 -m unittest discover -s tests -v`（临时 DB，不改仓库数据）
- 全新 DB：`catalog-cli init`（建表 + 空 registry 行）

## 硬性约束

- `catalog.pem`（私钥）与 `env` 绝不提交；CI 使用 Secret `CATALOG_SIGNING_KEY`（PKCS8 ed25519 PEM，明文或 base64 均可）。
- `sign` 需要 OpenSSL >= 3（macOS 自带 LibreSSL 不支持）；`openssl pkeyutl -sign -rawin` 的 `-in` 必须是真实文件，stdin / 进程替换会失败。
- 逐字节复现是验收标准，改动导出/签名格式会破坏 prod 内容可复现性：
  - export：4 空格缩进 + 末尾换行；model/provider 按 `rowid` 顺序、efforts 按 `sort_order`。
  - signed：2 空格缩进、**无**末尾换行。
  - `payload` = JSON 原始字节的 base64url（去 `=`，无填充）；`signature` = 对 payload 解码后字节的 ed25519 签名，base64url（去 `=`）。ed25519 确定性：同 payload + 同私钥必得同一 signature。
- 导出格式 v2：顶层 `formatVersion` = 2；`models` 段仍为 reviewed-models-v1（`schemaVersion` = 1）；`providers` 段为 reviewed-providers-v2（`schemaVersion` = 2，provider 含 `modelList: {path, authentication}`，协议下沉到每个 `models[]` 条目的 `protocol`）。
- TUI 的字母排序只用于显示：`tui/data.py` 可以排序，`build_document()` 必须继续按 `rowid` 导出，不要复用 TUI 的排序逻辑。
- TUI 直接写 `catalog.sqlite`，不会自动 export / sign；发布 sequence 由 CI 根据 prod 计算；不要把它接进发布链路。
- `export --sequence N` 只覆盖本次导出值，绝不修改 DB；CI 不再回写 main，不要恢复 `--bump-sequence` 或自动 push main 的逻辑。
- 修改 Link 的 `model_id` 用 `UPDATE` 保留 rowid；新增记录才会追加 rowid。TUI 已按此实现，改动时不要退化成 delete+insert。
- 签名信封 `formatVersion` 独立于 registry，固定为 1（`SIGNED_FORMAT_VERSION`），不受 registry 格式升级影响。
- 枚举：efforts = none/low/medium/high/xhigh/max；authentication = bearer-api-key/x-api-key；protocol = openai-responses/anthropic-messages/openai-compatible（protocol 存在 `provider_model` 行上，`link bind` 时必填）。
- `provider_model.model_ref` 必须存在于 model 表（外键；`model rm` 会级联删除 reasoning efforts 与 provider 关联）。
- logo 固定为 `logo/<providerId>.svg` 与 `logo/<providerId>_dark.svg`，必须提交到 main 并随 prod 发布；CDN 路径为 `/logo/<providerId>.svg`。
- `provider.logo_source` 默认 `default`，解析为 `https://models.dev/logos/{provider}.svg`；也支持只含 `{provider}` 占位符的 http(s) URL 模板或本地 SVG 路径。相对路径以仓库根目录为基准。
- dark 版本由 light 版本生成：所有 `<path>` 的 `fill` 必须设为 `#CED0D6`（含内联 style 的 fill 声明）；light 版本保持原始字节。
- logo 抓取失败不阻止 provider 保存，只警告；`catalog-cli logo sync [--force]` 用于补全/刷新。
- 测试不得访问真实网络：URL 抓取用 mock，本地文件用临时目录。
- schema 变更必须同步 `catalog.py` 的 `SCHEMA_SQL`、迁移逻辑、导出逻辑与 `CATALOG_GUIDE.md`。

## 验证回归

自动化测试：`python3 -m unittest discover -s tests -v`（TUI 使用 Textual headless，临时 DB）。发布前（无未发布改动、且 prod 已是同一格式版本时）本地应能复现 prod 内容：

```bash
git fetch origin prod \
  && SEQUENCE=$(git show origin/prod:reviewed-registry.json | python3 -c 'import json,sys; print(json.load(sys.stdin)["sequence"])') \
  && catalog-cli validate \
  && catalog-cli export --sequence "$SEQUENCE" \
  && catalog-cli sign --key catalog.pem \
  && cmp reviewed-registry.json <(git show origin/prod:reviewed-registry.json) \
  && cmp reviewed-registry.signed.json <(git show origin/prod:reviewed-registry.signed.json)
```

详细维护手册见 `CATALOG_GUIDE.md`。
