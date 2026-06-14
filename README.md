# Discord Copy Trade Bot

自动监听 Discord 信号 -> 解析 -> moomoo 下单 -> Telegram 通知 -> 复盘报告

## 快速开始

    pip install -r requirements.txt
    cp .env.example config/.env
    python -m src.main

## 模块说明
- listener/  : Discord 监听 (self-bot)
- parser/    : 信号解析
- broker/    : moomoo 下单
- notifier/  : Telegram 通知
- storage/   : 数据持久化
- utils/     : 日志/工具

## 风险声明
期权风险极高, 请先用模拟盘测试
