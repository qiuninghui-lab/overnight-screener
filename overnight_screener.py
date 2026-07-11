#!/usr/bin/env python3
"""
一夜持股法 — 云端定时股筛脚本
================================
不依赖 WorkBuddy，独立运行。在云服务器上定时执行（14:30），自动完成：
  1. 从东方财富获取 A 股实时行情
  2. 按 8 条一夜持股法条件筛选（条件 2-5）
  3. 综合评分 + 成交量温和放大验证（条件 6）
  4. 精选 Top3 推荐
  5. 通过 QQ 邮箱 SMTP 发送报告

部署方式：PythonAnywhere / Render / 任意支持 cron 的云服务器
依赖：pip install akshare
"""

import os
import sys
import json
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from typing import Optional

import akshare as ak
import pandas as pd

# ============================================================
# 配置（环境变量）
# ============================================================
QQ_SMTP_USER = os.getenv("QQ_SMTP_USER", "qiuninghui@qq.com")  # 发件邮箱
QQ_SMTP_PASS = os.getenv("QQ_SMTP_PASS", "")                    # QQ邮箱授权码
TO_EMAIL     = os.getenv("TO_EMAIL", "1204529407@qq.com")       # 收件邮箱

SMTP_HOST = "smtp.qq.com"
SMTP_PORT = 465

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


# ============================================================
# Step 1: 获取 A 股实时行情
# ============================================================
def fetch_spot_data() -> pd.DataFrame:
    """从东方财富获取 A 股全市场实时行情"""
    log.info("正在获取 A 股实时行情...")
    try:
        df = ak.stock_zh_a_spot_em()
        log.info(f"获取到 {len(df)} 只股票")
        return df
    except Exception as e:
        log.error(f"获取行情失败: {e}")
        raise


# ============================================================
# Step 2: 按一夜持股法条件 2-5 粗筛
# ============================================================
def filter_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    条件 2: 涨幅 3%~5%
    条件 3: 量比 > 1
    条件 4: 换手率 5%~10%
    条件 5: 流通市值 30亿~150亿
    排除: ST / *ST / 退市 / N(新股首日) / C(次新股前5日)
    """
    log.info("开始粗筛（条件 2-5）...")

    # 列名映射（AKShare 中文字段名）
    col_map = {
        "代码": "code",
        "名称": "name",
        "最新价": "price",
        "涨跌幅": "chg_pct",
        "换手率": "turnover",
        "量比": "vol_ratio",
        "流通市值": "float_mv",
        "成交量": "volume",
        "成交额": "amount",
    }

    df = df.rename(columns=col_map)
    required = ["code", "name", "price", "chg_pct", "turnover", "vol_ratio", "float_mv"]
    for col in required:
        if col not in df.columns:
            log.error(f"缺少必要字段: {col}，可用字段: {df.columns.tolist()}")
            return pd.DataFrame()

    # 转为数值
    for col in ["chg_pct", "turnover", "vol_ratio", "float_mv", "price"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # 排除 ST / *ST / 退市 / N / C
    bad_patterns = ["ST", "*ST", "退市", "N ", "C "]
    mask_valid = ~df["name"].apply(
        lambda n: any(p in str(n) for p in bad_patterns) if pd.notna(n) else True
    )

    # 条件 2: 涨幅 3%~5%
    mask_chg = (df["chg_pct"] >= 3.0) & (df["chg_pct"] <= 5.0)
    # 条件 3: 量比 > 1
    mask_vr  = df["vol_ratio"] > 1.0
    # 条件 4: 换手率 5%~10%
    mask_to  = (df["turnover"] >= 5.0) & (df["turnover"] <= 10.0)
    # 条件 5: 流通市值 30亿~150亿（float_mv 单位是元）
    mask_mv  = (df["float_mv"] >= 3_000_000_000) & (df["float_mv"] <= 15_000_000_000)

    result = df[mask_valid & mask_chg & mask_vr & mask_to & mask_mv].copy()
    result["float_mv_yi"] = (result["float_mv"] / 1e8).round(2)

    log.info(f"粗筛后: {len(result)} 只候选")
    return result


# ============================================================
# Step 3: 综合评分
# ============================================================
def score_candidates(df: pd.DataFrame) -> pd.DataFrame:
    """
    评分体系（满分 100）：
    - 涨幅分 (25): 距 4.0% 越近越好
    - 量比分 (25): 越高越好
    - 换手率分 (25): 距 7.0% 越近越好
    - 市值分 (25): 距 70亿 越近越好
    """
    if df.empty:
        return df

    df = df.copy()
    df["score_chg"] = (100 - abs(df["chg_pct"] - 4.0) * 40).clip(0, 100)
    df["score_vr"]  = (df["vol_ratio"] * 30).clip(0, 100)
    df["score_to"]  = (100 - abs(df["turnover"] - 7.0) * 20).clip(0, 100)
    df["score_mv"]  = (100 - abs(df["float_mv_yi"] - 70) * 1.2).clip(0, 100)
    df["score_total"] = (
        df["score_chg"] + df["score_vr"] + df["score_to"] + df["score_mv"]
    ) / 4

    return df.sort_values("score_total", ascending=False)


# ============================================================
# Step 4: K 线成交量温和放大验证（条件 6）
# ============================================================
def check_volume_trend(code: str) -> tuple[str, list]:
    """
    获取最近 5 日 K 线，检查近 3 日成交量是否阶梯式递增。
    返回: ("✅"|"⚠️"|"❌", [近3日成交量])
    """
    try:
        kline = ak.stock_zh_a_hist(symbol=code, period="daily", adjust="qfq")
        if len(kline) < 3:
            return ("❌", [])

        recent = kline.tail(3)
        vols = recent["成交量"].tolist()

        if len(vols) < 3:
            return ("❌", vols)

        v1, v2, v3 = vols[0], vols[1], vols[2]

        if v3 > v2 > v1:
            growth_2 = (v3 - v2) / v2 if v2 > 0 else 999
            growth_1 = (v2 - v1) / v1 if v1 > 0 else 999
            if growth_2 < 0.8 and growth_1 < 0.8:
                return ("✅", vols)   # 严格递增，增幅温和
            else:
                return ("⚠️", vols)   # 递增但增幅偏大
        elif v3 > v2:
            return ("⚠️", vols)       # 仅近 2 日递增
        else:
            return ("❌", vols)       # 不递增

    except Exception as e:
        log.warning(f"获取 {code} K线失败: {e}")
        return ("❌", [])


def select_top3(df: pd.DataFrame, max_check: int = 8) -> pd.DataFrame:
    """从评分前 N 名中选最优 3 只（含成交量验证）"""
    if df.empty:
        return df

    top_n = df.head(max_check).copy()
    vol_results = []

    for _, row in top_n.iterrows():
        code = str(row["code"])
        status, vols = check_volume_trend(code)
        vol_results.append({
            "code": code,
            "vol_status": status,
            "vol_last3": vols,
        })

    vol_df = pd.DataFrame(vol_results)
    top_n = top_n.merge(vol_df, on="code", how="left")

    # 按 vol_status 排序（✅ > ⚠️ > ❌），同等级按评分排
    status_order = {"✅": 0, "⚠️": 1, "❌": 2}
    top_n["vol_rank"] = top_n["vol_status"].map(status_order).fillna(2)
    top_n = top_n.sort_values(["vol_rank", "score_total"], ascending=[True, False])

    return top_n.head(3)


# ============================================================
# Step 5: 生成报告
# ============================================================
def generate_report(top3: pd.DataFrame, total_candidates: int, today: str) -> str:
    """生成 Markdown 报告"""
    lines = [
        f"# 一夜持股法 — 精选推荐",
        f"",
        f"> 数据日期：{today}  |  粗筛候选：{total_candidates} 只  |  精选：{len(top3)} 只",
        f"",
        "---",
        "",
    ]

    if top3.empty:
        lines.append("## 今日无精选标的，按纪律空仓")
        return "\n".join(lines)

    medals = ["🥇", "🥈", "🥉"]
    for i, (_, row) in enumerate(top3.iterrows()):
        lines.append(f"## {medals[i]} {row['code']} {row['name']}")
        lines.append("")
        lines.append(f"| 指标 | 数值 |")
        lines.append(f"|------|------|")
        lines.append(f"| 涨幅 | {row['chg_pct']:.2f}% |")
        lines.append(f"| 量比 | {row['vol_ratio']:.2f} |")
        lines.append(f"| 换手率 | {row['turnover']:.2f}% |")
        lines.append(f"| 流通市值 | {row['float_mv_yi']:.2f} 亿 |")
        lines.append(f"| 最新价 | {row['price']:.2f} 元 |")
        lines.append(f"| 综合评分 | {row['score_total']:.1f}/100 |")
        lines.append(f"| 成交量温和放大 | {row.get('vol_status', '❌')} |")
        lines.append("")

    lines.extend([
        "---",
        "## 待人工核验（14:45 前）",
        "- **条件 7**: 分时图强于大盘（叠加大盘分时对比）",
        "- **条件 8**: 尾盘创当日新高，回踩不破分时均线",
        "",
        "## 次日离场纪律",
        "1. 隔夜即一夜：次日开盘 30 分钟内了结",
        "2. 红了别贪：高开 3%+ 分批止盈",
        "3. 绿了别扛：低开破昨收立即止损",
        "4. 不补仓：超短线不越跌越买",
        "",
        "⚠️ 自动化粗筛 + 评分，不构成投资建议。条件 7-8 须人工确认。",
    ])

    return "\n".join(lines)


# ============================================================
# Step 6: 发邮件
# ============================================================
def send_email(subject: str, html_body: str) -> bool:
    """通过 QQ SMTP 发送邮件"""
    if not QQ_SMTP_PASS:
        log.error("未设置 QQ_SMTP_PASS 环境变量，跳过发邮件")
        return False

    msg = MIMEMultipart("alternative")
    msg["From"]    = QQ_SMTP_USER
    msg["To"]      = TO_EMAIL
    msg["Subject"] = subject
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.login(QQ_SMTP_USER, QQ_SMTP_PASS)
            server.sendmail(QQ_SMTP_USER, [TO_EMAIL], msg.as_string())
        log.info(f"邮件已发送至 {TO_EMAIL}")
        return True
    except Exception as e:
        log.error(f"邮件发送失败: {e}")
        return False


def report_to_html(md_report: str, top3: pd.DataFrame, today: str) -> str:
    """将精选结果转为简洁 HTML 邮件正文"""
    rows_html = ""
    medals = ["🥇", "🥈", "🥉"]
    for i, (_, row) in enumerate(top3.iterrows()):
        vol_icon = row.get("vol_status", "❌")
        rows_html += f"""
        <tr>
            <td>{medals[i]}</td>
            <td><strong>{row['code']}</strong></td>
            <td>{row['name']}</td>
            <td>{row['chg_pct']:.2f}%</td>
            <td>{row['vol_ratio']:.2f}</td>
            <td>{row['turnover']:.2f}%</td>
            <td>{row['float_mv_yi']:.2f}亿</td>
            <td>{row['score_total']:.1f}</td>
            <td>{vol_icon}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:600px;margin:0 auto;padding:20px;">
<h1 style="color:#d4380d;">一夜持股法 — 精选推荐</h1>
<p><strong>{today} 交易日</strong> | 精选 {len(top3)} 只</p>
<hr>
<table border="1" cellpadding="8" cellspacing="0" style="border-collapse:collapse;width:100%;font-size:14px;">
<tr style="background:#1a1a1a;color:#fff;">
    <th>排名</th><th>代码</th><th>名称</th><th>涨幅</th><th>量比</th><th>换手</th><th>市值</th><th>评分</th><th>量能</th>
</tr>
{rows_html}
</table>
<hr>
<p style="color:#d4380d;"><strong>⚠️ 14:45 前人工核验条件 7-8（分时强于大盘 + 尾盘新高回踩），确认后方可下单。</strong></p>
<p style="color:#999;font-size:12px;">纪律：隔夜即一夜，次日开盘30分钟内了结 | 红了别贪 绿了别扛 不补仓</p>
</body></html>"""

    return html


# ============================================================
# Main
# ============================================================
def main():
    today = datetime.now().strftime("%Y-%m-%d")
    weekday = datetime.now().weekday()

    # 非交易日跳过
    if weekday >= 5:
        log.info(f"今日 {today} 是周末，跳过")
        return

    log.info(f"=== 一夜持股法云端股筛启动 {today} ===")

    try:
        # 1. 获取行情
        df = fetch_spot_data()

        # 2. 粗筛（条件 2-5）
        candidates = filter_candidates(df)
        total = len(candidates)

        if total == 0:
            log.info("无符合条件标的，按纪律空仓")
            html = f"<h2>一夜持股法 — {today}</h2><p>今日无符合粗筛条件的标的，按纪律空仓。</p>"
            send_email(f"一夜持股法 - {today}（空仓）", html)
            return

        # 3. 综合评分
        candidates = score_candidates(candidates)

        # 4. 成交量验证 + 精选 Top3
        top3 = select_top3(candidates)
        log.info(f"精选 Top3: {top3[['code','name','score_total']].to_string()}")

        # 5. 生成报告
        md_report = generate_report(top3, total, today)

        # 6. 发邮件
        html_body = report_to_html(md_report, top3, today)
        send_email(f"一夜持股法精选 - {today}（{len(top3)}只候选）", html_body)

        log.info("=== 完成 ===")

    except Exception as e:
        log.error(f"执行异常: {e}", exc_info=True)
        send_email(
            f"一夜持股法 - {today}（执行异常）",
            f"<p>脚本运行出错：</p><pre>{e}</pre>",
        )


if __name__ == "__main__":
    main()
