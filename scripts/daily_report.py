#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日美债 + 恒生科技晨报

数据源：
  - 美国财政部 官方 CSV（Daily Treasury Par Yield Curve Rates）
    10年期 / 2年期 / 30年期国债收益率，含跨年兜底
  - 美联储官网 RSS（press_all.xml / speeches.xml），政策动向，按发布日期过滤
  - 腾讯行情接口 qt.gtimg.cn（恒生科技指数 hkHSTECH）

送达：
  - QQ 邮箱 SMTP（smtp.qq.com:465, SSL）

环境变量（由 GitHub Secrets 注入）：
  MAIL_USER       发件邮箱，如 pklwj@qq.com
  MAIL_AUTH_CODE  QQ邮箱 SMTP 授权码（16位），非登录密码
  MAIL_TO         收件邮箱，默认同 MAIL_USER
  FED_NEWS_DAYS   美联储动向回溯天数，默认 7
"""

import os
import re
import sys
import smtplib
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from email.header import Header
from email.utils import formataddr
from xml.etree import ElementTree as ET

CST = timezone(timedelta(hours=8))
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"

MAIL_USER = os.environ.get("MAIL_USER", "")
MAIL_AUTH_CODE = os.environ.get("MAIL_AUTH_CODE", "")
MAIL_TO = os.environ.get("MAIL_TO", "") or MAIL_USER

# 美联储动向向前回溯的天数（美联储常连续多日无动态，窗口不宜过窄）
FED_NEWS_DAYS = int(os.environ.get("FED_NEWS_DAYS", "7"))


def fetch(url, headers=None, timeout=25):
    """带 UA 的 GET，返回文本；失败返回 None。"""
    h = {"User-Agent": UA}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
        return raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"[warn] fetch failed {url}: {e}", file=sys.stderr)
        return None


# ---------- 1. 美债：美国财政部官方收益率曲线 ----------

def _fetch_treasury_year(y):
    """取某一日历年的收益率曲线 CSV，返回 [(date_obj, {rate10y, rate2y, rate30y}), ...]。
    CSV 按日期倒序排列（最新在前）。
    """
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/"
        f"interest-rates/daily-treasury-rates.csv/{y}/all"
        f"?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv"
    )
    txt = fetch(url)
    if not txt or "10 Yr" not in txt:
        return []

    rows = [r for r in txt.splitlines() if r.strip()]
    if len(rows) < 2:
        return []

    header = [c.strip().strip('"') for c in rows[0].split(",")]
    try:
        idx_10y = header.index("10 Yr")
        idx_2y = header.index("2 Yr") if "2 Yr" in header else None
        idx_30y = header.index("30 Yr") if "30 Yr" in header else None
    except ValueError as e:
        print(f"[warn] treasury header parse {y}: {e}", file=sys.stderr)
        return []

    out = []
    for r in rows[1:]:
        c = [x.strip() for x in r.split(",")]
        if len(c) <= idx_10y:
            continue
        try:
            # 日期格式 M/D/YYYY
            d = datetime.strptime(c[0], "%m/%d/%Y").date()
            v10 = float(c[idx_10y])
        except ValueError:
            continue
        out.append((d, {
            "rate10y": v10,
            "rate2y": float(c[idx_2y]) if idx_2y is not None and c[idx_2y] else None,
            "rate30y": float(c[idx_30y]) if idx_30y is not None and c[idx_30y] else None,
        }))
    return out


def get_treasury():
    """取最近两个交易日的 10 年期国债收益率。

    跨年兜底：把「当年 + 上一年」两个 CSV 合并后统一按日期排序，
    避免每年 1 月初当年文件行数不足、算不出环比变化的问题。
    """
    year = datetime.now(CST).year
    merged = []
    for y in (year, year - 1):
        merged.extend(_fetch_treasury_year(y))

    if not merged:
        return None

    # 统一按日期倒序（最新在前）
    merged.sort(key=lambda x: x[0], reverse=True)

    cur_date, cur = merged[0]
    prev_date, prev = (merged[1] if len(merged) > 1 else (None, None))

    delta_bp = None
    if prev:
        delta_bp = round((cur["rate10y"] - prev["rate10y"]) * 100, 1)

    return {
        "date": cur_date.strftime("%Y-%m-%d"),
        "rate10y": cur["rate10y"],
        "rate2y": cur["rate2y"],
        "rate30y": cur["rate30y"],
        "prev_date": prev_date.strftime("%Y-%m-%d") if prev_date else "",
        "prev_rate10y": prev["rate10y"] if prev else None,
        "delta_bp": delta_bp,
    }


# ---------- 2. 美联储动向 ----------

def _parse_pubdate(raw):
    """解析 RSS pubDate。注意美联储 feed 的 pubDate 可能被 CDATA 包裹，
    形如 <![CDATA[Tue, 13 Oct 2026 18:00:00 GMT]]>，需先剥离。
    返回带时区的 datetime，失败返回 None。
    """
    if not raw:
        return None
    s = raw.strip()
    # 剥离 CDATA
    s = re.sub(r"^<!\[CDATA\[", "", s)
    s = re.sub(r"\]\]>$", "", s)
    s = s.strip()
    # 常见格式：Tue, 13 Oct 2026 18:00:00 GMT
    for fmt in ("%a, %d %b %Y %H:%M:%S %Z", "%a, %d %b %Y %H:%M:%S %z"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


def get_fed_news(total=6, days=None):
    """拉取美联储新闻稿 + 讲话，只保留最近 days 天内的条目。

    - days  : 回溯窗口（默认 FED_NEWS_DAYS）
    - total : 最终返回的条目总数上限（两个源合并后统一截断，
              避免某个源独占配额）
    按发布时间倒序排列。
    """
    if days is None:
        days = FED_NEWS_DAYS

    feeds = [
        ("新闻稿", "https://www.federalreserve.gov/feeds/press_all.xml"),
        ("讲话", "https://www.federalreserve.gov/feeds/speeches.xml"),
    ]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    pool = []

    for label, url in feeds:
        txt = fetch(url)
        if not txt:
            continue
        try:
            root = ET.fromstring(txt)
        except ET.ParseError:
            continue
        for it in root.iter():
            if it.tag.split("}")[-1] != "item":
                continue
            title = link = pubraw = ""
            for ch in it:
                ct = ch.tag.split("}")[-1]
                if ct == "title":
                    title = (ch.text or "").strip()
                elif ct == "link":
                    link = (ch.text or "").strip()
                elif ct == "pubDate":
                    pubraw = (ch.text or "").strip()
            if not title:
                continue
            pub = _parse_pubdate(pubraw)
            # 无日期的条目跳过（无法判断时效，宁缺毋滥）
            if pub is None or pub < cutoff:
                continue
            pool.append({
                "kind": label,
                "title": title[:100],
                "link": link,
                "dt": pub,
                "date": pub.astimezone(CST).strftime("%m-%d"),
            })

    # 合并后按时间倒序，统一截断
    pool.sort(key=lambda x: x["dt"], reverse=True)
    return pool[:total]


# ---------- 3. 恒生科技 ----------

def get_hstech():
    """腾讯行情接口取恒生科技指数最新点位与涨跌幅。
    返回值额外包含 quote_date（行情所属日期）与 is_today（是否为当日数据）。
    """
    txt = fetch("https://qt.gtimg.cn/q=hkHSTECH")
    if not txt:
        return None
    m = re.search(r'"(.*?)"', txt, re.S)
    if not m:
        return None
    parts = m.group(1).split("~")
    if len(parts) < 33:
        return None
    try:
        qtime = parts[30] if len(parts) > 30 else ""
        quote_date = ""
        dm = re.search(r"(\d{4}/\d{2}/\d{2})", qtime)
        if dm:
            quote_date = dm.group(1).replace("/", "-")
        today = datetime.now(CST).strftime("%Y-%m-%d")

        return {
            "name": "恒生科技指数",
            "price": float(parts[3]),
            "prev_close": float(parts[4]),
            "open": float(parts[5]),
            "change": float(parts[31]) if parts[31] else None,   # 涨跌额
            "pct": float(parts[32]) if parts[32] else None,       # 涨跌幅 %
            "high": float(parts[33]) if len(parts) > 33 and parts[33] else None,
            "low": float(parts[34]) if len(parts) > 34 and parts[34] else None,
            "time": qtime,
            "quote_date": quote_date,
            "is_today": bool(quote_date and quote_date == today),
        }
    except (ValueError, IndexError) as e:
        print(f"[warn] hstech parse: {e}", file=sys.stderr)
        return None


# ---------- 4. 拼装晨报 ----------

# ---------- 4. 一句话解读 ----------

def interpret(treasury, hk):
    """基于利率变动方向与幅度，生成当日传导提示。
    逻辑：美债利率下行通常利好港股估值（分母端），反之为压制。
    """
    if not treasury or treasury.get("delta_bp") is None:
        return "美债数据未更新，暂不做解读。"

    d = treasury["delta_bp"]
    if abs(d) < 1:
        return "10年期美债利率基本持平，对港股估值的边际扰动很小。"

    if d < 0:
        if abs(d) >= 10:
            return (f"10年期美债利率大幅下行 {abs(d):.0f} 个基点，"
                    "对港股分母端构成明显利好，科技股估值压力缓解；"
                    "但需留意是否由避险情绪驱动——若是，则分子端可能反向承压。")
        return (f"10年期美债利率下行 {abs(d):.0f} 个基点，"
                "对恒生科技属温和利好，估值约束边际放松。")
    else:
        if d >= 10:
            return (f"10年期美债利率大幅上行 {d:.0f} 个基点，"
                    "对港股分母端构成明显压制，高估值科技股首当其冲。")
        return (f"10年期美债利率上行 {d:.0f} 个基点，"
                "对恒生科技形成小幅压制，关注是否延续。")


def build_report():
    now = datetime.now(CST)
    treasury = get_treasury()
    fed = get_fed_news()
    hk = get_hstech()

    lines = []
    lines.append(f"【美债 · 恒生科技晨报】{now.strftime('%Y-%m-%d %H:%M')} 北京时间")
    lines.append("=" * 46)
    lines.append("")

    # 一、美债
    lines.append("一、美债")
    if treasury and treasury.get("rate10y") is not None:
        r10 = treasury["rate10y"]
        lines.append(f"  · 10年期国债收益率：{r10:.2f}%")
        if treasury.get("delta_bp") is not None:
            d = treasury["delta_bp"]
            arrow = "↓" if d <= 0 else "↑"
            word = "下行" if d < 0 else ("上行" if d > 0 else "持平")
            lines.append(
                f"    较前一交易日（{treasury['prev_date']} {treasury['prev_rate10y']:.2f}%）"
                f" {arrow} {word} {abs(d):.1f} 个基点"
            )
        extra = []
        if treasury.get("rate2y") is not None:
            extra.append(f"2年期 {treasury['rate2y']:.2f}%")
        if treasury.get("rate30y") is not None:
            extra.append(f"30年期 {treasury['rate30y']:.2f}%")
        if extra:
            lines.append("    其他期限：" + " / ".join(extra))
        if treasury.get("date"):
            lines.append(f"    数据日期：{treasury['date']}（美国财政部）")
    else:
        lines.append("  · 10年期收益率：暂未取到（源：美国财政部）")
    lines.append("")

    # 二、美联储动向
    lines.append(f"二、美联储动向（近 {FED_NEWS_DAYS} 日）")
    if fed:
        for f in fed:
            stamp = f"[{f['date']}] " if f.get("date") else ""
            lines.append(f"  · {stamp}[{f['kind']}] {f['title']}")
    else:
        lines.append(f"  · 近 {FED_NEWS_DAYS} 日无新公告/讲话")
    lines.append("")

    # 三、恒生科技
    lines.append("三、恒生科技指数")
    if hk:
        arrow = "▲" if (hk.get("pct") or 0) >= 0 else "▼"
        pct = hk.get("pct")
        # 标注数据所属日期：当日 / 上一交易日
        if hk.get("is_today"):
            tag = "当日收盘"
        else:
            tag = f"{hk.get('quote_date', '')} 收盘（上一交易日）"
        lines.append(f"  · 最新点位：{hk['price']:.2f}　（{tag}）")
        if pct is not None:
            lines.append(f"  · 涨跌幅：{arrow} {pct:+.2f}%")
        if hk.get("high") and hk.get("low"):
            lines.append(f"  · 日内区间：{hk['low']:.2f} ~ {hk['high']:.2f}")
    else:
        lines.append("  · 暂未取到行情")

    lines.append("")
    # 四、一句话解读
    lines.append("四、一句话解读")
    lines.append("  " + interpret(treasury, hk))
    lines.append("")

    lines.append("-" * 46)
    lines.append("数据来源：美国财政部 / 美联储官网 / 腾讯行情")
    lines.append("本邮件为自动生成的信息摘要，不构成投资建议。")

    return "\n".join(lines)


# ---------- 5. 发信 ----------

def send_mail(subject, body):
    if not MAIL_USER or not MAIL_AUTH_CODE:
        print("[error] 缺少 MAIL_USER 或 MAIL_AUTH_CODE", file=sys.stderr)
        return False
    to = MAIL_TO or MAIL_USER
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = formataddr((str(Header("每日晨报", "utf-8")), MAIL_USER))
    msg["To"] = to
    msg["Subject"] = Header(subject, "utf-8")

    try:
        with smtplib.SMTP_SSL("smtp.qq.com", 465, timeout=30) as s:
            s.login(MAIL_USER, MAIL_AUTH_CODE)
            s.sendmail(MAIL_USER, [to], msg.as_string())
        print(f"[ok] mail sent to {to}")
        return True
    except Exception as e:
        print(f"[error] send failed: {e}", file=sys.stderr)
        return False


def main():
    body = build_report()
    print("----- REPORT PREVIEW -----")
    print(body)
    print("--------------------------")
    now = datetime.now(CST)
    ok = send_mail(f"美债·恒科晨报 {now.strftime('%Y-%m-%d')}", body)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
