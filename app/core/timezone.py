# -*- coding: utf-8 -*-
"""
统一时区 (v2.0.2)

本系统面向中国文旅业务，所有业务时间统一使用北京时间 (UTC+8, Asia/Shanghai)。

设计：
  - now(): 返回北京时间的 naive datetime（不带 tzinfo），用于数据库 DateTime 列的
    default/onupdate，以及"今日""近N天"等边界计算。
  - 采用 naive datetime 是为了与既有 DateTime 列（无时区）保持一致，避免 aware/naive
    混用报错；只要全链路都用 now()，存储与展示口径就一致（均为北京时间）。

  历史数据说明：升级前存量记录是按 UTC 存的，升级后新记录按北京时间存。二者会有
  8 小时口径差，但日志类数据会随时间滚动过期，短期内新数据即为正确的北京时间。
"""
import datetime

# 北京时间固定 UTC+8（中国不使用夏令时）
CST = datetime.timezone(datetime.timedelta(hours=8))


def now() -> datetime.datetime:
    """当前北京时间（naive，无 tzinfo）。用于 ORM 默认值与时间边界计算。"""
    return datetime.datetime.now(CST).replace(tzinfo=None)


def today_start() -> datetime.datetime:
    """今日 00:00:00（北京时间）。"""
    return now().replace(hour=0, minute=0, second=0, microsecond=0)


def days_ago(n: int) -> datetime.datetime:
    """n 天前的此刻（北京时间）。"""
    return now() - datetime.timedelta(days=n)
