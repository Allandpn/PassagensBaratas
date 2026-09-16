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
    paginas de pagamento sao Disallow. Traz voo direto que o Google nao lista
    e separa o preco em tres numeros (ver "Bases de preco" abaixo).

Skyscanner ficou de fora de proposito: o robots.txt deles poe Disallow em
/transporte/* (a busca de voos), /dataservices/* e /skippy_api/* (as APIs
internas de preco) para User-agent: *. Ou seja, pedem explicitamente para
bots nao automatizarem a busca.

Bases de preco
    O Vai de Promo devolve tres valores para a mesma tarifa, e a tela do site
    mostra o primeiro em destaque ("R$ 966,61 no PIX"):

        fare   -> tarifa, o que a tela chama de "Adultos (1x)"
        amount -> fare + taxa de embarque
        total  -> amount + taxa de servico (~10%), o "Preco total" da tela

    O Google Flights entrega um numero so, que ja inclui taxa de embarque e
    nao tem taxa de servico: equivale ao `amount`. Por isso guardamos os tres
    em toda oferta e a config escolhe qual deles dispara o alerta
    (`base_preco`) -- a mensagem sempre mostra os outros junto, para nao
    comparar tarifa de uma fonte com total da outra sem perceber.
"""
from __future__ import annotations

import json
import random
import time
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

BASES = ("tarifa", "com_taxas", "total")
BASE_PADRAO = "com_taxas"


def reais(valor: int | None) -> str:
    if valor is None:
        return "?"
    return f"{valor:,}".replace(",", ".")


@dataclass
class Oferta:
    preco: int            # base escolhida em cfg["base_preco"]; e o que alerta
    companhia: str
    rota: str
    partida: str
    chegada: str
    paradas: int
    destino: str
    fonte: str
    data_ida: str = ""
    data_volta: str = ""
    tarifa: int | None = None     # so o Vai de Promo separa a tarifa ("no PIX")
    com_taxas: int | None = None  # tarifa + taxa de embarque
    total: int | None = None      # tudo, ja com a taxa de servico

    def linha(self) -> str:
        paradas = "direto" if self.paradas == 0 else (
            "1 conexão" if self.paradas == 1 else f"{self.paradas} conexões"
        )
        partes = [
            f"<b>R$ {reais(self.preco)}</b> — {self.companhia} · {paradas}",
            f"   {self.rota} · sai {self.partida} · chega {self.chegada}",
        ]
        # So repete a decomposicao quando os numeros diferem de fato: no Google
        # os tres sao o mesmo valor e a linha extra seria ruido.
        valores = {v for v in (self.tarifa, self.com_taxas, self.total) if v is not None}
        if len(valores) > 1:
            partes.append(
                f"   <i>tarifa R$ {reais(self.tarifa)} · c/ taxas R$ {reais(self.com_taxas)}"
                f" · total R$ {reais(self.total)}</i>"
            )
        return "\n".join(partes)


@dataclass
class Resultado:
    fonte: str
    destino: str
    data_ida: str = ""
    data_volta: str = ""
    piso: int | None = None
    ofertas: list[Oferta] = field(default_factory=list)
    url: str = ""
    erro: str | None = None

    @property
    def rotulo(self) -> str:
        """Ex.: 'GIG 23/11' — identifica a combinacao no log e no historico."""
        if not self.data_volta:
            return self.destino
        return f"{self.destino} {datetime.fromisoformat(self.data_volta):%d/%m}"


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


def escolher_base(cfg: dict, tarifa: int, com_taxas: int, total: int) -> int:
    base = cfg.get("base_preco", BASE_PADRAO)
    return {"tarifa": tarifa, "com_taxas": com_taxas, "total": total}.get(base, com_taxas)


# --------------------------------------------------------------------------- #
# Google Flights
# --------------------------------------------------------------------------- #

def _query_google(cfg: dict, destino: str, ida: str, volta: str, teto: int | None):
    return create_query(
        flights=[
            FlightQuery(date=ida, from_airport=cfg["origem"], to_airport=destino),
            FlightQuery(date=volta, from_airport=destino, to_airport=cfg["origem"]),
        ],
        seat=cfg["classe"],
        trip="round-trip",
        passengers=Passengers(adults=cfg["adultos"]),
        language=cfg["idioma"],
        currency=cfg["moeda"],
        max_price=teto,
    )


def _oferta_google(voo, destino: str, ida: str, volta: str) -> Oferta:
    pernas = voo.flights
    d, a = pernas[0].departure, pernas[-1].arrival
    preco = int(voo.price)
    return Oferta(
        # O Google nao separa tarifa de taxa de embarque: o numero dele ja e o
        # "com taxas". Repetimos nos tres campos para o alerta funcionar em
        # qualquer base_preco, e linha() omite a decomposicao por serem iguais.
        preco=preco,
        tarifa=preco,
        com_taxas=preco,
        total=preco,
        companhia="/".join(voo.airlines) or voo.type,
        rota="-".join([pernas[0].from_airport.code] + [p.to_airport.code for p in pernas]),
        partida=f"{d.date[2]:02d}/{d.date[1]:02d} {d.time[0]:02d}:{d.time[1]:02d}",
        chegada=f"{a.date[2]:02d}/{a.date[1]:02d} {a.time[0]:02d}:{a.time[1]:02d}",
        paradas=len(pernas) - 1,
        destino=destino,
        fonte=GOOGLE,
        data_ida=ida,
        data_volta=volta,
    )


def buscar_google(cfg: dict, destino: str, ida: str, volta: str,
                  proxy: str | None, log) -> Resultado:
    res = Resultado(
        fonte=GOOGLE, destino=destino, data_ida=ida, data_volta=volta,
        url=_query_google(cfg, destino, ida, volta, None).url(),
    )
    pausa = cfg["pausa_entre_buscas_seg"]

    for teto in list(cfg["degraus_preco"]) + [None]:
        rotulo = f"ate R$ {teto}" if teto else "sem teto"
        query = _query_google(cfg, destino, ida, volta, teto)

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
            log(f"  [Google] {res.rotulo} {rotulo}: nada")
            time.sleep(pausa)
            continue
        except RuntimeError as exc:
            log(f"  [Google] {res.rotulo} {rotulo}: ERRO {exc}")
            res.erro = str(exc)
            time.sleep(pausa)
            continue

        res.ofertas = sorted((_oferta_google(v, destino, ida, volta) for v in voos),
                             key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
        res.erro = None
        log(f"  [Google] {res.rotulo} {rotulo}: {len(voos)} ofertas, piso R$ {res.piso}")
        return res

    return res


# --------------------------------------------------------------------------- #
# Vai de Promo
# --------------------------------------------------------------------------- #

def _aammdd(data_iso: str) -> str:
    return datetime.fromisoformat(data_iso).strftime("%y%m%d")


def _token_vdp(cfg: dict, destino: str, ida: str, volta: str) -> str:
    """Ida e volta usam hifen: FLNGIG261031-GIGFLN261122 (descoberto testando a API)."""
    ori = cfg["origem"]
    return f"{ori}{destino}{_aammdd(ida)}-{destino}{ori}{_aammdd(volta)}"


def _oferta_vdp(cfg: dict, dados: dict, rec: dict, destino: str,
                ida: str, volta: str) -> Oferta | None:
    preco = dados["prices"][rec["prices"][0]]
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
    # fare e o numero grande da tela ("no PIX"), amount soma a taxa de embarque
    # e total ainda soma a taxa de servico. Ver "Bases de preco" no topo.
    total = round(preco.get("total") or 0)
    com_taxas = round(preco.get("amount") or total)
    tarifa = round(preco.get("fare") or com_taxas)
    return Oferta(
        preco=escolher_base(cfg, tarifa, com_taxas, total),
        tarifa=tarifa,
        com_taxas=com_taxas,
        total=total,
        companhia="/".join(sorted(nomes.get(c, c) for c in codigos)) or "?",
        rota=rota,
        partida=hora(segmentos[0]["departure"]["date"]),
        chegada=hora(segmentos[-1]["arrival"]["date"]),
        paradas=int(jornada.get("total_stops") or 0),
        destino=destino,
        fonte=VAIDEPROMO,
        data_ida=ida,
        data_volta=volta,
    )


def _cliente_vdp(cfg: dict):
    return primp.Client(impersonate=VDP_IMPERSONATE, timeout=cfg.get("timeout_vdp_seg", 120))


def _json_vdp(cliente, url: str) -> dict:
    r = cliente.get(url, headers=VDP_HEADERS)
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}")
    return json.loads(r.text)


def listar_providers(cfg: dict, destino: str, ida: str, log, cliente=None) -> list[dict]:
    """Companhias que atendem a rota. Nao depende da data de volta, entao o
    monitor busca uma vez por destino e reaproveita em toda a janela."""
    cliente = cliente or _cliente_vdp(cfg)
    url = VDP_PROVIDERS.format(ori=cfg["origem"], des=destino, data=_aammdd(ida))
    return _tentar(lambda: _json_vdp(cliente, url), cfg["tentativas"], log).get("providers", [])


def urls_de_busca(cfg: dict, providers: list[dict], token: str) -> dict[str, str]:
    """A lista vem com repeticoes (I8 tres vezes, WB e TS duas) que diferem so
    no mileage_carrier e produzem exatamente a mesma URL. Sem o dedup a rodada
    dispara 10 requisicoes onde existem 6 buscas distintas."""
    urls: dict[str, str] = {}
    for prov in providers:
        codigo = prov.get("code")
        base = prov.get("endpoint")
        if not codigo or not base:
            continue
        urls.setdefault(codigo, f"{base}/api/search/{token}/{cfg['adultos']}/0/0/Y/{codigo}")
    return urls


def buscar_vaidepromo(cfg: dict, destino: str, ida: str, volta: str, log,
                      providers: list[dict] | None = None) -> Resultado:
    token = _token_vdp(cfg, destino, ida, volta)
    res = Resultado(
        fonte=VAIDEPROMO,
        destino=destino,
        data_ida=ida,
        data_volta=volta,
        url=VDP_PAGINA.format(token=token, ad=cfg["adultos"]),
    )
    cliente = _cliente_vdp(cfg)
    pausa = cfg["pausa_entre_buscas_seg"]

    if providers is None:
        try:
            providers = listar_providers(cfg, destino, ida, log, cliente)
        except RuntimeError as exc:
            log(f"  [VaiDePromo] {res.rotulo}: ERRO ao listar companhias — {exc}")
            res.erro = str(exc)
            return res

    todas: list[Oferta] = []
    falhas: list[str] = []
    for codigo, url in urls_de_busca(cfg, providers, token).items():
        try:
            dados = _tentar(lambda u=url: _json_vdp(cliente, u), cfg["tentativas"], log)
        except RuntimeError as exc:
            log(f"  [VaiDePromo] {res.rotulo} {codigo}: ERRO {exc}")
            falhas.append(codigo)
            time.sleep(pausa)
            continue

        if dados.get("errors"):
            log(f"  [VaiDePromo] {res.rotulo} {codigo}: sem tarifa")
            time.sleep(pausa)
            continue

        do_provider = [
            o for o in (_oferta_vdp(cfg, dados, rec, destino, ida, volta)
                        for rec in dados.get("recommendations", []))
            if o is not None
        ]
        if do_provider:
            log(f"  [VaiDePromo] {res.rotulo} {codigo}: {len(do_provider)} tarifas, "
                f"piso R$ {min(o.preco for o in do_provider)}")
            todas.extend(do_provider)
        time.sleep(pausa)

    if todas:
        res.ofertas = sorted(todas, key=lambda o: o.preco)
        res.piso = res.ofertas[0].preco
    elif falhas:
        res.erro = "falha nas companhias: " + ", ".join(falhas)
    return res
