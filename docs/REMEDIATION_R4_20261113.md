# 第四轮整改：第三轮复审问题的修复证据

日期：2026-11-13。依据：`docs/V0.1_第三轮整改复审_20261113.md` 第 1 节（3 项 P1 + 1 项 P2）。

本文档只记录改了什么、用什么证明、还剩什么没验证；不含审查结论。

---

## 一、总览

| 项 | 优先级 | 复审结论 | 本轮修法 | 证据 |
|---|---|---|---|---|
| 并行上传删除共用原件 | P1 | 失败方无条件删除路径；同扩展名时两个请求发布同一路径，删掉的是胜者登记的文件 | 只在「胜者对象指向别的路径」时才删除 | `test_upload_dedupe.py` 8 项 |
| 登记失败已提交再删内容 | P1 | `create_parse_run` 内部提交，外层回滚撤不掉；失败仍消耗修订号 | `create_parse_run(commit=False)`；完整性失败先回滚再单独标记 | `test_register_concurrency.py` 6 项 |
| 部分覆盖挡住应补发的完整索引 | P1 | 只取最新终态，遇到 partial 被拒就结束 | 按「complete 优先、同状态取最新」选候选 | `test_r03_publish_commit.py` 12 项 |
| 切换材料后按钮一直禁用 | P2 | 切换作废甲请求，但没有重置 `busy` | 切换时 `setBusy(false)` | `MaterialSwitch.test.tsx` 6 项 |

当前：后端 **262 passed**，前端 **23 passed**，验收 13/13，恢复演练 7/7。

---

## 二、逐项说明

### P1-1 并行上传删除共用原件（`imports.py`）

**复审复现**：两个连接上传相同字节的 TXT，用同步屏障确保都完成物理发布后再写 `StoredObject`。两个批次都成功、库里只有一个对象，但对象校验失败；worker 跑完后两份材料的解析任务都失败。原因是保存点内唯一冲突后，失败方无条件删除路径；同扩展名时两个请求发布的是同一路径。

**上一轮的错误**：上一轮用 `published.created` 判断「这个文件是不是我建的」。判据方向错了——**同扩展名时创建文件的那一方也可能输掉插入**，因为它建的正是胜者要用的那个路径。真正该问的是：胜者登记的对象是不是就指向这个路径。

**修法**：

```python
except IntegrityError:
    obj = ...  # 胜者的行
    if published.relative_path != obj.relative_path:
        storage.delete_unpublished(published.relative_path)
```

**证据**（`test_upload_dedupe.py` 8 项，本轮新增 2 项）：

- `test_parallel_same_extension_keeps_the_shared_original`：`Barrier` 同步起跑，两个批次都成功；断言对象存在、**哈希校验通过**、文件在盘上、两条材料；再跑 worker，两个 import 任务都 `succeeded`、两份材料都生成。原回归只查「磁盘文件都在数据库中登记」，磁盘为空也能通过，所以没抓到。
- `test_parallel_different_extension_keeps_every_referenced_file`：同场景但扩展名不同，发布路径不同；失败方可以删自己的文件，但登记对象必须可校验。
- 反向验证：去掉路径比较后，同扩展名用例报 `共用原件校验失败: .../source.txt`，与复审描述一致。

### P1-2 登记失败已提交再删内容（`materials.py` + `pipeline.py`）

**复审复现**：在审计写入处注入失败。新连接仍看到工作副本 revision=1、一个工作版本和一个排队任务，但文件校验失败。根因是 `create_parse_run` 内部 `session.commit()`，外层回滚撤不掉已提交的 run/job/revision，清理却仍删除文件。另一条路径（校验失败分支）复现了「登记失败、无工作版本、revision 已加一」。

**修法**：

1. `create_parse_run` 增加 `commit: bool = True`。默认行为不变（HTTP/CLI 入口仍提交），登记路径传 `commit=False`，于是「修订号 + 新版本 + run + job + 审计」在同一事务里一次提交。
2. 完整性失败分支改为：先 `rollback()`（撤销 CAS 消耗的修订号），再在独立事务里写 `integrity_status`，最后抛错。

**证据**（`test_register_concurrency.py` 6 项，本轮新增 2 项）：

- `test_failure_after_parse_run_leaves_no_partial_state`：monkeypatch 审计写入抛错；新连接读到 revision=0、无工作版本、无解析任务、无孤儿文件；撤销 patch 后同一修订号能重新登记成功。
- `test_integrity_failure_does_not_consume_a_revision`：构造「记录的哈希等于待登记字节、盘上文件内容不同」，命中复用分支的校验失败；断言抛 `INTEGRITY_ERROR`、revision 未变、无新版本。

### P1-3 部分覆盖挡住完整索引（`pipeline.py`）

**复审复现**：同一版本有序号 0（complete，当前活动）、1（complete，漏发）、2（partial）。worker 启动后指针仍在 0，预期应到 1。上一轮只取最新终态 run，拿到 partial，被 `_publish_active` 正当地拒绝（partial 不得顶替 complete），然后就结束了。

**修法**：候选排序改为「complete 优先，同状态取最新」：

```python
.order_by((models.ParseRun.status == "complete").desc(),
          models.ParseRun.sequence.desc())
```

`_publish_active` 仍是唯一的裁决点，所以「partial 不得顶替 complete」「不得回退到更旧」这两条规则没有第二份实现。

**证据**（`test_r03_publish_commit.py` 12 项，本轮新增 2 项）：

- `test_repair_skips_a_newer_partial_and_takes_the_complete_behind_it`：指针停在 run0、run1 漏发、run2 是 partial → 补发后指针到 run1，检索命中。
- `test_repair_from_no_active_pointer_with_a_newer_partial`：无活动指针的同款混合 → 也到 run1（不是 run2）。
- 原有「不覆盖更新版本」「历史 source_ref 仍有效」继续通过。

### P2 切换后按钮一直禁用（`MaterialDetail.tsx`）

**复审复现**：甲点「创建工作副本」，切到乙并等乙原件加载完成，再让甲请求返回。乙的「创建工作副本」仍禁用，保存和重新解析共享该状态。

**修法**：切换材料的 effect 里 `setBusy(false)`。甲的回包被代次与归属检查丢弃后不会再复位 `busy`，所以必须在切换时复位。

**证据**（`MaterialSwitch.test.tsx` 6 项，本轮新增 1 项）：

- `test_切换后仍可继续操作`：甲的副本请求跨越切换、迟到返回后，乙的「创建工作副本」「重新解析」「保存」全部可用，而「登记为新版本」仍禁用（乙确实没有副本）。
- 反向验证：去掉 `setBusy(false)` 后该用例报 `to have property "disabled" with value false`。

---

## 三、本轮的反向验证清单

| 修复 | 还原后实测 |
|---|---|
| 按胜者路径决定是否删除 | `共用原件校验失败: .../source.txt` |
| `create_parse_run(commit=False)` | 见 `test_failure_after_parse_run_leaves_no_partial_state` 的 revision/版本/任务断言 |
| 完整性失败先回滚 | `失败登记消耗了修订号` |
| 候选按 complete 优先 | 指针停在 run0 而非 run1 |
| 切换时复位 `busy` | `to have property "disabled" with value false` |

---

## 四、三轮复审的共同教训

三次复审推翻的项，判据问题比实现问题更一致：

| 轮次 | 被推翻的项 | 判据缺陷 |
|---|---|---|
| 二 | R03 提交间隙、R02 多点名、CAS、去重、init、切换、测试库 | 注入点不在真实提交边界；只测单点文件名；CAS 返回值未用 |
| 三 | 补发候选、改名 `.v2`、并行登记、切换正文/副本 | 补发查询条件自己猜规则；`.v2` 判据看字符形式；CAS 在副作用之后；代次只作废一个 |
| 四 | 并行删除、登记事务、补发候选（混合终态）、`busy` | 判据问「谁建的」而非「谁在用」；内层提交未察觉；只看最新一个；复位点漏一处 |

可复用的规则：**断言要问「用户能感知的结果」，不要问「我以为的那个中间状态」**。上传那条原回归问「磁盘文件都在库里登记」，所以磁盘为空也通过；改成问「登记对象哈希可校验、材料能解析」才抓得住。

---

## 五、仍未验证

| 项 | 说明 |
|---|---|
| Linux 平台 | 仅 macOS 27.0.1 |
| 真实案件材料 | 全部 `TEST-ONLY` 合成数据 |
| 5 万页以上 | 验收规模 1000 文件 / 10000 页 |
| 备份进程 `kill -9` | 演练用正常终止 |
| 加密 Office | 未测 |
| 多用户并发写 | 单用户设计，未测 |
| 真实浏览器限速 | 材料切换用受控 Promise；未在浏览器里人为限速 |
| 崩溃注入方式 | `RuntimeError` 模拟，不是真实 `SIGKILL` |

## 六、待用户提供

- 原有 22 条检索测试的样本与期望结果。
- 原审查六项目对应的仓库或 commit 清单。
