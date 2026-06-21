# 明日方舟基建运转大模拟 (arknights_base_simulation)

输入**干员练度表**、**每日上线时刻**、**素材理智价值**, 搜索**最大日均理智收益**的循环生产方案。

```
干员练度表.xlsx ─┐
每日上线时刻   ─┼─►  优化器  ─►  最优布局 + 各设施排班 + 日均理智收益
config.json    ─┘
```

### 如何开始使用

> 我想基于我的干员列表、物品价值评估和每日上线时间来获得最佳排班表，我该准备什么？

#### 导出你的数据

1. **干员练度表**: 前往 [一图流 · 干员练度](https://ark.yituliu.cn/survey/operators), 登录后点击「导出」下载 `干员练度表.xlsx`, 放到 `data/` 目录下
2. **物品价值表** (可选): 前往 [一图流 · 物品价值](https://ark.yituliu.cn/material/value), 导出 `物品价值表.xlsx`, 通过 `--values-xlsx` 参数载入以覆盖默认估值
3. **上线时刻**: 确定你每天上线换班的时间(24h制), 用 `--logins` 传入

```bash
# 示例: 243 布局, 每天 0:00/13:00/16:30 上线换班, 14天模拟
python -m arknights_base_simulation --logins 0,13,16.5 --layout 4,2,3 --n-shifts 3 --days 14

# 用自定义物品价值
python -m arknights_base_simulation --logins 8,22 --values-xlsx 物品价值表.xlsx

# 锁定特定干员到指定设施
python -m arknights_base_simulation --logins 8,22 --lock control:令,夕 --lock trading:但书
```

#### 导入排班方案到 MAA

> 我希望让牛牛代班我的排班！怎么导出给牛牛？

1. 运行优化器并导出 MAA 排班表:
   ```bash
   python -m arknights_base_simulation --logins 8,22 --export-maa maa_plan.json
   ```
2. 将生成的 `maa_plan.json` 复制到 MAA 安装目录下的 `resource/custom_infrast/`
3. 在 MAA 中打开 **基建换班** 任务, 选择 **自定义基建换班** 模式, 即可使用导出的排班方案

> 注: `--export-maa` 目前仅支持单班次模式。多班次(`--n-shifts`)的排班需手动导入或参考输出自行编排。

<details>
<summary>全部命令行参数</summary>

| 参数 | 说明 |
|------|------|
| `xlsx` (位置参数) | 干员练度表路径, 默认 `data/干员练度表.xlsx` |
| `--logins H1,H2,...` | 每日上线时刻(24h制, 1~4个) |
| `--config PATH` | config.json 路径 |
| `--values-xlsx PATH` | 物品价值表.xlsx, 覆盖 config 中的 material_values_ap |
| `--layout M,T,P` | 指定布局(制造,贸易,发电), 如 `4,2,3`; 不指定则枚举取最优 |
| `--lock ROOM:OP,...` | 锁定干员到设施, 可多次使用 |
| `--n-shifts N` | 自动生成 N 组轮换排班(2~4) |
| `--shifts PATH` | 手写多班次排班 JSON, 跳过优化器 |
| `--days N` | 逐日瞬态模拟天数 |
| `--initial-mood M` | 瞬态模拟初始心情(默认错峰) |
| `--export-maa PATH` | 导出 MAA 排班表 JSON |
| `--no-local-search` | 关闭局部搜索(更快, 质量略低) |
| `--json` | 输出 JSON |

</details>

## 输入

1. **练度表 xlsx** — 从一图流导出。精英化等级决定解锁哪些基建技能。
2. **`--logins`** — 每日上线时刻(24h制)。两次上线之间干员不可换班, 影响产能溢出和心情损耗。
3. **`config.json`** — 素材理智价值(`material_values_ap`) + 各设施常量。所有数值可改。

## 优化器

**单班次** (默认): 枚举布局 → 贪心排班 → 协同种子(感知链等跨设施组合) → 局部搜索 + 跨设施交换 → 生产线优化。~1-3s。

**多班次** (`--n-shifts 2~4`): 逐班次独立构建, 按 gap 时长排序(最长→最短→中间)确保可持续工休比。007 永驻干员(但書/龙舌兰)在菲亚梅塔可用时全班次工作, 靠 mood swap 维持心情。~30-70s。

## 瞬态模拟

`--days N` 按真实时间轴逐 gap 推进心情, 跟踪占空循环(满心情上岗→耗尽下班→宿舍恢复→再上岗)。输出每日产出曲线 + 可持续日产。默认**错峰**初始心情; `--initial-mood M` 可看新部署爬坡。

## 动态联动

- **条件配对**: 「当与X在同一设施」满足才计, 避免无条件高估
- **派系计数**: 按 `factions.json` 真实人数缩放
- **抱团资源**: 人间烟火、感知信息→思维链环、无声共鸣、热情值等跨设施转化链
- **协同种子**: 优化器自动识别资源池链(如感知链: 令+夕+迷迭香+至简+车尔尼+爱丽丝+黑键), 整组预置后贪心填充

<details>
<summary>特殊贸易干员</summary>

按 PRTS 官方技能文本实装, 3级站赤金订单概率取长期期望:

- **但书·违约单**: 原生<4赤金单 +2 赤金交付, 报酬同步 +500/赤金
- **龙舌兰·投资**: 原生≥4赤金单 +500 龙门币, 不额外耗赤金。与但书互斥(同站龙舌兰失效)
- **巫恋·低语**: 同站他人效率归零, 每人 +45%; 解放席位给龙舌兰/柏喙
- **高品质订单**: α/β 提质按 PRTS 概率表折期望, 联动龙舌兰/但书概率
- **007 永驻**: 但書+龙舌兰全班次不下班, 菲亚梅塔宿舍 swap 维持(2/h > 2×0.75/h)

</details>

<details>
<summary>游戏机制常量 (config.json)</summary>

产能/心情/电力等常量按 [PRTS wiki](https://prts.wiki) 校准:

- 制造站: 库存体积54, 赤金72分/件, 作战记录180分/件
- 贸易站: 3级普通赤金订单 2/3/4赤金 = 30%/50%/20%, 期望2.9赤金/1450龙门币/203.4分
- 心情: 上限24, 工作基础消耗1.0/h, 同房间人数减免; 宿舍 L5 满氛围 4.0/h 恢复
- 赤金是中间产物, 未卖出默认不计收益(`unsold_gold_value_factor=0`)

</details>

<details>
<summary>已知简化</summary>

- 心情阈值型(夕/令)稳态默认高心情分支, 瞬态按实际心情分支
- 会客室/办公室/加工站/训练室按保守估值
- 赤金订单按 PRTS 概率取期望, 未逐笔随机模拟
- 无人机按边际收益分配, 不被生产力%二次放大

</details>

## 模块

```
src/arknights_base_simulation/
  cli.py        命令行 / MAA导出
  roster.py     练度表解析
  skills.py     技能库 + 解锁/互斥组
  effects.py    技能描述 → 结构化效果
  synergy.py    动态联动(派系/抱团资源/条件配对)
  engine.py     模拟引擎(产能/心情/赤金链/溢出/电力)
  simulate.py   逐日瞬态模拟(占空循环/菲亚梅塔swap)
  optimizer.py  布局枚举 + 排班优化 + 007永驻 + 多班次轮换
  valuetable.py 物品价值表解析
data/             技能/数值/派系/参考配置
config.json       素材理智价值 + 机制常量
tests/            冒烟测试
```

## 致谢

- [逻辑元](https://space.bilibili.com/688411531) — 基建一图流排班表, 本项目的优化目标基准
- [公孙长乐](https://space.bilibili.com/22606843) — 基建机制解析与数据整理
- [一图流](https://ark.yituliu.cn) — 干员练度表 / 物品价值表 / 基建技能数据
- [PRTS wiki](https://prts.wiki) — 游戏机制常量校准
