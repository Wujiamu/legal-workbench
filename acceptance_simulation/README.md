# 法律工作台全格式仿真验收套件（TEST-ONLY）

本目录用于向外部验收 Agent 提供一套**零真实案件隐私风险、全格式、带预设核对陷阱**的仿真案件材料与配套工作流入口，供直接对运行在 `http://127.0.0.1:8765` 的本地法律工作台执行端到端真实验收。

## 目录结构

```
acceptance_simulation/
├── generate_dossier.py      # 仿真材料可重复生成脚本（仅写本地静态文件，不碰数据库）
├── README.md                # 本说明
├── HANDOFF_PROMPT.md        # 可直接复制发给另一个 Agent 的验收任务指令
├── ANSWER_KEY.md            # 标准答案与核对基准（含精确整数分、陷阱、诱饵明细）
└── dossier/                 # 10 份全格式仿真材料（均带 TEST-ONLY 显著标识）
    ├── 01_诉讼文书/01_民事起诉状.docx
    ├── 02_核心合同与担保/02_成套设备采购主合同.pdf
    ├── 02_核心合同与担保/03_最高额连带责任保证函（扫描件）.pdf
    ├── 02_核心合同与担保/04_战略合作意向书（未生效）.txt
    ├── 03_财务流水与对账/05_标准往来明细台账.xlsx
    ├── 03_财务流水与对账/06_非标银行对账单_多表头混列.csv
    ├── 04_履约往来与暗线材料/07_催款函与签收回执（拍照件）.png
    ├── 04_履约往来与暗线材料/08_工商变更与关联核查底档.txt
    ├── 04_履约往来与暗线材料/09_证据目录清单草稿.xlsx
    └── 04_履约往来与暗线材料/10_损坏扫描附件（模拟损坏）.pdf
```

## 10 份材料覆盖的核心验收点

| 文件 | 格式特征 | 重点覆盖工作台能力 |
|---|---|---|
| `01_民事起诉状.docx` | DOCX＋嵌套表格 | 当事人/简称消歧、诉请金额与抵扣/重叠排除、办案期限建议 |
| `02_成套设备采购主合同.pdf` | 3 页文字层 PDF | 分期金额/日期精确抽取、PDF 逐页预览、强指纹＋**身份证/物流码防误报** |
| `03_最高额连带责任保证函（扫描件）.pdf` | **纯图像扫描 PDF** | 离线 RapidOCR 识别、保证人→主债务人担保关系方向、600 万担保限额 |
| `04_战略合作意向书（未生效）.txt` | UTF-8 TXT | 无约束力声明（AC10 证明力越界复核陷阱）、孤儿机构差集 |
| `05_标准往来明细台账.xlsx` | 双 Sheet XLSX（含公式） | 标准列映射出边、`Decimal` 整数分、多币种（CNY/USD）、异常行进 `skipped_rows` |
| `06_非标银行对账单_多表头混列.csv` | UTF-8-BOM 多表头 CSV | 覆盖图标记未解析格式、`nonstandard-table-script-sop` 外部 Python 全量计算留痕 |
| `07_催款函与签收回执（拍照件）.png` | 高清中文拍照件 PNG | 图片 RapidOCR、原图预览、850 万欠款确认及跨文件手机指纹碰撞 |
| `08_工商变更与关联核查底档.txt` | UTF-8 TXT | 4 类强指纹跨材料碰撞、2 家孤儿机构复现、**仅简称出现**的双主体共现秒查 |
| `09_证据目录清单草稿.xlsx` | XLSX 目录清单 | 证据目录导入、材料绑定与逐条人工确认及实时编号 |
| `10_损坏扫描附件（模拟损坏）.pdf` | 截断无效 PDF | 损坏容错（不崩溃同批/worker），在覆盖格子图中明确归类为失败 |

## 就绪的 4 个工作空间 Skill

工作空间 `.agents/skills/`（及权威源 `docs/skills/`）已内置对齐本机环境的 4 个 Skill：
1. `legal-workbench-e2e-acceptance`：十阶段端到端验收主 SOP。
2. `legal-workbench-cli`：全部 CLI/同源上传入口（基线 `0008_claim_amounts + 2026-11-22`）。
3. `nonstandard-table-script-sop`：非标表格先读样本再写本地 `Decimal` 脚本留痕规程。
4. `chart-substantive-review`：消费 `export-chart-audit-bundle` 的对外图表四步实质盲审规程。

## 导入方式与注意事项

- 当前正式 `.env` 中 `LWB_IMPORT_ROOTS` 为空，CLI `import` 处于关闭状态；验收时请通过浏览器界面（`http://127.0.0.1:8765`）直接上传 `acceptance_simulation/dossier/`，或调用同源 multipart 接口 `POST /api/v1/cases/{case_id}/intakes/upload`（须带请求头 `Origin: http://127.0.0.1:8765`）。
- 如确需测试 CLI `import`，可在维护窗口由操作者在 `.env` 显式追加 `LWB_IMPORT_ROOTS=/path/to/legal-workbench/acceptance_simulation/dossier` 后重启服务。
- 若需重新生成干净测试文件，执行 `backend/.venv/bin/python acceptance_simulation/generate_dossier.py` 即可（全过程零数据库写入）。
