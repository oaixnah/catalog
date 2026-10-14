# AGENTS.md

## 项目概览

Ravel 的自建模型/提供商目录。**`catalog.sqlite` 是唯一数据源**，由它生成 `reviewed-registry.json` 与 `reviewed-registry.signed.json`，CI 发布到 `prod` 分支（仅这两个文件）供 CDN 托管：https://catalog.ravel.oaix.tech。

## 分支与数据流（最容易踩错）

```
catalog.sqlite → catalog.py export → reviewed-registry.json → catalog.py sign → reviewed-registry.signed.json → prod → CDN
```

- main：只放 `catalog.sqlite` / `catalog.py` / 文档；两个 JSON 已被 `.gitignore` 忽略，**不要提交到 main**。
- prod：由 `.github/workflows/publish.yml` 强制推送，永远只含两个 JSON；用 `git show prod:reviewed-registry.json` 查看线上内容，**不要手工提交**；不要给 prod 开分支保护（CI 需要 force push）。
- 旧的「编辑 JSON → 导入 SQLite」脚本已删除；不要把 JSON 当数据源改回去。
- CI 每次发布把 `sequence` +1 并回写 `catalog.sqlite` 到 main（`[skip ci]`）：**改数据前先 `git pull`**。若 main 有分支保护，需放行 GitHub Actions 推送。

## 常用命令

- CRUD：`python3 catalog.py model|provider|link list|show|add|set|rm ...`，帮助 `python3 catalog.py --help`
- 校验：`python3 catalog.py validate`；`export` 会先校验，失败拒绝导出
- 本地构建（不发布）：`python3 catalog.py export`，再 `python3 catalog.py sign --key catalog.pem`
- 测试务必用 DB 副本：`cp catalog.sqlite /tmp/x.sqlite && python3 catalog.py --db /tmp/x.sqlite ...`
- 全新 DB：`python3 catalog.py init`（建表 + 空 registry 行）

## 硬性约束

- `catalog.pem`（私钥）与 `env` 绝不提交；CI 使用 Secret `CATALOG_SIGNING_KEY`（PKCS8 ed25519 PEM，明文或 base64 均可）。
- `sign` 需要 OpenSSL >= 3（macOS 自带 LibreSSL 不支持）；`openssl pkeyutl -sign -rawin` 的 `-in` 必须是真实文件，stdin / 进程替换会失败。
- 逐字节复现是验收标准，改动导出/签名格式会破坏 prod 内容可复现性：
  - export：4 空格缩进 + 末尾换行；model/provider 按 `rowid` 顺序、efforts 按 `sort_order`。
  - signed：2 空格缩进、**无**末尾换行。
  - `payload` = JSON 原始字节的标准 base64（去 `=`）；`signature` = 对 payload 解码后字节的 ed25519 签名，base64url（去 `=`）。ed25519 确定性：同 payload + 同私钥必得同一 signature。
- 枚举：efforts = none/low/medium/high/xhigh/max；authentication = bearer-api-key/x-api-key；protocol = openai-responses/anthropic-messages/openai-compatible。
- `provider_model.model_ref` 必须存在于 model 表（外键；`model rm` 会级联删除 reasoning efforts 与 provider 关联）。
- schema 变更必须同步 `catalog.py` 的 `SCHEMA_SQL`、导出逻辑与 `CATALOG_GUIDE.md`。

## 验证回归

无测试框架。发布前（无未发布改动时）本地应能复现 prod 内容：

```bash
python3 catalog.py validate \
  && python3 catalog.py export \
  && python3 catalog.py sign --key catalog.pem \
  && cmp reviewed-registry.json <(git show prod:reviewed-registry.json) \
  && cmp reviewed-registry.signed.json <(git show prod:reviewed-registry.signed.json)
```

详细维护手册见 `CATALOG_GUIDE.md`。
