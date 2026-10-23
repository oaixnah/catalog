# Catalog

Ravel 的自建模型/提供商目录。数据源为 `catalog.sqlite`，由其生成 `reviewed-registry.json` 与 `reviewed-registry.signed.json`，通过 CI 发布到 `prod` 分支，由 CDN（https://catalog.ravel.oaix.tech）托管。

- 维护手册：[CATALOG_GUIDE.md](CATALOG_GUIDE.md)
- CLI：`catalog-cli --help`

## 安装 CLI

在仓库根目录的虚拟环境中做一次 editable 安装，即可使用 `catalog-cli`：

```bash
uv pip install -e .        # 或 source .venv/bin/activate && python3 -m pip install -e .
catalog-cli --help
```

`python3 catalog.py ...` 仍然可用，CI 也继续使用它。必须使用 `-e`（editable）：数据库、签名私钥和导出产物都在仓库根目录，CLI 默认按 `catalog.py` 所在目录定位它们。
