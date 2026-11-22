# 0001 Anki 背面双向同步与冲突保护

- 状态：已接受
- 日期：2026-11-22

## 背景

系统当前是单向推送：只把 `ready` 卡片写入 Anki，不读取 Anki 的修改（见 README「单向推送」）。

这带来一个隐性风险：

- 用户只在 Anki 补充背面时，系统内容没变，`pushed_hash` 一致，推送会跳过，暂时不会覆盖。
- 一旦系统卡片变化（手动编辑、生成 AI 答案、合并等），`pushed_hash` 失效，下次推送会调用 `updateNoteFields`，把 Front 和 Back 一起写回，**静默覆盖**用户在 Anki 的补充。
- 系统不会把 Anki 的修改读回来，卡片详情、搜索、导出看到的仍是旧内容。

需要在不破坏正面识别键的前提下，保护 Anki 侧对背面的编辑。

## 决策

### 1. 字段归属

- **正面**：系统维护。用于去重、候选匹配、Anki 笔记接管。
- **背面**：双向。系统可以生成/编辑，Anki 也可以补充。
- 牌组、标签：系统维护，不参与冲突判定。

### 2. 同步基线

在 `anki_sync` 增加按字段的基线：

```sql
ALTER TABLE anki_sync ADD COLUMN pushed_front_hash TEXT;
ALTER TABLE anki_sync ADD COLUMN pushed_back_hash TEXT;
```

- 基线是“最近一次成功同步后，双方一致的内容”的规范化哈希。
- 现有 `pushed_hash` 保留，继续表示整张卡（含牌组、媒体引用）是否需要处理；不再作为字段级判断的唯一依据。
- 原来把 `pushed_hash` 置空的地方，同时清空两个字段基线，强制重新判定。

### 3. 规范化与“算不算修改”

系统字段和 Anki 字段都先转成规范化 Markdown，再计算哈希：

- 统一换行符（CRLF → LF）。
- 解码 HTML 实体。
- 去掉每行行尾空白和首尾空行。
- Anki 字段如果包含 HTML 标签，先用 `anki_to_markdown` 转回 Markdown；纯 Markdown 保持原样。
- 图片增删、文字增删、加粗/列表等语义变化都算修改。

仅换行符、HTML 实体、行尾空白不同，不算修改。

### 4. 判定矩阵

对每个 `ready` 且已关联 note 的卡片，逐字段判断：

| 系统侧 | Anki 侧 | 处理 |
| --- | --- | --- |
| 未变 | 未变 | 跳过 |
| 变了 | 未变 | 只推送变化的字段 |
| 未变 | 变了（背面） | 回写到系统背面，更新基线 |
| 未变 | 变了（背面，且为空） | 不自动回写，记为冲突 |
| 未变 | 变了（正面） | 记为冲突，不自动采纳 |
| 变了 | 变了，规范化后相同 | 视为一致，更新基线 |
| 变了 | 变了，规范化后不同 | 记为冲突，不覆盖 |

牌组变化只调用 `changeDeck`，不碰字段。

### 5. 冲突记录

新增 `anki_conflict` 表：

```sql
CREATE TABLE anki_conflict (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kp_id INTEGER NOT NULL REFERENCES knowledge_point(id) ON DELETE CASCADE,
    anki_note_id INTEGER NOT NULL,
    field TEXT NOT NULL CHECK (field IN ('front', 'back')),
    base_hash TEXT,
    system_value TEXT NOT NULL,
    anki_value TEXT NOT NULL,
    system_hash TEXT NOT NULL,
    anki_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'resolved')),
    resolution TEXT CHECK (resolution IN ('system', 'anki')),
    note TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE UNIQUE INDEX idx_anki_conflict_pending
    ON anki_conflict(kp_id, field) WHERE status = 'pending';
```

- 同一张卡的同一个字段最多一条待处理冲突；再次检测到变化时刷新快照和 `note`。
- 保存双方快照，用于展示、解决和事后恢复。
- `note` 记录媒体拉取失败等原因。

### 6. 冲突解决

冲突在设置页「同步冲突」列表中处理，卡片详情显示冲突标记。

- **用系统**：把当前系统字段写回 Anki，更新基线，标记已解决；保留 Anki 原文快照，供人工恢复。
- **用 Anki**：仅背面可用。把 Anki 内容回写系统，补齐媒体，更新基线，标记已解决；记录系统原值，支持撤销。
- **手动编辑**：打开卡片详情手动修改；之后回到冲突列表选择「用系统」，推送当前系统内容。
- 正面冲突不提供「用 Anki」，避免绕过去重和接管匹配。

`用 Anki` 的撤销会恢复系统原背面并重新打开冲突；`用系统` 不自动重开冲突，但保留 Anki 快照。

### 7. 媒体回写

从 Anki 回写背面时：

- 提取背面引用的媒体文件。
- 系统媒体目录缺失的，调用 `retrieveMediaFile` 拉取并保存。
- 已存在的同名文件不覆盖。
- 任一媒体拉取失败，不自动回写背面，记为冲突并在 `note` 中说明。

### 8. 检测时机

- 每次推送任务开始时，读取所有 `ready` 且已关联 note 的 Anki 笔记，按 500 条分批 `notesInfo`。
- 先判定并回写 Anki 侧修改，再推送系统侧修改，最后记录冲突。
- 另提供手动「从 Anki 拉取」，可处理任意状态的已关联卡片。
- 不做后台定时轮询。

### 9. 自动推送

- 保留抓取结束后的自动推送。
- 有冲突的卡片跳过，其余卡片照常推送和同步 AnkiWeb。
- 冲突不会阻塞整次推送。

### 10. 历史数据与上线

首次启用时没有字段级基线，按安全模式处理：

- 如果整卡 `pushed_hash` 与当前系统一致：说明系统没变，直接把 Anki 字段与系统字段比较。不同则视为 Anki 侧编辑，按矩阵处理。
- 如果 `pushed_hash` 与当前系统不一致：说明系统变过，无法还原上次推送内容。Anki 字段与当前系统相同则建立基线；不同则一律记为冲突，不自动覆盖。
- 首次上线可能出现一批待确认冲突，这是为了避免误覆盖。

## 后果

- 正面保持稳定，去重和接管匹配不受 Anki 编辑影响。
- Anki 背面修改不会在系统更新时被静默覆盖。
- 推送任务多一次 `notesInfo` 读取，冲突列表和卡片详情需要新增界面。
- 首次上线需要人工清理一批历史冲突。
- 冲突解决是显式操作，不再追求全自动合并。

## 备选方案

- **保持单向推送**：实现最省事，但用户修改仍可能被静默覆盖，不满足需求。
- **Anki 全量优先**：自动把所有 Anki 修改回写系统，正面也采纳；会破坏去重和接管匹配，风险高。
- **系统全量优先**：检测到 Anki 修改也直接覆盖；只是把静默覆盖变成有提示的覆盖，仍会丢内容。
- **自动三方合并**：需要完整保存每次推送的字段内容并实现文本合并，复杂度和误判都高，本期不做。

## 不在本期范围

- 自动采纳 Anki 正面修改。
- 三方文本自动合并。
- 拉取 Anki 标签或牌组变更。
- 后台定时轮询 Anki。
