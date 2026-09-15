# -*- coding: utf-8 -*-
"""
Fontes de preco do monitor.

Google Flights (via fast-flights)
    Busca com teto de preco crescente. O ranking "melhores voos" do Google
    esconde tarifas mais baratas; o primeiro teto que devolve resultado e o
    piso real da rota.

Vai de Promo
    API JSON publica do proprio site (mesma que o buscador deles usa no
    navegador). Permitida pelo robots.txt: so /redirect/, /safearea/ e as
    paginas de pagamento sao Disallow. Preco ja com taxas, pronto pra compra,
    e costuma trazer voo direto que o Google nao lista.

Skyscanner ficou de fora de proposito: o robots.txt deles poe Disallow em
/transporte/* (a busca de voos), /dataservices/* e /skippy_api/* (as APIs
internas de preco) para User-agent: *. Ou seja, pedem explicitamente para
bots nao automatizarem a busca.
"""
from __future__ import annotations

import json
import random
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime

import primp
from fast_flights import FlightQuery, Passengers, create_query, get_flights

GOOGLE = "Google Flights"
VAIDEPROMO = "Vai de Promo"

VDP_PROVIDERS = "https://site.aereo.vaidepromo.com.br/api/air/providers/{ori}/{des}/{data}/"
VDP_PAGINA = "https://www.vaidepromo.com.br/passagens-aereas/pesquisa/{token}/{ad}/0/0/Y/"
VDP_HEADERS = {
    "Referer": "https://www.vaidepromo.com.br/",
    "Accept": "application/json",
}
# Sem isso o primp usa um fingerprint TLS que o backend deles rejeita.
VDP_IMPERSONATE = "chrome_145"


@dataclass
class Oferta:
    preco: int
    companhia: str
    rota: str
    partida: str
    chegada: str
    paradas: int
    destino: str
    fonte: str

    def linha(self) -> str:
        paradas = "direto" if self.paradas == 0 else (
            "1 conexão" if self.paradas == 1 else f"{self.paradas} conexões"
        )
        preco = f"{self.preco:,}".replace(",", ".")
        return (
            f"<b>R$ {preco}</b> — {self.companhia} · {paradas}\n"
            f"   {self.rota} · sai {self.partida} · chega {self.chegada}"
        )


@dataclass
class Resultado:
    fonte: str
    destino: str
    piso: int | None = None
    ofertas: list[Oferta] = field(default_factory=list)
    url: str = ""
    erro: str | None = None


class SemResultado(Exception):
    """A fonte respondeu ok, mas nao ha voo dentro do que foi pedido."""


def _tentar(fn, tentativas: int, log):
    """Repete com backoff. SemResultado passa direto, nao e falha."""
    ultimo = None
    for n in range(1, tentativas + 1):
        try:
            return fn()
        except SemResultado:
            raise
        except Exception as exc:
            ultimo = exc
            if n < tentativas:
                espera = 2 ** n + random.uniform(0, 1.5)
                log(f"    tentativa {n} falhou ({type(exc).__name__}), repetindo em {espera:.1f}s")
                time.sleep(espera)
    raise RuntimeError(f"{type(ultimo).__name__}: {ultimo}")


# --------------------------------------------------------------------------- #
# Google Flights
# --------------------------------------------------------------------------- #

def _query_google(cfg: dict, destino: str, teto: int | None):
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


def _oferta_google(voo, destino: str) -> Oferta:
    pernas = voo.flights
    d, a = pernas[0].departure, pernas[-1].arrival
    return Oferta(
        preco=int(voo.price),
        companhia="/".join(voo.airlines) or voo.type,
        rota="-".join([pernas[0].from_airport.code] + [p.to_airport.code for p in pernas]),
        partida=f"{d.date[2]:02d}/{d.date[1]:02d} {d.time[0]:02d}:{d.time[1]:02d}",
        chegada=f"{a.date[2]:02d}/{a.date[1]:02d} {a.time[0]:02d}:{a.time[1]:02d}",
        paradas=len(pernas) - 1,
        destino=destino,
        fonte=GOOGLE,
    )


def buscar_google(cfg: dict, destino: str, proxy: str | None, log) -> Resultado:
    res = Resultado(fonte=GOOGLE, destino=destino, url=_query_google(cfg, destino, None).url())
    pausa = cfg["pausa_entre_buscas_seg"]

    for teto in list(cfg["degraus_preco"]) + [None]:
        rotulo = f"ate R$ {teto}" if teto else "sem teto"
        query = _query_google(cfg, destino, teto)

        def chamar():
            try:
                return get_flights(query, proxy=proxy) if proxy else get_flights(query)
            except TypeError:
                # A lib estoura TypeError quando o payload de voos vem vazio.
                # E "nenhum voo dentro do teto", nao falha de rede.
                raise SemResultado

        try:
            voos = _tentar(chamar, cfg["tentativas"], log)
        except SemResultado:
            log(f"  [Google] {destino} {rotulo}: nada")
            time.sleep(pausa)
            continue
        except RuntimeError as exc:
            log(f"  [Google] {destino} {rotulo}: ERRO {exc}")
            res.erro = str(exc)
            time.sleep(pausa)
            continue

        res.ofertas = sorted((_oferta_google(v, destino) for v in voos), key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
        res.erro = None
        log(f"  [Google] {destino} {rotulo}: {len(voos)} ofertas, piso R$ {res.piso}")
        return res

    return res


# --------------------------------------------------------------------------- #
# Vai de Promo
# --------------------------------------------------------------------------- #

def _aammdd(data_iso: str) -> str:
    return datetime.fromisoformat(data_iso).strftime("%y%m%d")


def _token_vdp(cfg: dict, destino: str) -> str:
    """Ida e volta usam hifen: FLNGIG261031-GIGFLN261122 (descoberto testando a API)."""
    ori, ida, volta = cfg["origem"], _aammdd(cfg["data_ida"]), _aammdd(cfg["data_volta"])
    return f"{ori}{destino}{ida}-{destino}{ori}{volta}"


def _oferta_vdp(dados: dict, rec: dict, destino: str) -> Oferta | None:
    preco = dados["prices"][rec["prices"][0]]["total"]
    trechos = []
    for perna, opcoes in enumerate(rec["itineraries"]):
        if not opcoes:
            continue
        try:
            trechos.append(dados["itineraries"][perna][opcoes[0]])
        except (IndexError, KeyError):
            return None
    if not trechos:
        return None

    jornada = trechos[0]["journeys"][0]
    segmentos = jornada.get("segments") or []
    if not segmentos:
        return None

    def hora(carimbo: str) -> str:
        try:
            dt = datetime.fromisoformat(carimbo)
            return f"{dt.day:02d}/{dt.month:02d} {dt.hour:02d}:{dt.minute:02d}"
        except ValueError:
            return "?"

    codigos = {s.get("marketing_carrier_code") for s in segmentos if s.get("marketing_carrier_code")}
    nomes = {c["code"]: c["name"] for c in dados.get("carriers", [])}
    rota = "-".join(
        [segmentos[0]["departure"]["location"]] + [s["arrival"]["location"] for s in segmentos]
    )
    return Oferta(
        preco=round(preco),
        companhia="/".join(sorted(nomes.get(c, c) for c in codigos)) or "?",
        rota=rota,
        partida=hora(segmentos[0]["departure"]["date"]),
        chegada=hora(segmentos[-1]["arrival"]["date"]),
        paradas=int(jornada.get("total_stops") or 0),
        destino=destino,
        fonte=VAIDEPROMO,
    )


def buscar_vaidepromo(cfg: dict, destino: str, log) -> Resultado:
    token = _token_vdp(cfg, destino)
    res = Resultado(
        fonte=VAIDEPROMO,
        destino=destino,
        url=VDP_PAGINA.format(token=token, ad=cfg["adultos"]),
    )
    cliente = primp.Client(impersonate=VDP_IMPERSONATE, timeout=cfg.get("timeout_vdp_seg", 120))
    pausa = cfg["pausa_entre_buscas_seg"]

    def json_de(url: str) -> dict:
        r = cliente.get(url, headers=VDP_HEADERS)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        return json.loads(r.text)

    url_prov = VDP_PROVIDERS.format(ori=cfg["origem"], des=destino, data=_aammdd(cfg["data_ida"]))
    try:
        providers = _tentar(lambda: json_de(url_prov), cfg["tentativas"], log).get("providers", [])
    except RuntimeError as exc:
        log(f"  [VaiDePromo] {destino}: ERRO ao listar companhias — {exc}")
        res.erro = str(exc)
        return res

    todas: list[Oferta] = []
    falhas: list[str] = []
    for prov in providers:
        codigo = prov.get("code")
        base = prov.get("endpoint")
        if not codigo or not base:
            continue
        url = f"{base}/api/search/{token}/{cfg['adultos']}/0/0/Y/{codigo}"
        try:
            dados = _tentar(lambda u=url: json_de(u), cfg["tentativas"], log)
        except RuntimeError as exc:
            log(f"  [VaiDePromo] {destino} {codigo}: ERRO {exc}")
            falhas.append(codigo)
            time.sleep(pausa)
            continue

        if dados.get("errors"):
            log(f"  [VaiDePromo] {destino} {codigo}: sem tarifa")
            time.sleep(pausa)
            continue

        do_provider = [
            o for o in (_oferta_vdp(dados, rec, destino) for rec in dados.get("recommendations", []))
            if o is not None
        ]
        if do_provider:
            log(f"  [VaiDePromo] {destino} {codigo}: {len(do_provider)} tarifas, "
                f"piso R$ {min(o.preco for o in do_provider)}")
            todas.extend(do_provider)
        time.sleep(pausa)

    if todas:
        res.ofertas = sorted(todas, key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
    elif falhas:
        res.erro = "falha nas companhias: " + ", ".join(falhas)
    return res
