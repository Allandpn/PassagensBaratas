# -*- coding: utf-8 -*-
"""
Monitor de passagens Florianopolis <-> Rio de Janeiro.

Estrategia: em vez de confiar no ranking "melhores voos" do Google (que esconde
tarifas baratas), consulta com teto de preco crescente (800 -> 900 -> 1000 ...).
O primeiro teto que devolve resultado e o piso real daquela rota naquele momento.

Uso:
    python monitor.py            # roda e so avisa se houver promocao
    python monitor.py --force    # roda e sempre manda mensagem
    python monitor.py --resumo   # manda o resumo diario com a tendencia
    python monitor.py --dry-run  # nao envia nada, so imprime
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fast_flights import FlightQuery, Passengers, create_query, get_flights

RAIZ = Path(__file__).resolve().parent
ESTADO = RAIZ / "state"
HISTORICO = ESTADO / "historico.jsonl"
ULTIMO_ALERTA = ESTADO / "ultimo_alerta.json"
BRT = timezone(timedelta(hours=-3))

# O console do Windows abre em cp1252 e engasga nos acentos/emoji das mensagens.
if sys.stdout.encoding and sys.stdout.encoding.lower().replace("-", "") != "utf8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# O Google nao devolve nome de cidade pelo codigo IATA nesta API; mapa so para exibicao.
NOMES = {
    "FLN": "Florianópolis",
    "RIO": "Rio (todos os aeroportos)",
    "GIG": "Rio / Galeão",
    "SDU": "Rio / Santos Dumont",
}


def agora() -> datetime:
    return datetime.now(BRT)


def log(msg: str) -> None:
    print(f"[{agora():%H:%M:%S}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Busca
# --------------------------------------------------------------------------- #

@dataclass
class Oferta:
    preco: int
    companhia: str
    rota: str
    partida: str
    chegada: str
    paradas: int
    destino: str

    def linha(self) -> str:
        conexoes = "direto" if self.paradas == 0 else (
            "1 conexão" if self.paradas == 1 else f"{self.paradas} conexões"
        )
        preco = f"{self.preco:,}".replace(",", ".")
        return (
            f"<b>R$ {preco}</b> — {self.companhia}\n"
            f"   {self.rota}  ({conexoes})\n"
            f"   sai {self.partida} · chega {self.chegada}"
        )


@dataclass
class ResultadoDestino:
    destino: str
    piso: int | None = None          # menor preco encontrado
    ofertas: list[Oferta] = field(default_factory=list)
    url: str = ""
    erro: str | None = None


class SemResultado(Exception):
    """O Google respondeu ok, mas nao existe voo dentro do teto pedido."""


def _monta_query(cfg: dict, destino: str, teto: int | None):
    return create_query(
        flights=[
            FlightQuery(date=cfg["data_ida"], from_airport=cfg["origem"], to_airport=destino),
            FlightQuery(date=cfg["data_volta"], from_airport=destino, to_airport=cfg["origem"]),
        ],
        seat=cfg["classe"],
        trip="round-trip",
        passengers=Passengers(adults=cfg["adultos"]),
        language=cfg["idioma"],
        currency=cfg["moeda"],
        max_price=teto,
    )


def _buscar(query, tentativas: int, proxy: str | None):
    """Devolve a lista de ofertas. Levanta SemResultado quando nada cabe no teto."""
    ultimo = None
    for n in range(1, tentativas + 1):
        try:
            return get_flights(query, proxy=proxy) if proxy else get_flights(query)
        except TypeError:
            # A lib estoura TypeError quando o payload de voos vem vazio. Isso e
            # "nenhum voo dentro do teto", nao falha de rede (verificado em teste).
            raise SemResultado
        except Exception as exc:  # rede, bloqueio, mudanca no HTML
            ultimo = exc
            if n < tentativas:
                espera = 2 ** n + random.uniform(0, 1.5)
                log(f"    tentativa {n} falhou ({type(exc).__name__}), repetindo em {espera:.1f}s")
                time.sleep(espera)
    raise RuntimeError(f"{type(ultimo).__name__}: {ultimo}")


def _para_oferta(voo, destino: str) -> Oferta:
    pernas = voo.flights
    rota = "-".join([pernas[0].from_airport.code] + [p.to_airport.code for p in pernas])
    d, a = pernas[0].departure, pernas[-1].arrival
    return Oferta(
        preco=int(voo.price),
        companhia="/".join(voo.airlines) or voo.type,
        rota=rota,
        partida=f"{d.date[2]:02d}/{d.date[1]:02d} {d.time[0]:02d}:{d.time[1]:02d}",
        chegada=f"{a.date[2]:02d}/{a.date[1]:02d} {a.time[0]:02d}:{a.time[1]:02d}",
        paradas=len(pernas) - 1,
        destino=destino,
    )


def varrer_destino(cfg: dict, destino: str, proxy: str | None) -> ResultadoDestino:
    """Sobe a escada de tetos ate achar o primeiro que devolve voo."""
    res = ResultadoDestino(destino=destino, url=_monta_query(cfg, destino, None).url())
    pausa = cfg["pausa_entre_buscas_seg"]
    tentativas = cfg["tentativas"]

    for teto in list(cfg["degraus_preco"]) + [None]:
        rotulo = f"ate R$ {teto}" if teto else "sem teto (melhores voos)"
        try:
            voos = _buscar(_monta_query(cfg, destino, teto), tentativas, proxy)
        except SemResultado:
            log(f"  {destino} {rotulo}: nada")
            time.sleep(pausa)
            continue
        except RuntimeError as exc:
            # Falha de rede/bloqueio num degrau nao invalida os outros: segue a escada.
            log(f"  {destino} {rotulo}: ERRO {exc}")
            res.erro = str(exc)
            time.sleep(pausa)
            continue

        res.ofertas = sorted((_para_oferta(v, destino) for v in voos), key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
        res.erro = None  # achou preco: falhas em degraus anteriores nao importam mais
        log(f"  {destino} {rotulo}: {len(voos)} ofertas, piso R$ {res.piso}")
        return res

    return res


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


def gravar_historico(resultados: list[ResultadoDestino]) -> None:
    ESTADO.mkdir(exist_ok=True)
    linha = {
        "quando": agora().isoformat(timespec="minutes"),
        "pisos": {r.destino: r.piso for r in resultados},
        "erros": {r.destino: r.erro for r in resultados if r.erro},
    }
    with HISTORICO.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(linha, ensure_ascii=False) + "\n")


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
    return [p for reg in registros for p in reg.get("pisos", {}).values() if isinstance(p, int)]


# --------------------------------------------------------------------------- #
# Mensagem
# --------------------------------------------------------------------------- #

def montar_mensagem(cfg: dict, resultados: list[ResultadoDestino], faixa: str,
                    piso: int | None, motivo: str, resumo: bool) -> str:
    alvos = cfg["alvos"]
    ida = datetime.fromisoformat(cfg["data_ida"]).strftime("%d/%m/%Y")
    volta = datetime.fromisoformat(cfg["data_volta"]).strftime("%d/%m/%Y")

    partes = [
        "<b>" + CABECALHO[faixa].format(**alvos) + "</b>",
        f"✈️ {NOMES.get(cfg['origem'], cfg['origem'])} → Rio de Janeiro · ida e volta",
        f"📅 {ida} → {volta} · {cfg['adultos']} pessoa · econômica",
        "",
    ]

    com_oferta = [r for r in resultados if r.ofertas]
    if not com_oferta:
        partes.append("Nenhuma oferta retornada nesta rodada.")
    for r in sorted(com_oferta, key=lambda r: r.piso or 10 ** 9):
        partes.append(f"<b>— {NOMES.get(r.destino, r.destino)} —</b>")
        for o in r.ofertas[:3]:
            partes.append(o.linha())
        partes.append(f'<a href="{r.url}">abrir no Google Flights</a>')
        partes.append("")

    historico = ler_historico(30)
    todos = pisos_validos(historico)
    if todos and piso is not None:
        minimo = min(todos)
        media = round(sum(todos) / len(todos))
        partes.append(f"📉 Mínimo em 30 dias: R$ {minimo} · média R$ {media}")
        # So faz sentido falar em recorde depois de algumas medicoes.
        if piso <= minimo and len(historico) >= 5:
            partes.append("⭐ <b>É o menor preço já registrado pelo monitor.</b>")

    if resumo:
        partes.append("")
        partes.append(f"<i>Resumo diário · {len(historico)} medições nos últimos 30 dias</i>")

    erros = [r for r in resultados if r.erro]
    if erros:
        partes.append("")
        partes.append("⚠️ <i>Falha ao consultar: " + ", ".join(r.destino for r in erros) + "</i>")

    partes.append("")
    partes.append(f"<i>{motivo} · {agora():%d/%m %H:%M} BRT</i>")
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

def main() -> int:
    ap = argparse.ArgumentParser(description="Monitor de passagens FLN <-> Rio")
    ap.add_argument("--force", action="store_true", help="envia mensagem mesmo sem promocao")
    ap.add_argument("--resumo", action="store_true", help="envia o resumo diario")
    ap.add_argument("--dry-run", action="store_true", help="imprime em vez de enviar")
    ap.add_argument("--config", default=str(RAIZ / "config.json"))
    args = ap.parse_args()

    cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
    proxy = os.environ.get("PROXY_URL") or None
    ESTADO.mkdir(exist_ok=True)

    log(f"Buscando {cfg['origem']} -> {cfg['destinos']} | {cfg['data_ida']} / {cfg['data_volta']}")
    resultados = [varrer_destino(cfg, d, proxy) for d in cfg["destinos"]]
    gravar_historico(resultados)

    pisos = [r.piso for r in resultados if r.piso is not None]
    if not pisos:
        log("Nenhum destino respondeu — possível bloqueio do Google ou mudança no site.")
        enviar_telegram(
            "⚠️ <b>Monitor de voos falhou</b>\nNenhum destino respondeu nesta rodada. "
            "Pode ser bloqueio do IP do runner ou mudança no Google Flights.\n"
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
