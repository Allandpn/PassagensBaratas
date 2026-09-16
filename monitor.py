# -*- coding: utf-8 -*-
"""
Monitor de passagens Florianopolis <-> Rio de Janeiro.

Consulta duas fontes (Google Flights e Vai de Promo — ver fontes.py), varre a
janela de datas configurada, compara o piso de todas as combinacoes e avisa no
Telegram quando entra na faixa de preco desejada.

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
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from fontes import (
    BASES, BASE_PADRAO, GOOGLE, VAIDEPROMO, Oferta, Resultado, buscar_google,
    buscar_vaidepromo, listar_providers, reais,
)

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

ROTULO_BASE = {
    "tarifa": "tarifa por adulto (o valor grande do Vai de Promo, sem taxas)",
    "com_taxas": "tarifa + taxa de embarque (comparável com o Google)",
    "total": "preço total, já com taxa de serviço",
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


def ddmm(data_iso: str) -> str:
    return f"{datetime.fromisoformat(data_iso):%d/%m}"


# --------------------------------------------------------------------------- #
# Janela de datas
# --------------------------------------------------------------------------- #

def expandir_datas(valor) -> list[str]:
    """Aceita tres formas em data_ida / data_volta:

        "2026-11-22"                            -> uma data so
        ["2026-11-22", "2026-11-28"]            -> lista explicita
        {"de": "2026-11-22", "ate": "2026-11-28"}  -> intervalo, dia a dia
    """
    if isinstance(valor, str):
        return [valor]
    if isinstance(valor, list):
        return sorted({str(v) for v in valor})
    if isinstance(valor, dict):
        inicio = date.fromisoformat(valor["de"])
        fim = date.fromisoformat(valor.get("ate", valor["de"]))
        if fim < inicio:
            raise ValueError(f'intervalo invertido: "de" {inicio} vem depois de "ate" {fim}')
        return [(inicio + timedelta(days=n)).isoformat() for n in range((fim - inicio).days + 1)]
    raise ValueError(f"data em formato não reconhecido: {valor!r}")


def combinacoes(cfg: dict) -> list[tuple[str, str]]:
    """Pares (ida, volta) da janela. Descarta volta anterior a ida e respeita
    o teto de combinacoes — a rodada cresce no produto das duas janelas."""
    idas = expandir_datas(cfg["data_ida"])
    voltas = expandir_datas(cfg["data_volta"])
    pares = [(i, v) for i in idas for v in voltas if v >= i]
    if not pares:
        raise ValueError("nenhuma combinação válida: toda data de volta é anterior à de ida")

    teto = cfg.get("max_combinacoes_datas", 12)
    if len(pares) > teto:
        log(f"Janela pede {len(pares)} combinações; cortando em {teto} "
            f"(ajuste max_combinacoes_datas se quiser mais).")
        pares = pares[:teto]
    return pares


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


def melhor_oferta(resultados: list[Resultado]) -> Oferta | None:
    todas = [o for r in resultados for o in r.ofertas]
    return min(todas, key=lambda o: o.preco) if todas else None


def url_da_oferta(resultados: list[Resultado], oferta: Oferta) -> str:
    for r in resultados:
        if r.fonte == oferta.fonte and r.destino == oferta.destino \
                and r.data_volta == oferta.data_volta and r.data_ida == oferta.data_ida:
            return r.url
    return ""


def pisos_por_volta(resultados: list[Resultado]) -> dict[str, int]:
    """Menor preco de cada data de volta, somando as duas fontes."""
    saida: dict[str, int] = {}
    for r in resultados:
        if r.piso is None:
            continue
        atual = saida.get(r.data_volta)
        if atual is None or r.piso < atual:
            saida[r.data_volta] = r.piso
    return dict(sorted(saida.items()))


def gravar_historico(cfg: dict, resultados: list[Resultado]) -> None:
    ESTADO.mkdir(exist_ok=True)
    pisos: dict[str, dict[str, int | None]] = {}
    erros: dict[str, str] = {}
    for r in resultados:
        pisos.setdefault(r.fonte, {})[r.rotulo] = r.piso
        if r.erro:
            erros[f"{r.fonte}/{r.rotulo}"] = r.erro

    registro = {
        "quando": agora().isoformat(timespec="minutes"),
        "base": cfg.get("base_preco", BASE_PADRAO),
        "pisos": pisos,
        "erros": erros,
    }
    melhor = melhor_oferta(resultados)
    if melhor:
        registro["melhor"] = {
            "preco": melhor.preco,
            "tarifa": melhor.tarifa,
            "com_taxas": melhor.com_taxas,
            "total": melhor.total,
            "fonte": melhor.fonte,
            "destino": melhor.destino,
            "ida": melhor.data_ida,
            "volta": melhor.data_volta,
        }
    with HISTORICO.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(registro, ensure_ascii=False) + "\n")


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
    """Aceita o formato antigo ({destino: piso}) e o novo ({fonte: {rotulo: piso}})."""
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

def _linha_datas(cfg: dict) -> str:
    idas = expandir_datas(cfg["data_ida"])
    voltas = expandir_datas(cfg["data_volta"])

    def faixa(datas: list[str]) -> str:
        if len(datas) == 1:
            return ddmm(datas[0])
        return f"{ddmm(datas[0])} a {ddmm(datas[-1])} ({len(datas)} datas)"

    return f"📅 ida {faixa(idas)} · volta {faixa(voltas)}"


def montar_mensagem(cfg: dict, resultados: list[Resultado], faixa: str,
                    piso: int | None, motivo: str, resumo: bool) -> str:
    base = cfg.get("base_preco", BASE_PADRAO)
    nomes = {**NOMES, **cfg.get("nomes", {})}

    partes = [
        "<b>" + CABECALHO[faixa].format(**cfg["alvos"]) + "</b>",
        f"✈️ {nomes.get(cfg['origem'], cfg['origem'])} → Rio de Janeiro · ida e volta",
        _linha_datas(cfg) + f" · {cfg['adultos']} pessoa · econômica",
        f"<i>Alerta pela {ROTULO_BASE.get(base, base)}.</i>",
    ]

    melhor = melhor_oferta(resultados)
    if melhor is None:
        partes += ["", "Nenhuma oferta retornada nesta rodada."]
    else:
        partes += [
            "",
            f"━━ <b>Melhor da janela</b> · {melhor.fonte} ━━",
            f"📅 {ddmm(melhor.data_ida)} → {ddmm(melhor.data_volta)} · "
            f"{nomes.get(melhor.destino, melhor.destino)}",
            melhor.linha(),
        ]
        url = url_da_oferta(resultados, melhor)
        if url:
            partes.append(f'<a href="{url}">ver ofertas</a>')

    # Piso de cada data de volta: e o que responde "vale a pena esticar?".
    por_volta = pisos_por_volta(resultados)
    if len(por_volta) > 1:
        minimo = min(por_volta.values())
        partes += ["", "━━ <b>Piso por data de volta</b> ━━"]
        for data, valor in por_volta.items():
            estrela = " ⭐" if valor == minimo else ""
            partes.append(f"{ddmm(data)} · R$ {reais(valor)}{estrela}")

    # Piso de cada fonte, para ver quem esta mais barata nesta rodada.
    partes.append("")
    for fonte in (GOOGLE, VAIDEPROMO):
        da_fonte = [r.piso for r in resultados if r.fonte == fonte and r.piso is not None]
        if da_fonte:
            partes.append(f"<i>{fonte}: piso R$ {reais(min(da_fonte))}</i>")

    historico = ler_historico(30)
    todos = pisos_validos(historico)
    if todos and piso is not None:
        minimo = min(todos)
        partes += ["", f"📉 Mínimo em 30 dias: R$ {reais(minimo)} · "
                       f"média R$ {reais(round(sum(todos) / len(todos)))}"]
        # So faz sentido falar em recorde depois de algumas medicoes.
        if piso <= minimo and len(historico) >= 5:
            partes.append("⭐ <b>É o menor preço já registrado pelo monitor.</b>")

    if resumo:
        partes.append(f"<i>Resumo diário · {len(historico)} medições em 30 dias</i>")

    erros = [r for r in resultados if r.erro]
    if erros:
        partes += ["", "⚠️ <i>Falhou: " + ", ".join(f"{r.fonte}/{r.rotulo}" for r in erros) + "</i>"]

    partes += ["", f"<i>{motivo} · {agora():%d/%m %H:%M} BRT</i>"]
    return "\n".join(partes)


def _sem_tags(texto: str) -> str:
    """Versao legivel no console: o Telegram renderiza o HTML, o terminal nao."""
    texto = re.sub(r'<a href="([^"]+)">([^<]*)</a>', r"\2: \1", texto)
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
    pares = combinacoes(cfg)
    resultados: list[Resultado] = []

    for destino in cfg["destinos"]:
        for ida, volta in pares:
            resultados.append(buscar_google(cfg, destino, ida, volta, proxy, log))

    for destino in cfg.get("destinos_vaidepromo", []):
        # A lista de companhias so depende de origem/destino/ida, entao vale
        # para a janela inteira: uma chamada em vez de uma por data de volta.
        providers = None
        for ida, volta in pares:
            if providers is None:
                try:
                    providers = listar_providers(cfg, destino, ida, log)
                except RuntimeError as exc:
                    log(f"  [VaiDePromo] {destino}: ERRO ao listar companhias — {exc}")
                    resultados.append(Resultado(fonte=VAIDEPROMO, destino=destino,
                                                data_ida=ida, data_volta=volta, erro=str(exc)))
                    break
            resultados.append(buscar_vaidepromo(cfg, destino, ida, volta, log, providers))

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
    base = cfg.get("base_preco", BASE_PADRAO)
    if base not in BASES:
        log(f'base_preco "{base}" não existe; usando "{BASE_PADRAO}". Opções: {", ".join(BASES)}')
        cfg["base_preco"] = BASE_PADRAO
    proxy = os.environ.get("PROXY_URL") or None
    ESTADO.mkdir(exist_ok=True)

    pares = combinacoes(cfg)
    log(f"Buscando {cfg['origem']} -> Rio | {len(pares)} combinações de data "
        f"({pares[0][0]} → {pares[0][1]} ... {pares[-1][0]} → {pares[-1][1]}) "
        f"| base de preço: {cfg.get('base_preco', BASE_PADRAO)}")
    resultados = coletar(cfg, proxy)
    gravar_historico(cfg, resultados)

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
    melhor = melhor_oferta(resultados)
    if melhor:
        log(f"Piso da rodada: R$ {piso} (faixa: {faixa}) — {melhor.fonte} {melhor.destino} "
            f"{ddmm(melhor.data_ida)} → {ddmm(melhor.data_volta)} · "
            f"tarifa R$ {melhor.tarifa} · total R$ {melhor.total}")
    else:
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
