# -*- coding: utf-8 -*-
"""
Monitor de passagens Florianopolis <-> Rio de Janeiro.

Consulta duas fontes (Google Flights e Vai de Promo — ver fontes.py), compara
o piso das duas e avisa no Telegram quando entra na faixa de preco desejada.

Uso:
    python monitor.py            # so avisa se houver promocao
    python monitor.py --force    # sempre manda mensagem
    python monitor.py --resumo   # resumo diario com a tendencia
    python monitor.py --dry-run  # nao envia nada, so imprime
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fontes import GOOGLE, VAIDEPROMO, Oferta, Resultado, buscar_google, buscar_vaidepromo

RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / "state"
HISTORICO = ESTADO / "historico.jsonl"
ULTIMO_ALERTA = ESTADO / "ultimo_alerta.json"
BRT = timezone(timedelta(hours=-3))

# O console do Windows abre em cp1252 e engasga nos acentos/emoji das mensagens.
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

NOMES = {
    "FLN": "Florianópolis",
    "RIO": "Rio (todos os aeroportos)",
    "GIG": "Rio / Galeão",
    "SDU": "Rio / Santos Dumont",
}


def carregar_env() -> None:
    """Le o .env em execucao local. No GitHub Actions as vars ja vem do ambiente."""
    arquivo = RAIZ / ".env"
    if not arquivo.exists():
        return
    for linha in arquivo.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip())


def agora() -> datetime:
    return datetime.now(BRT)


def log(msg: str) -> None:
    print(f"[{agora():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Classificacao e decisao de alerta
# --------------------------------------------------------------------------- #

def classificar(preco: int, alvos: dict) -> str:
    if preco < alvos["jackpot"]:
        return "jackpot"
    if preco < alvos["alerta"]:
        return "alerta"
    if preco < alvos["aviso"]:
        return "aviso"
    return "acima"


CABECALHO = {
    "jackpot": "🔥🔥 ACHEI ABAIXO DE R$ {jackpot} — COMPRA AGORA",
    "alerta": "🔥 Faixa R$ {jackpot}–{alerta} — vale muito a pena",
    "aviso": "⚠️ Abaixo de R$ {aviso}, mas acima de R$ {alerta}",
    "acima": "📊 Nada abaixo de R$ {aviso} ainda",
}


def carregar_ultimo_alerta() -> dict:
    if ULTIMO_ALERTA.exists():
        try:
            return json.loads(ULTIMO_ALERTA.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}


def deve_alertar(piso: int, faixa: str, cfg: dict) -> tuple[bool, str]:
    """Evita repetir a mesma mensagem toda madrugada."""
    if faixa == "acima":
        return False, "acima do alvo"

    ultimo = carregar_ultimo_alerta()
    preco_ant = ultimo.get("preco")
    quando = ultimo.get("quando")

    if preco_ant is None:
        return True, "primeiro alerta"
    if piso < preco_ant:
        return True, f"baixou de R$ {preco_ant} para R$ {piso}"
    if quando:
        try:
            horas = (agora() - datetime.fromisoformat(quando)).total_seconds() / 3600
        except ValueError:
            return True, "estado anterior ilegível"
        if horas >= cfg["antispam_horas"]:
            return True, f"lembrete ({horas:.0f}h desde o último aviso)"
    return False, f"já avisei R$ {preco_ant} há pouco"


def gravar_ultimo_alerta(piso: int, faixa: str) -> None:
    ULTIMO_ALERTA.write_text(
        json.dumps({"preco": piso, "faixa": faixa, "quando": agora().isoformat()},
                   indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def gravar_historico(resultados: list[Resultado]) -> None:
    ESTADO.mkdir(exist_ok=True)
    pisos: dict[str, dict[str, int | None]] = {}
    erros: dict[str, str] = {}
    for r in resultados:
        pisos.setdefault(r.fonte, {})[r.destino] = r.piso
        if r.erro:
            erros[f"{r.fonte}/{r.destino}"] = r.erro
    with HISTORICO.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"quando": agora().isoformat(timespec="minutes"), "pisos": pisos, "erros": erros},
            ensure_ascii=False,
        ) + "\n")


def ler_historico(dias: int = 30) -> list[dict]:
    if not HISTORICO.exists():
        return []
    corte = agora() - timedelta(days=dias)
    saida = []
    for linha in HISTORICO.read_text(encoding="utf-8").splitlines():
        if not linha.strip():
            continue
        try:
            reg = json.loads(linha)
            if datetime.fromisoformat(reg["quando"]) >= corte:
                saida.append(reg)
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            continue
    return saida


def pisos_validos(registros: list[dict]) -> list[int]:
    """Aceita o formato antigo ({destino: piso}) e o novo ({fonte: {destino: piso}})."""
    saida = []
    for reg in registros:
        for valor in (reg.get("pisos") or {}).values():
            if isinstance(valor, int):
                saida.append(valor)
            elif isinstance(valor, dict):
                saida.extend(p for p in valor.values() if isinstance(p, int))
    return saida


# --------------------------------------------------------------------------- #
# Mensagem
# --------------------------------------------------------------------------- #

def montar_mensagem(cfg: dict, resultados: list[Resultado], faixa: str,
                    piso: int | None, motivo: str, resumo: bool) -> str:
    ida = datetime.fromisoformat(cfg["data_ida"]).strftime("%d/%m/%Y")
    volta = datetime.fromisoformat(cfg["data_volta"]).strftime("%d/%m/%Y")

    partes = [
        "<b>" + CABECALHO[faixa].format(**cfg["alvos"]) + "</b>",
        f"✈️ {NOMES.get(cfg['origem'], cfg['origem'])} → Rio de Janeiro · ida e volta",
        f"📅 {ida} → {volta} · {cfg['adultos']} pessoa · econômica",
    ]

    com_oferta = [r for r in resultados if r.ofertas]
    if not com_oferta:
        partes += ["", "Nenhuma oferta retornada nesta rodada."]

    for fonte in (GOOGLE, VAIDEPROMO):
        da_fonte = [r for r in com_oferta if r.fonte == fonte]
        if not da_fonte:
            continue
        melhor = min(r.piso for r in da_fonte if r.piso is not None)
        partes += ["", f"━━ <b>{fonte}</b> · piso R$ {melhor} ━━"]
        for r in sorted(da_fonte, key=lambda r: r.piso or 10 ** 9):
            partes.append(f"<i>{NOMES.get(r.destino, r.destino)}</i>")
            for o in r.ofertas[:2]:
                partes.append(o.linha())
            partes.append(f'<a href="{r.url}">ver ofertas</a>')

    historico = ler_historico(30)
    todos = pisos_validos(historico)
    if todos and piso is not None:
        minimo = min(todos)
        partes += ["", f"📉 Mínimo em 30 dias: R$ {minimo} · média R$ {round(sum(todos)/len(todos))}"]
        # So faz sentido falar em recorde depois de algumas medicoes.
        if piso <= minimo and len(historico) >= 5:
            partes.append("⭐ <b>É o menor preço já registrado pelo monitor.</b>")

    if resumo:
        partes.append(f"<i>Resumo diário · {len(historico)} medições em 30 dias</i>")

    erros = [r for r in resultados if r.erro]
    if erros:
        partes += ["", "⚠️ <i>Falhou: " + ", ".join(f"{r.fonte}/{r.destino}" for r in erros) + "</i>"]

    partes += ["", f"<i>{motivo} · {agora():%d/%m %H:%M} BRT</i>"]
    return "\n".join(partes)


def _sem_tags(texto: str) -> str:
    for tag in ("<b>", "</b>", "<i>", "</i>"):
        texto = texto.replace(tag, "")
    return texto


def enviar_telegram(texto: str, dry_run: bool) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

    if dry_run or not token or not chat:
        if not dry_run:
            log("TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID ausentes — imprimindo em vez de enviar.")
        print("\n" + "=" * 62)
        print(_sem_tags(texto))
        print("=" * 62 + "\n")
        return

    dados = urllib.parse.urlencode({
        "chat_id": chat,
        "text": texto,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/sendMessage", data=dados)
    with urllib.request.urlopen(req, timeout=30) as resp:
        corpo = json.loads(resp.read())
    if not corpo.get("ok"):
        raise RuntimeError(f"Telegram recusou a mensagem: {corpo}")
    log("Mensagem enviada no Telegram.")


# --------------------------------------------------------------------------- #

def coletar(cfg: dict, proxy: str | None) -> list[Resultado]:
    resultados: list[Resultado] = []
    for destino in cfg["destinos"]:
        resultados.append(buscar_google(cfg, destino, proxy, log))
    for destino in cfg.get("destinos_vaidepromo", []):
        resultados.append(buscar_vaidepromo(cfg, destino, log))
    return resultados


def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor de passagens FLN <-> Rio")
    ap.add_argument("--force", action="store_true", help="envia mensagem mesmo sem promocao")
    ap.add_argument("--resumo", action="store_true", help="envia o resumo diario")
    ap.add_argument("--dry-run", action="store_true", help="imprime em vez de enviar")
    ap.add_argument("--config", default=str(RAIZ / "config.json"))
    args = ap.parse_args()

    carregar_env()
    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    proxy = os.environ.get("PROXY_URL") or None
    ESTADO.mkdir(exist_ok=True)

    log(f"Buscando {cfg['origem']} -> Rio | {cfg['data_ida']} / {cfg['data_volta']}")
    resultados = coletar(cfg, proxy)
    gravar_historico(resultados)

    pisos = [r.piso for r in resultados if r.piso is not None]
    if not pisos:
        log("Nenhuma fonte respondeu — possível bloqueio ou mudança nos sites.")
        enviar_telegram(
            "⚠️ <b>Monitor de voos falhou</b>\nNenhuma fonte respondeu nesta rodada. "
            "Pode ser bloqueio do IP do runner ou mudança no Google Flights / Vai de Promo.\n"
            f"<i>{agora():%d/%m %H:%M} BRT</i>",
            args.dry_run,
        )
        return 1

    piso = min(pisos)
    faixa = classificar(piso, cfg["alvos"])
    log(f"Piso da rodada: R$ {piso} (faixa: {faixa})")

    alertar, motivo = deve_alertar(piso, faixa, cfg)
    if not (alertar or args.force or args.resumo):
        log(f"Sem envio: {motivo}")
        return 0

    if alertar:
        rotulo = motivo
    elif args.resumo:
        rotulo = "resumo diário"
    else:
        rotulo = "envio manual"

    enviar_telegram(montar_mensagem(cfg, resultados, faixa, piso, rotulo, args.resumo),
                    args.dry_run)
    if alertar and not args.dry_run:
        gravar_ultimo_alerta(piso, faixa)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
