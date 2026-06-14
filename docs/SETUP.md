# Setup Guide

## 1. Environment

    python -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt

## 2. Config

    cp .env.example config/.env
    # edit config/.env with your tokens

### Discord User Token
1. Browser login to Discord
2. F12 -> Network -> any request -> Headers -> Authorization
3. WARNING: token equals your password. Leaking = account stolen.

### Telegram Bot
1. Chat @BotFather -> /newbot
2. Get BOT_TOKEN
3. Send a message to your bot, then visit:
   https://api.telegram.org/bot<TOKEN>/getUpdates
   to find your chat_id

### moomoo OpenD
1. Download from https://www.moomoo.com/download/OpenAPI
2. Launch and login. Confirm 127.0.0.1:11111
3. Unlock trade password

## 3. Test

    python scripts/test_parser.py
    pytest tests/
    python -m src.main

## 4. Enable real orders

Edit src/broker/moomoo_client.py, uncomment the "Real order" block.
USE SIMULATE FOR 1-2 WEEKS FIRST!

## 5. Daily report

    python scripts/generate_report.py
