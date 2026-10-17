# 美债 · 恒生科技 每日晨报

每个工作日（北京时间 08:30 左右）自动向 `pklwj@qq.com` 发送一封晨报，内容包括：

1. **美债**：10 年期国债收益率 + 较前一交易日变化（单位：基点），附 2 年期 / 30 年期
2. **美联储动向**：近 7 日新闻稿与官员讲话标题（按发布日期过滤，倒序）
3. **恒生科技指数**：最新点位、涨跌幅、日内区间
4. **一句话解读**：美债利率变动对港股估值的传导提示

## 运行方式

由 GitHub Actions 定时触发（`.github/workflows/daily.yml`），**本机是否开机完全无关**。

- 定时：`cron: "17 0 * * *"`（UTC 00:17 = 北京时间 08:17）
- 注意：GitHub 的 `schedule` 是 best-effort，实测常延迟数小时甚至整天不派发；
  需要准点请配外部定时器调用 `workflow_dispatch` API（见下）
- 支持手动触发：仓库 → Actions → 每日美债恒科晨报 → Run workflow

## 数据源

| 内容 | 来源 | 说明 |
|---|---|---|
| 国债收益率曲线 | 美国财政部 (home.treasury.gov) | 官方 Daily Treasury Par Yield Curve Rates CSV |
| 美联储新闻/讲话 | 美联储官网 RSS | press_all.xml / speeches.xml |
| 恒生科技指数 | 腾讯行情接口 | qt.gtimg.cn，`hkHSTECH` |

**取数细节**
- 美债：合并「当年 + 上一年」两年 CSV 后按日期排序，避免每年 1 月初当年文件行数不足而算不出环比。
- 美联储：解析 `pubDate`（含 CDATA 剥离），默认只保留近 7 日条目，按时间倒序取前 6 条。
- 恒科：自动判断行情是否为当日；非当日会标注「上一交易日」，避免误读为实时价。

## 必需的仓库 Secret

在 `Settings → Secrets and variables → Actions` 中添加：

| Secret 名 | 值 | 说明 |
|---|---|---|
| `MAIL_USER` | `pklwj@qq.com` | 发件邮箱 |
| `MAIL_AUTH_CODE` | QQ 邮箱 SMTP 授权码 | **不是登录密码**，在 QQ 邮箱 设置→账户 中开启 SMTP 后获取 |
| `MAIL_TO` | `pklwj@qq.com` | 收件邮箱（可选，默认同 MAIL_USER） |

> 授权码只以 Secret 形式注入，不会出现在代码或日志中。

**可选的环境变量**（加在 Secrets 或 workflow 的 `env` 中）：

| 变量 | 默认 | 说明 |
|---|---|---|
| `FED_NEWS_DAYS` | `7` | 美联储动向回溯天数 |

## 本地测试

```bash
export MAIL_USER="pklwj@qq.com"
export MAIL_AUTH_CODE="你的授权码"
python3 scripts/daily_report.py
```

不设环境变量时，脚本仍会打印晨报预览（仅跳过发信步骤）。

## 免责声明

本晨报为公开数据自动汇总，不构成任何投资建议。
