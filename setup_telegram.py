# -*- coding: utf-8 -*-
"""
Descobre o TELEGRAM_CHAT_ID e manda uma mensagem de teste.

    1. No Telegram, fale com @BotFather -> /newbot -> copie o token
    2. Abra a conversa do SEU bot e mande qualquer mensagem (ex.: "oi")
    3. python setup_telegram.py <token>
"""
import json
import sys
import urllib.parse
import urllib.request


def api(token: str, metodo: str, **params):
    url = f"https://api.telegram.org/bot{token}/{metodo}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read())


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    token = sys.argv[1].strip()

    eu = api(token, "getMe")
    if not eu.get("ok"):
        print(f"Token invalido: {eu}")
        return 1
    print(f"Bot: @{eu['result']['username']}")

    updates = api(token, "getUpdates")
    chats = {}
    for u in updates.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            chats[chat["id"]] = chat.get("title") or chat.get("first_name") or chat.get("username")

    if not chats:
        print("\nNenhuma mensagem encontrada.")
        print(f"Abra https://t.me/{eu['result']['username']}, mande 'oi' e rode de novo.")
        return 1

    print("\nChats encontrados:")
    for cid, nome in chats.items():
        print(f"  TELEGRAM_CHAT_ID = {cid}   ({nome})")

    cid = next(iter(chats))
    envio = api(token, "sendMessage", chat_id=cid,
                text="Monitor de voos FLN <-> Rio conectado. Te aviso quando a passagem cair.")
    print("\nMensagem de teste enviada." if envio.get("ok") else f"\nFalhou: {envio}")

    print("\nAgora cadastre no GitHub:")
    print("  Settings > Secrets and variables > Actions > New repository secret")
    print(f"    TELEGRAM_BOT_TOKEN = {token}")
    print(f"    TELEGRAM_CHAT_ID   = {cid}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
