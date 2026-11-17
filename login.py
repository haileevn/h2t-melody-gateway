"""Interactive login on a trusted computer. Session file persists in TELEGRAM_SESSION."""
import asyncio
import os
from telethon import TelegramClient

async def main():
    session = os.environ.get('TELEGRAM_SESSION', '/data/teletune')
    async with TelegramClient(session, int(os.environ['TELEGRAM_API_ID']), os.environ['TELEGRAM_API_HASH']) as client:
        await client.start()
        me = await client.get_me()
        print(f'Logged in to Telegram user id {me.id}. Keep the .session file private.')

if __name__ == '__main__':
    asyncio.run(main())
