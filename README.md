# 订阅聚合器

多源抓取 → 去重 → TCP 粗筛 → 输出高质量订阅。

**纯 Python 标准库实现，不需要 `pip install` 任何东西。**

> 本仓库自带 GitHub Actions（`.github/workflows/update.yml`），
> 每 6 小时自动跑一轮，结果自动提交回 `订阅/` 目录，手机随时可订阅。
> 订阅地址见下方「方式二」。

---

## 它解决什么问题

免费节点源的问题是：

1. 单个源随时失效
2. 一个源里 40% 是死节点
3. 节点质量参差不齐，没人帮你筛

这个工具把 8 个源合并起来，去掉重复的，剔除连 TCP 都连不上的死节点，
输出一份干净的订阅。客户端拿到后点一次「测延迟」，就能直接按速度排序用了。

---

## 两种用法

### 方式一：本地手动跑（最简单）

双击 **`运行.bat`**，等 20–30 秒，结果在 **`订阅/`** 目录。

命令行参数（可选）：

```bash
python aggregate.py                      # 默认：测活 + 输出 800 个
python aggregate.py --limit 300          # 只保留 300 个
python aggregate.py --pre-limit 3000     # 送测 3000 个（更准，也慢一些）
python aggregate.py --no-check           # 跳过测活，4 秒出结果
python aggregate.py --timeout 5          # 放宽 TCP 超时（网络差时用）
```

| 参数 | 默认 | 说明 |
|---|---|---|
| `--limit` | 800 | 最终保留节点数，太多了客户端会卡 |
| `--pre-limit` | 3000 | 测活前先截断到多少个，控制总耗时 |
| `--timeout` | 3.0 | TCP 超时秒数 |
| `--workers` | 64 | 并发数 |
| `--no-check` | 关 | 跳过 TCP 测活 |

### 方式二：GitHub Actions 云端自动跑（"源源不断"）

把这个目录推到你的 GitHub 仓库，`.github/workflows/update.yml` 会**每 6 小时**自动跑一次，
结果自动提交回仓库，手机随时能订阅。

推上去之后，你的订阅地址是：

```
Clash / FlClash :  https://raw.githubusercontent.com/你的用户名/你的仓库/main/订阅/merged.yaml
v2rayN / v2rayNG:  https://raw.githubusercontent.com/你的用户名/你的仓库/main/订阅/merged.txt
```

国内访问 raw.githubusercontent.com 可能慢，用 jsDelivr 版本：

```
https://cdn.jsdelivr.net/gh/你的用户名/你的仓库@main/订阅/merged.yaml
```

不想建仓库的话，也可以直接用别人已经跑好的（见 `sources.txt` 第 ⑥ 组）。

---

## 输出文件

| 文件 | 格式 | 给谁用 |
|---|---|---|
| `订阅/merged.txt` | Base64 | v2rayN、v2rayNG、FlClash（通用） |
| `订阅/merged.yaml` | Clash YAML | FlClash、Clash Verge、mihomo（支持规则分流） |
| `订阅/merged.urls` | 明文，每行一个 | 自己肉眼检查、或手动挑节点 |

YAML 已用 mihomo 核心校验过：`mihomo.exe -t -f merged.yaml` → `test is successful`。

---

## 修改源列表

编辑 **`sources.txt`**，一行一个地址，`#` 开头是注释。

**顺序即优先级** —— 靠前的源，其节点在截断时优先保留。所以把质量高的源放前面。

只支持 V2Ray 格式（base64 节点列表 / 每行一个链接）。
Clash YAML 格式的源会被跳过（不引入 PyYAML 依赖）。

---

## 为什么不做"真协议测速"

因为那需要 `sing-box` / `xray` 核心逐个起代理，很重（每个节点一个本地端口）。
客户端（v2rayN / FlClash）本身就内置了这个能力，而且用的是同一套核心，更准。

所以职责划分是：

- **本脚本**：广度覆盖 + 去死节点（快，24 秒搞定上万个候选）
- **客户端**：精确测延迟/测速 + 排序（准，点一下就行）

这样你不用在自己电脑上跑一个吃 CPU 的重型服务。

---

## 常见问题

**Q：跑出来几十个节点全是超时？**
A：先试 `--no-check` 看能抓到多少，然后 `--timeout 5` 放宽。还不行就是源的问题，
换 `sources.txt` 里的源。

**Q：客户端导入后卡死？**
A：节点太多。用 `--limit 300` 重跑。

**Q：YAML 导入 FlClash 报错？**
A：确认你用的是 `merged.yaml` 而不是 `merged.txt`。FlClash 两个都能吃，
但 `merged.txt` 没有规则分流。

**Q：订阅多久更新一次？**
A：本地跑的话，每次双击就是最新；GitHub Actions 是每 6 小时。
免费节点寿命普遍只有几小时到几天，建议至少每天跑一次。
