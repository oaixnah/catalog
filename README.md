# Catalog

Ravel 的自建模型/提供商目录。数据源为 `catalog.sqlite`，由其生成 `reviewed-registry.json` 与 `reviewed-registry.signed.json`，通过 CI 发布到 `prod` 分支，由 CDN（https://catalog.ravel.oaix.tech）托管。

- 维护手册：[CATALOG_GUIDE.md](CATALOG_GUIDE.md)
- CLI：`python3 catalog.py --help`
