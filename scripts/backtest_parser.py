# scripts/backtest_parser.py
import asyncio, discord, os, sys, json
from pathlib import Path
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / "config" / ".env", override=True)

from src.parser.signal_parser import parse_signal

LIMIT = 1000  # 每个频道抓多少条

# 读 channels.json
with open(ROOT / "config" / "channels.json") as f:
    CHANNELS = json.load(f)


class Crawler(discord.Client):
    async def on_ready(self):
        print(f"logged in as {self.user}\n")

        all_results = {}

        for ch_id_str, cfg in CHANNELS.items():
            if not cfg.get("enabled", True):
                print(f"⏭  跳过禁用频道 {cfg['name']}")
                continue

            ch_id = int(ch_id_str)
            ch_name = cfg["name"]
            trigger_users = set(cfg.get("trigger_user_ids", []))

            print(f"=== 抓取 #{ch_name} (id={ch_id}) ===")

            try:
                ch = self.get_channel(ch_id) or await self.fetch_channel(ch_id)
            except Exception as e:
                print(f"❌ 无法访问频道: {e}\n")
                continue

            total, by_user, parsed, missed = 0, 0, [], []

            async for msg in ch.history(limit=LIMIT):
                total += 1
                if trigger_users and msg.author.id not in trigger_users:
                    continue
                by_user += 1
                sigs = parse_signal(msg.content)
                if sigs:
                    parsed.append({
                        "ts": msg.created_at.isoformat(),
                        "author": str(msg.author),
                        "content": msg.content,
                        "signals": [
                            s.__dict__ if hasattr(s, "__dict__") else s
                            for s in sigs
                        ],
                    })
                else:
                    missed.append({
                        "ts": msg.created_at.isoformat(),
                        "author": str(msg.author),
                        "content": msg.content[:200],
                    })

            rate = len(parsed) / max(by_user, 1) * 100
            print(f"  总消息: {total}")
            print(f"  trigger 用户发的: {by_user}")
            print(f"  成功解析: {len(parsed)} ({rate:.1f}%)")
            print(f"  未解析: {len(missed)}\n")

            all_results[ch_name] = {
                "channel_id": ch_id,
                "total": total,
                "by_trigger_user": by_user,
                "parsed_count": len(parsed),
                "missed_count": len(missed),
                "parse_rate": round(rate, 1),
                "parsed": parsed,
                "missed": missed,
            }

        # 汇总
        out = ROOT / "data" / "parser_backtest.json"
        out.parent.mkdir(exist_ok=True)
        with open(out, "w", encoding="utf-8") as f:
            json.dump(all_results, f, ensure_ascii=False, indent=2)

        print(f"\n📊 全部完成，详情写入 {out}")
        print("\n=== 汇总 ===")
        for name, r in all_results.items():
            print(f"  {name:20s}  trigger={r['by_trigger_user']:4d}  "
                  f"parsed={r['parsed_count']:4d}  rate={r['parse_rate']:5.1f}%")

        await self.close()


Crawler().run(os.getenv("DISCORD_USER_TOKEN"))