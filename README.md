# Catalog

Ravel 的自建模型/提供商目录。数据源为 `catalog.sqlite`，由其生成 `reviewed-registry.json` 与 `reviewed-registry.signed.json`；`logo/` 保存每个 provider 的明暗两套 SVG。CI 将两个 JSON 与 `logo/` 发布到 `prod` 分支，由 CDN（https://catalog.ravel.oaix.tech）托管。

- 维护手册：[CATALOG_GUIDE.md](CATALOG_GUIDE.md)
- CLI：`catalog-cli --help`

## 安装 CLI

在仓库根目录的虚拟环境中做一次 editable 安装，即可使用 `catalog-cli`：

```bash
uv pip install -e .        # 或 source .venv/bin/activate && python3 -m pip install -e .
catalog-cli --help
```

`python3 catalog.py ...` 仍然可用，CI 也继续使用它。必须使用 `-e`（editable）：数据库、签名私钥和导出产物都在仓库根目录，CLI 默认按 `catalog.py` 所在目录定位它们。

## Provider Logo

`logo/<providerId>.svg` 与 `logo/<providerId>_dark.svg` 由 provider 的 `logo_source` 生成。默认来源是 `https://models.dev/logos/{provider}.svg`；也可以使用只含 `{provider}` 的 http(s) URL 模板或本地 SVG 路径：

```bash
catalog-cli provider add ... --logo-source default
catalog-cli provider set <id> --logo-source https://example.com/logos/{provider}.svg
catalog-cli provider set <id> --logo-source ./assets/openai.svg
catalog-cli logo sync [provider ...] [--force]
```

dark 版本会把所有 `<path>` 的 `fill` 设为 `#CED0D6`；获取失败只警告，provider 仍会保存。CDN 路径为 `/logo/<providerId>.svg` 与 `/logo/<providerId>_dark.svg`。

## 交互式 TUI

```bash
catalog-cli tui                          # 默认打开 Models
catalog-cli --db /tmp/x.sqlite tui       # 在数据库副本上操作
catalog-cli tui --view links             # 直接打开 Links
```

TUI 基于 Textual，依赖随 `pip install -e .` 安装。常用键位：

- `1/2/3` 切换 Models / Providers / Links
- `↑/↓` 移动，`→` 详情，`←` 返回/清除勾选
- `Space` 多选，`Enter` 打开动作栏，`Tab` 切换 Delete / Bind
- `d` 删除/解绑，`e` 编辑，`m` 关联管理，`n` 新增
- `?` 帮助，`q` 退出

TUI 改动会立即写入 `catalog.sqlite`，不会自动 export/sign；发布仍由 CI 完成。列表按字母排序，但导出的 JSON 仍按 rowid 顺序。完整键位见 [CATALOG_GUIDE.md](CATALOG_GUIDE.md)。
